from datetime import datetime
import math
import time
import traceback

from PyQt5.QtCore import QThread

from util.const import get_fid
from util.db_helper import (
    get_position_strategy,
    save_position_strategy,
    save_strategy_signal,
    update_strategy_signal_order_result,
)
from util.market_history import (
    MIN_HISTORY_DAYS,
    get_db_path as get_market_history_db_path,
    load_breakout_metrics,
    load_company_stock_master,
)
from util.notifier import send_message
from util.time_helper import (
    check_adjacent_transaction_closed_for_buying,
    check_transaction_open,
)


class HighBreakoutStrategy(QThread):
    """KOSPI+KOSDAQ 기업주식 전체를 실시간 이벤트로 감시하는 신고가 전략.

    핵심 구조
    ---------
    - 과거 일봉은 market_history.db에서만 읽는다.
    - 장 시작 전에 직전 60일 최고가/20일 평균거래량/20일 평균거래대금을 종목별 1회 계산한다.
    - 장중에는 pandas rolling을 반복하지 않고 실시간 틱 숫자만 비교한다.
    - 종목을 1초마다 순회하지 않고 Kiwoom 주식체결 이벤트가 온 종목만 즉시 검사한다.
    - 매수 수량/예산/계좌 전체 보유수 제한은 StrategyManager가 담당한다.
    """

    strategy_name = "HighBreakoutStrategy"
    event_driven = True

    BREAKOUT_WINDOW = 60
    VOLUME_AVG_WINDOW = 20
    MIN_AVG_TRADING_VALUE = 2_000_000_000  # 최근 20봉 평균 거래대금 최소 20억원
    STOP_LOSS_PCT = -5.0

    REALTIME_CHUNK_SIZE = 90
    REALTIME_SCREEN_START = 3000
    MAX_REALTIME_SCREENS = 190

    def __init__(self, kiwoom, auto_init=True):
        super().__init__()
        self.kiwoom = kiwoom
        self.universe = {}
        self.is_init_success = False
        self.breakout_metrics = {}

        self.listener_registered = False
        self.realtime_registered = False

        self.order_guard = None
        self.buy_order_handler = None
        self.buy_rejection_reason_handler = None

        self.market_history_db_path = get_market_history_db_path()
        self.last_signal_log_at = {}

        if auto_init:
            self.init_strategy()

    def set_order_guard(self, callback):
        """Manager의 계좌/예수금 안전상태를 실시간 주문 직전에 확인한다."""
        self.order_guard = callback

    def set_buy_order_handler(self, callback):
        """실제 매수 수량/자금 예약/SendOrder를 Manager에 위임한다."""
        self.buy_order_handler = callback

    def set_buy_rejection_reason_handler(self, callback):
        """Manager가 매수를 거절했을 때 웹 신호 로그에 남길 사유 조회기를 연결한다."""
        self.buy_rejection_reason_handler = callback

    def _get_buy_rejection_reason(self, code):
        if self.buy_rejection_reason_handler is None:
            return "StrategyManager에서 신규매수를 승인하지 않음"
        try:
            return (
                self.buy_rejection_reason_handler(code)
                or "StrategyManager에서 신규매수를 승인하지 않음"
            )
        except Exception:
            return "StrategyManager 신규매수 거절 사유 조회 실패"

    def _order_allowed(self):
        if self.order_guard is None:
            return self.is_init_success

        try:
            return bool(self.order_guard())
        except Exception:
            return False

    def init_strategy(self):
        """
        단독 실행 호환용.

        main.py에서는 auto_init=False로 만들고 StrategyManager가 초기화하므로
        일반 실행에서는 이 경로를 사용하지 않는다.
        """
        try:
            self.kiwoom.get_order()
            self.kiwoom.get_balance()

            self.check_and_get_universe()
            self.check_and_get_price_data()
            self.set_universe_real_time()

            self.is_init_success = True
            send_message(
                "[HighBreakoutStrategy] "
                "전체 기업주식 실시간 초기화 완료"
            )

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    def check_and_get_universe(self, shared_universe_df=None):
        """market_history.db의 KOSPI+KOSDAQ 기업주식 마스터를 사용한다."""
        master_df = load_company_stock_master(
            db_path=self.market_history_db_path
        )

        if master_df.empty:
            raise RuntimeError(
                "stock_master가 비어 있습니다. "
                "64bit Python으로 bootstrap_market_history.py를 "
                "먼저 실행하세요."
            )

        self.universe = {
            str(code).strip().upper().zfill(6): {
                "code_name": str(name).strip() or str(code),
                "market": str(market),
                "holding": False,
            }
            for code, name, market in zip(
                master_df["code"],
                master_df["code_name"],
                master_df["market"],
            )
        }

        # 시총/상장상태 변화와 무관하게 기존 보유 종목은 매도 관리를 위해 유지한다.
        for raw_code, balance_info in self.kiwoom.balance.items():
            code = str(raw_code).strip().upper().zfill(6)
            code_name = (
                balance_info.get("종목명")
                or self.kiwoom.get_master_code_name(code)
                or code
            )

            if code not in self.universe:
                self.universe[code] = {
                    "code_name": code_name,
                    "market": "HOLDING",
                    "holding": True,
                }
            else:
                self.universe[code]["holding"] = True

        print(
            f"[HighBreakout] 기업주식 유니버스: "
            f"{len(self.universe)}종목"
        )

    def check_and_get_price_data(self, shared_price_map=None):
        """신고가 계산에 필요한 요약 지표만 메모리에 올린다."""
        codes = list(self.universe.keys())

        metrics = load_breakout_metrics(
            codes,
            breakout_window=self.BREAKOUT_WINDOW,
            volume_window=self.VOLUME_AVG_WINDOW,
            ma_window=20,
            db_path=self.market_history_db_path,
        )

        self.breakout_metrics = metrics
        missing = [
            code
            for code in codes
            if code not in metrics
        ]

        print(
            "[HighBreakout] 신고가 기준값 준비: "
            f"{len(metrics)}/{len(codes)}종목 / "
            f"이력부족 {len(missing)}종목"
        )

        if not metrics:
            raise RuntimeError(
                "신고가 계산 가능한 종목이 없습니다. "
                f"최소 {MIN_HISTORY_DAYS}거래일을 구축하세요."
            )

    def get_realtime_candidate_codes(self):
        """신규매수 가능 종목 + 이 전략 기존 보유종목."""
        eligible_codes = set(
            self.breakout_metrics.keys()
        )

        holding_codes = {
            str(code).strip().upper().zfill(6)
            for code in self.kiwoom.balance
            if get_position_strategy(code)
            == self.strategy_name
        }

        return sorted(
            eligible_codes | holding_codes
        )

    def set_universe_real_time(
        self,
        register_market=True,
        codes_override=None,
        force_refresh=False,
    ):
        if not self.listener_registered:
            self.kiwoom.add_realtime_listener(
                self.on_realtime_tick
            )
            self.listener_registered = True

        # Manager가 두 전략의 후보 합집합을 한 번 등록하기 위해
        # 초기화 단계에서는 listener만 붙일 수 있다.
        if not register_market:
            return

        if (
            self.realtime_registered
            and not force_refresh
        ):
            return

        codes = (
            sorted(
                set(
                    str(code)
                    .strip()
                    .upper()
                    .zfill(6)
                    for code in codes_override
                )
            )
            if codes_override is not None
            else self.get_realtime_candidate_codes()
        )

        required_screens = (
            math.ceil(
                len(codes)
                / self.REALTIME_CHUNK_SIZE
            )
            if codes
            else 0
        )

        if required_screens > self.MAX_REALTIME_SCREENS:
            raise RuntimeError(
                "신고가 실시간 등록에 "
                f"{required_screens}개 화면이 필요합니다. "
                f"안전 한도 {self.MAX_REALTIME_SCREENS}개를 "
                "초과했습니다."
            )

        fids = ";".join(
            [
                get_fid("체결시간"),
                get_fid("현재가"),
                get_fid("시가"),
                get_fid("고가"),
                get_fid("저가"),
                get_fid("누적거래량"),
                get_fid("(최우선)매도호가"),
                get_fid("(최우선)매수호가"),
            ]
        )

        for i in range(
            0,
            len(codes),
            self.REALTIME_CHUNK_SIZE,
        ):
            screen_no = str(
                self.REALTIME_SCREEN_START
                + (i // self.REALTIME_CHUNK_SIZE)
            )
            code_chunk = ";".join(
                codes[
                    i : i
                    + self.REALTIME_CHUNK_SIZE
                ]
            )

            result = self.kiwoom.set_real_reg(
                screen_no,
                code_chunk,
                fids,
                "0",
            )

            if result not in (0, None):
                raise RuntimeError(
                    "실시간 등록 실패: "
                    f"screen={screen_no}, "
                    f"result={result}"
                )

        self.realtime_registered = True

        print(
            "[HighBreakout] 공통 실시간 이벤트 등록 완료: "
            f"{len(codes)}종목 / "
            f"{required_screens}화면"
        )

    def on_realtime_tick(self, raw_code, tick):
        """체결 이벤트가 들어온 종목만 즉시 검사한다.

        웹 분석을 위해 전략 조건 판정을 주문 가능 여부보다 먼저 수행한다.
        따라서 계좌 안전중지/예산 부족/최대 보유수 도달 상황에서도
        기술적 매수·매도 신호 자체는 strategy_signal_log에 남는다.
        """
        code = str(raw_code).strip().upper().zfill(6)

        if code not in self.universe or not self.is_init_success:
            return

        try:
            if code in self.kiwoom.balance:
                if get_position_strategy(code) != self.strategy_name:
                    return

                sell_signal = self.get_sell_signal(code, tick=tick)
                if sell_signal is None:
                    return

                signal_key = self._save_sell_signal(code, tick, sell_signal)

                order_info = self.kiwoom.order.get(code, {})
                if int(order_info.get("미체결수량", 0) or 0) > 0:
                    update_strategy_signal_order_result(
                        signal_key,
                        order_attempted=False,
                        order_result="BLOCKED",
                        blocked_reason="동일 종목 미체결 주문 존재",
                    )
                    return

                if not self._order_allowed():
                    update_strategy_signal_order_result(
                        signal_key,
                        order_attempted=False,
                        order_result="BLOCKED",
                        blocked_reason="StrategyManager 주문 안전조건 미충족",
                    )
                    return

                self.order_sell(
                    code,
                    tick=tick,
                    signal=sell_signal,
                    signal_key=signal_key,
                )
                return

            self.check_buy_signal_and_order(code, tick=tick)

        except Exception as exc:
            print(f"[HighBreakout] 실시간 검사 오류 {code}: {exc}")

    def _metric(self, code):
        return self.breakout_metrics.get(
            str(code)
            .strip()
            .upper()
            .zfill(6)
        )

    def _dynamic_ma20(
        self,
        code,
        current_price,
    ):
        metric = self._metric(code)

        if not metric:
            return None

        if (
            int(
                metric.get(
                    "ma19_count",
                    0,
                )
                or 0
            )
            == 19
        ):
            return (
                float(
                    metric["ma19_close_sum"]
                )
                + float(current_price)
            ) / 20.0

        return (
            float(
                metric.get("ma20", 0)
                or 0
            )
            or None
        )

    def _get_buy_signal(self, code, tick=None):
        """순수 전략 매수조건만 판정한다. 주문 가능 여부는 여기서 판단하지 않는다."""
        metric = self._metric(code)
        if not metric:
            return None

        rt = tick or self.kiwoom.universe_realtime_transaction_info.get(code, {})
        current_price = int(rt.get("현재가", 0) or 0)
        volume = int(rt.get("누적거래량", 0) or 0)
        breakout_price = float(metric.get("breakout_price", 0) or 0)
        volume_ma20 = float(metric.get("volume_ma20", 0) or 0)
        trading_value_ma20 = float(metric.get("trading_value_ma20", 0) or 0)

        if (
            current_price <= 0
            or breakout_price <= 0
            or volume_ma20 < 0
            or trading_value_ma20 <= 0
        ):
            return None

        if not (
            current_price > breakout_price
            and volume >= volume_ma20
            and trading_value_ma20 >= self.MIN_AVG_TRADING_VALUE
        ):
            return None

        return {
            "reason_code": "BREAKOUT_ENTRY",
            "signal_reason": (
                f"직전 {self.BREAKOUT_WINDOW}일 최고가 돌파 + "
                f"누적거래량 20일 평균 이상 + 20일 평균거래대금 20억원 이상"
            ),
            "current_price": current_price,
            "condition_data": {
                "current_price": current_price,
                "breakout_price": breakout_price,
                "volume": volume,
                "volume_ma20": volume_ma20,
                "trading_value_ma20": trading_value_ma20,
                "min_avg_trading_value": self.MIN_AVG_TRADING_VALUE,
            },
        }

    def check_buy_signal_and_order(self, code, tick=None):
        # 정규장이 아니면 전략 신호로 보지 않는다.
        if not check_transaction_open():
            return False

        signal = self._get_buy_signal(code, tick=tick)
        if signal is None:
            return False

        code_name = self.universe[code]["code_name"]
        signal_key = save_strategy_signal(
            strategy_name=self.strategy_name,
            code=code,
            code_name=code_name,
            signal_type="BUY",
            reason_code=signal["reason_code"],
            signal_reason=signal["signal_reason"],
            current_price=signal["current_price"],
            condition_data=signal["condition_data"],
        )

        # 아래부터는 "조건은 맞지만 실제 주문이 가능한가"를 판단한다.
        if check_adjacent_transaction_closed_for_buying():
            update_strategy_signal_order_result(
                signal_key, False, "BLOCKED", "15:15 이후 신규매수 제한 시간"
            )
            return False

        if code in self.kiwoom.balance:
            update_strategy_signal_order_result(
                signal_key, False, "BLOCKED", "이미 보유 중인 종목"
            )
            return False

        order_info = self.kiwoom.order.get(code, {})
        if int(order_info.get("미체결수량", 0) or 0) > 0:
            update_strategy_signal_order_result(
                signal_key, False, "BLOCKED", "동일 종목 미체결 주문 존재"
            )
            return False

        if not self._order_allowed():
            update_strategy_signal_order_result(
                signal_key, False, "BLOCKED", "StrategyManager 주문 안전조건 미충족"
            )
            return False

        bid = int(
            (tick or self.kiwoom.universe_realtime_transaction_info.get(code, {})).get(
                "(최우선)매수호가", 0
            ) or 0
        )
        if bid <= 0:
            update_strategy_signal_order_result(
                signal_key, False, "BLOCKED", "최우선 매수호가를 확인할 수 없음"
            )
            return False

        if self.buy_order_handler is None:
            update_strategy_signal_order_result(
                signal_key, False, "BLOCKED", "Manager buy_order_handler 미연결"
            )
            return False

        order = self.buy_order_handler(
            strategy_name=self.strategy_name,
            code=code,
            code_name=code_name,
            price=bid,
            rqname="send_buy_order",
            screen_no="2004",
        )

        if order is None:
            update_strategy_signal_order_result(
                signal_key,
                order_attempted=False,
                order_result="BLOCKED",
                blocked_reason=self._get_buy_rejection_reason(code),
            )
            return False

        quantity = int(order["quantity"])
        update_strategy_signal_order_result(
            signal_key,
            order_attempted=True,
            order_result="ORDER_SENT",
            blocked_reason=None,
        )

        save_position_strategy(
            code=code,
            code_name=code_name,
            strategy_name=self.strategy_name,
            quantity=0,
            buy_price=0,
        )

        data = signal["condition_data"]
        send_message(
            f"[신고가돌파 매수] {code_name}({code}) "
            f"{quantity}주 {bid:,}원 / "
            f"직전{self.BREAKOUT_WINDOW}일 최고가 "
            f"{int(data['breakout_price']):,}원 돌파 / "
            f"누적거래량 {int(data['volume']):,} / "
            f"20일 평균거래대금 "
            f"{data['trading_value_ma20'] / 100_000_000:,.1f}억원"
        )
        return True

    def is_stop_loss_triggered(
        self,
        code,
        tick=None,
    ):
        if code not in self.kiwoom.balance:
            return False

        balance_info = self.kiwoom.balance[
            code
        ]

        purchase_price = float(
            balance_info.get(
                "매입가",
                balance_info.get(
                    "매입단가",
                    0,
                ),
            )
            or 0
        )

        rt = (
            tick
            or self.kiwoom
            .universe_realtime_transaction_info
            .get(code, {})
        )

        current_price = float(
            rt.get(
                "현재가",
                balance_info.get(
                    "현재가",
                    0,
                ),
            )
            or 0
        )

        if (
            purchase_price <= 0
            or current_price <= 0
        ):
            return False

        return (
            (
                current_price
                - purchase_price
            )
            / purchase_price
            * 100
        ) <= self.STOP_LOSS_PCT

    def get_sell_signal(self, code, tick=None):
        """매도조건을 사유와 조건 스냅샷 형태로 반환한다."""
        if code not in self.kiwoom.balance:
            return None

        balance_info = self.kiwoom.balance[code]
        purchase_price = float(
            balance_info.get("매입가", balance_info.get("매입단가", 0)) or 0
        )
        rt = tick or self.kiwoom.universe_realtime_transaction_info.get(code, {})
        current_price = float(rt.get("현재가", balance_info.get("현재가", 0)) or 0)

        if purchase_price <= 0 or current_price <= 0:
            return None

        return_pct = (current_price - purchase_price) / purchase_price * 100.0

        if return_pct <= self.STOP_LOSS_PCT:
            return {
                "reason_code": "STOP_LOSS",
                "signal_reason": f"수익률 {return_pct:.2f}% <= {self.STOP_LOSS_PCT:.2f}%",
                "current_price": current_price,
                "condition_data": {
                    "purchase_price": purchase_price,
                    "current_price": current_price,
                    "return_pct": return_pct,
                    "stop_loss_pct": self.STOP_LOSS_PCT,
                },
                "market_order": True,
            }

        ma20 = self._dynamic_ma20(code, current_price)
        if ma20 is not None and current_price < ma20:
            return {
                "reason_code": "MA20_BREAKDOWN",
                "signal_reason": f"현재가 {current_price:,.0f}원 < MA20 {ma20:,.1f}원",
                "current_price": current_price,
                "condition_data": {
                    "purchase_price": purchase_price,
                    "current_price": current_price,
                    "return_pct": return_pct,
                    "ma20": ma20,
                },
                "market_order": False,
            }

        return None

    def _save_sell_signal(self, code, tick, signal):
        return save_strategy_signal(
            strategy_name=self.strategy_name,
            code=code,
            code_name=self.universe[code]["code_name"],
            signal_type="SELL",
            reason_code=signal["reason_code"],
            signal_reason=signal["signal_reason"],
            current_price=signal["current_price"],
            condition_data=signal["condition_data"],
        )

    def check_sell_signal(self, code, tick=None):
        """기존 Manager 호환용 bool 인터페이스."""
        return self.get_sell_signal(code, tick=tick) is not None

    def order_sell(self, code, tick=None, signal=None, signal_key=None):
        balance_info = self.kiwoom.balance[code]
        quantity = int(
            balance_info.get("매매가능수량", 0)
            or balance_info.get("보유수량", 0)
            or 0
        )

        if signal is None:
            signal = self.get_sell_signal(code, tick=tick)
        if signal is None:
            return False
        if signal_key is None:
            signal_key = self._save_sell_signal(code, tick, signal)

        if quantity < 1:
            update_strategy_signal_order_result(
                signal_key, False, "BLOCKED", "매매가능수량이 0"
            )
            return False

        rt = tick or self.kiwoom.universe_realtime_transaction_info.get(code, {})

        if signal.get("market_order"):
            order_price = 0
            order_classification = "03"
            order_label = "[신고가돌파 손절 시장가 매도]"
        else:
            order_price = int(rt.get("(최우선)매도호가", 0) or 0)
            if order_price <= 0:
                update_strategy_signal_order_result(
                    signal_key, False, "BLOCKED", "최우선 매도호가를 확인할 수 없음"
                )
                return False
            order_classification = "00"
            order_label = "[신고가돌파 매도]"

        result = self.kiwoom.send_order(
            "send_sell_order",
            "2004",
            2,
            code,
            quantity,
            order_price,
            order_classification,
            strategy_name=self.strategy_name,
        )

        if result != 0:
            update_strategy_signal_order_result(
                signal_key,
                order_attempted=True,
                order_result="ORDER_FAILED",
                blocked_reason=f"Kiwoom SendOrder 실패 result={result}",
            )
            send_message(
                f"{order_label} 주문 실패: "
                f"{self.universe[code]['code_name']}({code}) result={result}"
            )
            return False

        update_strategy_signal_order_result(
            signal_key, True, "ORDER_SENT", None
        )

        self.kiwoom.order[code] = {
            "주문구분": "매도",
            "미체결수량": quantity,
            "strategy_name": self.strategy_name,
            "order_time": time.time(),
        }

        price_text = "시장가" if signal.get("market_order") else f"{order_price:,}원"
        send_message(
            f"{order_label} {self.universe[code]['code_name']}({code}) "
            f"{quantity}주 {price_text} / 사유: {signal['signal_reason']}"
        )
        return True

    def check_code(self, code):
        """Manager 호환용. 신규매수는 실시간 이벤트에서 처리한다."""
        code = str(code).strip().upper().zfill(6)
        if code not in self.universe:
            return False

        if code in self.kiwoom.balance:
            if get_position_strategy(code) != self.strategy_name:
                return False

            signal = self.get_sell_signal(code)
            if signal is None:
                return False

            signal_key = self._save_sell_signal(code, None, signal)
            order_info = self.kiwoom.order.get(code, {})
            if int(order_info.get("미체결수량", 0) or 0) > 0:
                update_strategy_signal_order_result(
                    signal_key, False, "BLOCKED", "동일 종목 미체결 주문 존재"
                )
                return False

            if not self._order_allowed():
                update_strategy_signal_order_result(
                    signal_key, False, "BLOCKED", "StrategyManager 주문 안전조건 미충족"
                )
                return False

            return self.order_sell(code, signal=signal, signal_key=signal_key)

        return False

