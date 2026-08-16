from datetime import datetime, time as dt_time
import math
import time
import traceback

from PyQt5.QtCore import QThread

from util.const import get_fid
from util.db_helper import (
    delete_pullback_runtime_state,
    get_position_strategy,
    get_pullback_runtime_state,
    save_position_strategy,
    save_pullback_runtime_state,
    save_strategy_signal,
    update_strategy_signal_order_result,
)
from util.market_history import (
    MIN_HISTORY_DAYS,
    get_db_path as get_market_history_db_path,
    has_completed_daily_bar_after,
    load_company_stock_master,
    load_daily_bar,
    load_pullback_metrics,
)
from util.notifier import send_message
from util.time_helper import (
    check_adjacent_transaction_closed_for_buying,
    check_transaction_open,
)


class PullbackTrendStrategy(QThread):
    """상승 추세의 20일선 눌림목을 전 종목 실시간 이벤트로 감시한다.

    매수
    ----
    - MA5 > MA20 > MA60
    - 이격도 97~103%
    - 현재가 >= MA20
      (둘을 함께 적용하므로 실질 허용 범위는 100~103%)
    - 실제 매수 수량/예산/계좌 전체 포지션 제한은 StrategyManager가 담당

    매도
    ----
    1) -5% 고정 손절: 전량 시장가
    2) 현재가 < MA20: 전량 시장가
    3) 당일 누적거래량 >= 전일 거래량 * 1.15 이면서 음봉: 전량 시장가
    4) 매수 전 직전 60거래일 최고가 도달: 최초 1회 보유수량 30% 시장가
    5) 전고점 도달 다음 거래일 15:20 이후,
       당일 누적거래량 < 전고점 도달일 최종 거래량: 잔량 전량 시장가
    """

    strategy_name = "PullbackTrendStrategy"
    event_driven = True

    PREVIOUS_HIGH_WINDOW = 60
    STOP_LOSS_PCT = -5.0
    DISTANCE_MIN_PCT = 97.0
    DISTANCE_MAX_PCT = 103.0
    BEARISH_VOLUME_MULTIPLIER = 1.15
    PARTIAL_EXIT_RATIO = 0.30
    VOLUME_FADE_CHECK_TIME = dt_time(
        15,
        20,
        0,
    )

    REALTIME_CHUNK_SIZE = 90
    REALTIME_SCREEN_START = 3200
    MAX_REALTIME_SCREENS = 190

    def __init__(self, kiwoom, auto_init=True):
        super().__init__()

        self.kiwoom = kiwoom
        self.universe = {}
        self.is_init_success = False
        self.metrics = {}

        self.listener_registered = False
        self.realtime_registered = False

        self.order_guard = None
        self.buy_order_handler = None
        self.buy_rejection_reason_handler = None

        self.market_history_db_path = (
            get_market_history_db_path()
        )

        if auto_init:
            self.init_strategy()

    def set_order_guard(self, callback):
        self.order_guard = callback

    def set_buy_order_handler(self, callback):
        """매수 수량/중앙 예산 예약/실제 주문을 Manager에 위임한다."""
        self.buy_order_handler = callback

    def set_buy_rejection_reason_handler(self, callback):
        """Manager의 신규매수 거절 사유 조회기를 연결한다."""
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
        """단독 실행 호환용."""
        try:
            self.kiwoom.get_order()
            self.kiwoom.get_balance()

            self.check_and_get_universe()
            self.check_and_get_price_data()
            self.set_universe_real_time(
                register_market=True
            )

            self.is_init_success = True

            send_message(
                "[PullbackTrendStrategy] "
                "전체 기업주식 실시간 초기화 완료"
            )

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    def check_and_get_universe(
        self,
        shared_universe_df=None,
    ):
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
                "code_name": str(name).strip()
                or str(code),
                "market": str(market),
                "holding": False,
            }
            for code, name, market in zip(
                master_df["code"],
                master_df["code_name"],
                master_df["market"],
            )
        }

        # 이 전략이 실제로 관리 중인 보유종목은 마스터에서 빠져도 유지.
        for (
            raw_code,
            balance_info,
        ) in self.kiwoom.balance.items():
            code = (
                str(raw_code)
                .strip()
                .upper()
                .zfill(6)
            )

            if (
                get_position_strategy(code)
                != self.strategy_name
            ):
                continue

            code_name = (
                balance_info.get("종목명")
                or self.kiwoom.get_master_code_name(
                    code
                )
                or code
            )

            if code not in self.universe:
                self.universe[code] = {
                    "code_name": code_name,
                    "market": "HOLDING",
                    "holding": True,
                }
            else:
                self.universe[code]["holding"] = (
                    True
                )

        print(
            "[PullbackTrend] 기업주식 유니버스: "
            f"{len(self.universe)}종목"
        )

    def check_and_get_price_data(
        self,
        shared_price_map=None,
    ):
        codes = list(self.universe.keys())

        self.metrics = load_pullback_metrics(
            codes,
            previous_high_window=(
                self.PREVIOUS_HIGH_WINDOW
            ),
            db_path=self.market_history_db_path,
        )

        missing = [
            code
            for code in codes
            if code not in self.metrics
        ]

        print(
            "[PullbackTrend] 기준값 준비: "
            f"{len(self.metrics)}/{len(codes)}종목 / "
            f"이력부족 {len(missing)}종목"
        )

        if not self.metrics:
            raise RuntimeError(
                "눌림목 계산 가능한 종목이 없습니다. "
                f"최소 {MIN_HISTORY_DAYS}거래일을 구축하세요."
            )

    def get_realtime_candidate_codes(self):
        eligible_codes = set(
            self.metrics.keys()
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

        if (
            required_screens
            > self.MAX_REALTIME_SCREENS
        ):
            raise RuntimeError(
                "눌림목 실시간 등록에 "
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
            "[PullbackTrend] 공통 실시간 이벤트 등록 완료: "
            f"{len(codes)}종목 / "
            f"{required_screens}화면"
        )

    def _metric(self, code):
        return self.metrics.get(
            str(code)
            .strip()
            .upper()
            .zfill(6)
        )

    def _dynamic_mas(
        self,
        code,
        current_price,
    ):
        metric = self._metric(code)

        if not metric or current_price <= 0:
            return None

        return (
            (
                float(metric["close_sum_4"])
                + current_price
            )
            / 5.0,
            (
                float(metric["close_sum_19"])
                + current_price
            )
            / 20.0,
            (
                float(metric["close_sum_59"])
                + current_price
            )
            / 60.0,
        )

    def on_realtime_tick(
        self,
        raw_code,
        tick,
    ):
        """전략 신호 판정을 주문 가능 여부보다 먼저 수행한다.

        따라서 계좌 안전중지, 최대 보유수, 예산 부족, 미체결 주문 등으로
        실제 주문이 막혀도 완전한 전략 BUY/SELL 신호는 monitoring.db에 남는다.
        """
        code = (
            str(raw_code)
            .strip()
            .upper()
            .zfill(6)
        )

        if (
            code not in self.universe
            or not self.is_init_success
        ):
            return

        try:
            if code in self.kiwoom.balance:
                if (
                    get_position_strategy(code)
                    != self.strategy_name
                ):
                    return

                self.manage_position(
                    code,
                    tick,
                )
                return

            # 체결 반영/전량매도 정리 중 동일 전략의 DB row가 남아 있으면
            # 즉시 재매수하지 않는다. 이 상태는 전략 조건이 아니라 운영 상태이므로
            # 매수 신호를 판정한 뒤 BLOCKED로 기록한다.
            self.check_buy_signal_and_order(
                code,
                tick=tick,
            )

        except Exception as exc:
            print(
                "[PullbackTrend] 실시간 검사 오류 "
                f"{code}: {exc}"
            )

    def _get_buy_signal(
        self,
        code,
        tick=None,
    ):
        """순수 눌림목 매수조건만 판정한다."""
        metric = self._metric(code)
        if not metric:
            return None

        rt = (
            tick
            or self.kiwoom
            .universe_realtime_transaction_info
            .get(code, {})
        )

        current_price = float(
            rt.get("현재가", 0) or 0
        )

        if current_price <= 0:
            return None

        mas = self._dynamic_mas(
            code,
            current_price,
        )

        if mas is None:
            return None

        ma5, ma20, ma60 = mas

        if ma20 <= 0:
            return None

        distance = (
            current_price
            / ma20
            * 100.0
        )

        if not (ma5 > ma20 > ma60):
            return None

        if not (
            self.DISTANCE_MIN_PCT
            <= distance
            <= self.DISTANCE_MAX_PCT
        ):
            return None

        if current_price < ma20:
            return None

        return {
            "reason_code": "PULLBACK_ENTRY",
            "signal_reason": (
                "MA5 > MA20 > MA60 정배열 + "
                f"MA20 이격도 {self.DISTANCE_MIN_PCT:.0f}~"
                f"{self.DISTANCE_MAX_PCT:.0f}% + 현재가 MA20 이상"
            ),
            "current_price": current_price,
            "condition_data": {
                "current_price": current_price,
                "ma5": ma5,
                "ma20": ma20,
                "ma60": ma60,
                "distance_pct": distance,
                "distance_min_pct": self.DISTANCE_MIN_PCT,
                "distance_max_pct": self.DISTANCE_MAX_PCT,
                "current_price_above_ma20": current_price >= ma20,
                "previous_high": float(
                    metric.get("previous_high", 0) or 0
                ),
            },
        }

    def check_buy_signal_and_order(
        self,
        code,
        tick=None,
    ):
        # 장외 틱은 매수 신호로 기록하지 않는다.
        if not check_transaction_open():
            return False

        signal = self._get_buy_signal(
            code,
            tick=tick,
        )

        if signal is None:
            return False

        code_name = self.universe[code][
            "code_name"
        ]

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

        # 아래부터는 전략 조건이 아니라 실제 주문 가능 여부다.
        if check_adjacent_transaction_closed_for_buying():
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "15:15 이후 신규매수 제한 시간",
            )
            return False

        if code in self.kiwoom.balance:
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "이미 보유 중인 종목",
            )
            return False

        # 체결 반영/전량매도 정리 중 DB row가 남아 있으면 재매수 차단.
        if (
            get_position_strategy(code)
            == self.strategy_name
        ):
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "기존 전략 포지션 DB 정리/체결 반영 대기",
            )
            return False

        order_info = self.kiwoom.order.get(
            code,
            {},
        )

        if int(
            order_info.get("미체결수량", 0)
            or 0
        ) > 0:
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "동일 종목 미체결 주문 존재",
            )
            return False

        if not self._order_allowed():
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "StrategyManager 주문 안전조건 미충족",
            )
            return False

        rt = (
            tick
            or self.kiwoom
            .universe_realtime_transaction_info
            .get(code, {})
        )

        bid = int(
            rt.get(
                "(최우선)매수호가",
                0,
            )
            or 0
        )

        if bid <= 0:
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "최우선 매수호가를 확인할 수 없음",
            )
            return False

        if self.buy_order_handler is None:
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "Manager buy_order_handler 미연결",
            )
            return False

        order = self.buy_order_handler(
            strategy_name=self.strategy_name,
            code=code,
            code_name=code_name,
            price=bid,
            rqname="pullback_buy",
            screen_no="2101",
        )

        if order is None:
            update_strategy_signal_order_result(
                signal_key,
                order_attempted=False,
                order_result="BLOCKED",
                blocked_reason=(
                    self._get_buy_rejection_reason(code)
                ),
            )
            return False

        quantity = int(
            order["quantity"]
        )

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

        # 과거 동일 종목 거래의 전고점 상태는 신규 진입 시 초기화.
        delete_pullback_runtime_state(code)

        metric = self._metric(code) or {}
        save_pullback_runtime_state(
            code=code,
            strategy_name=self.strategy_name,
            previous_high=float(
                metric.get(
                    "previous_high",
                    0,
                )
                or 0
            ),
            high_touch_date=None,
            high_touch_volume=0,
            partial_exit_requested=False,
        )

        data = signal["condition_data"]

        send_message(
            f"[눌림목 매수] "
            f"{code_name}"
            f"({code}) "
            f"{quantity}주 {bid:,}원 / "
            f"MA5 {data['ma5']:,.1f} > "
            f"MA20 {data['ma20']:,.1f} > "
            f"MA60 {data['ma60']:,.1f} / "
            f"이격도 {data['distance_pct']:.2f}%"
        )

        return True

    def is_stop_loss_triggered(
        self,
        code,
        tick=None,
    ):
        if code not in self.kiwoom.balance:
            return False

        info = self.kiwoom.balance[code]

        purchase_price = float(
            info.get(
                "매입가",
                info.get(
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
                info.get(
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
            * 100.0
        ) <= self.STOP_LOSS_PCT

    def _bearish_volume_exit(
        self,
        code,
        tick,
    ):
        metric = self._metric(code)

        if not metric:
            return False

        prev_volume = float(
            metric.get("prev_volume", 0)
            or 0
        )
        current_volume = float(
            tick.get("누적거래량", 0)
            or 0
        )
        open_price = float(
            tick.get("시가", 0) or 0
        )
        current_price = float(
            tick.get("현재가", 0) or 0
        )

        if min(
            prev_volume,
            open_price,
            current_price,
        ) <= 0:
            return False

        return (
            current_volume
            >= prev_volume
            * self.BEARISH_VOLUME_MULTIPLIER
            and current_price < open_price
        )

    def _touch_day_volume(
        self,
        code,
        state,
    ):
        touch_date = str(
            state.get(
                "high_touch_date",
                "",
            )
            or ""
        )

        if not touch_date:
            return 0

        bar = load_daily_bar(
            code,
            touch_date,
            db_path=self.market_history_db_path,
        )

        if bar is not None:
            return int(
                bar.get("volume", 0) or 0
            )

        return int(
            state.get(
                "high_touch_volume",
                0,
            )
            or 0
        )

    def _is_next_day_volume_fade(
        self,
        code,
        tick,
        state,
    ):
        touch_date = str(
            state.get(
                "high_touch_date",
                "",
            )
            or ""
        )
        today = datetime.now().strftime(
            "%Y%m%d"
        )

        if (
            not touch_date
            or today <= touch_date
        ):
            return False

        # touch_date 이후의 완성 일봉이 이미 존재한다면
        # 바로 다음 거래일 판단 시점은 지난 것이다.
        if has_completed_daily_bar_after(
            code,
            touch_date,
            before_date=today,
            db_path=self.market_history_db_path,
        ):
            return False

        if (
            datetime.now().time()
            < self.VOLUME_FADE_CHECK_TIME
        ):
            return False

        touch_volume = self._touch_day_volume(
            code,
            state,
        )
        current_volume = int(
            tick.get("누적거래량", 0)
            or 0
        )

        return (
            touch_volume > 0
            and current_volume < touch_volume
        )

    def get_sell_signal(
        self,
        code,
        tick,
    ):
        """매도 우선순위를 유지하면서 순수 SELL 신호를 반환한다."""
        current_price = float(
            tick.get("현재가", 0) or 0
        )

        if current_price <= 0:
            return None

        balance_info = self.kiwoom.balance.get(
            code,
            {},
        )

        purchase_price = float(
            balance_info.get("매입가")
            or balance_info.get("매입단가")
            or 0
        )

        return_rate = None
        if purchase_price > 0:
            return_rate = (
                (current_price - purchase_price)
                / purchase_price
                * 100.0
            )

        # 1. -5% 고정손절
        if (
            return_rate is not None
            and return_rate <= self.STOP_LOSS_PCT
        ):
            return {
                "reason_code": "STOP_LOSS",
                "signal_reason": "-5% 고정손절",
                "current_price": current_price,
                "ratio": 1.0,
                "condition_data": {
                    "purchase_price": purchase_price,
                    "current_price": current_price,
                    "return_pct": return_rate,
                    "stop_loss_pct": self.STOP_LOSS_PCT,
                },
            }

        # 2. 20일 이동평균선 이탈
        mas = self._dynamic_mas(
            code,
            current_price,
        )

        if mas is not None:
            _, ma20, _ = mas

            if current_price < ma20:
                return {
                    "reason_code": "MA20_BREAKDOWN",
                    "signal_reason": "20일선 이탈",
                    "current_price": current_price,
                    "ratio": 1.0,
                    "condition_data": {
                        "current_price": current_price,
                        "ma20": ma20,
                    },
                }

        # 3. 전일대비 거래량 15% 이상 증가 + 음봉
        metric = self._metric(code)
        if metric:
            prev_volume = float(
                metric.get("prev_volume", 0)
                or 0
            )
            current_volume = float(
                tick.get("누적거래량", 0)
                or 0
            )
            open_price = float(
                tick.get("시가", 0)
                or 0
            )

            bearish_volume = (
                prev_volume > 0
                and open_price > 0
                and current_volume
                >= prev_volume
                * self.BEARISH_VOLUME_MULTIPLIER
                and current_price < open_price
            )

            if bearish_volume:
                return {
                    "reason_code": "BEARISH_VOLUME",
                    "signal_reason": (
                        "전일대비 거래량 15% 이상 증가 + 음봉"
                    ),
                    "current_price": current_price,
                    "ratio": 1.0,
                    "condition_data": {
                        "current_price": current_price,
                        "open_price": open_price,
                        "current_volume": current_volume,
                        "prev_volume": prev_volume,
                        "volume_multiplier": (
                            self.BEARISH_VOLUME_MULTIPLIER
                        ),
                        "required_volume": (
                            prev_volume
                            * self.BEARISH_VOLUME_MULTIPLIER
                        ),
                        "bearish_candle": (
                            current_price < open_price
                        ),
                    },
                }

        if not metric:
            return None

        state = (
            get_pullback_runtime_state(code)
            or {}
        )

        previous_high = float(
            state.get("previous_high")
            or metric.get(
                "previous_high",
                0,
            )
            or 0
        )

        today = datetime.now().strftime(
            "%Y%m%d"
        )

        # 전고점 터치 당일에는 이후 틱으로 거래량 스냅샷을 계속 갱신한다.
        if (
            state.get("high_touch_date")
            == today
        ):
            save_pullback_runtime_state(
                code=code,
                strategy_name=self.strategy_name,
                high_touch_volume=int(
                    tick.get(
                        "누적거래량",
                        0,
                    )
                    or 0
                ),
            )
            state = (
                get_pullback_runtime_state(code)
                or state
            )

        # 4. 전고점 부분익절을 이미 주문한 다음 거래일의 거래량 감소
        if (
            state.get(
                "partial_exit_requested"
            )
            and self._is_next_day_volume_fade(
                code,
                tick,
                state,
            )
        ):
            touch_volume = self._touch_day_volume(
                code,
                state,
            )
            current_volume = int(
                tick.get("누적거래량", 0)
                or 0
            )

            return {
                "reason_code": "NEXT_DAY_VOLUME_FADE",
                "signal_reason": (
                    "전고점 도달 다음날 거래량 감소"
                ),
                "current_price": current_price,
                "ratio": 1.0,
                "condition_data": {
                    "current_price": current_price,
                    "high_touch_date": state.get(
                        "high_touch_date"
                    ),
                    "touch_day_volume": touch_volume,
                    "current_volume": current_volume,
                    "check_time": "15:20",
                    "intended_sell_ratio": 1.0,
                },
            }

        # 5. 전고점 최초 도달 -> 30% 부분익절
        if (
            previous_high > 0
            and current_price >= previous_high
            and not state.get(
                "partial_exit_requested",
                False,
            )
        ):
            return {
                "reason_code": (
                    "PREVIOUS_HIGH_PARTIAL_EXIT"
                ),
                "signal_reason": (
                    "전고점 도달 30% 부분익절"
                ),
                "current_price": current_price,
                "ratio": self.PARTIAL_EXIT_RATIO,
                "condition_data": {
                    "current_price": current_price,
                    "previous_high": previous_high,
                    "intended_sell_ratio": (
                        self.PARTIAL_EXIT_RATIO
                    ),
                    "high_touch_date": today,
                    "current_volume": int(
                        tick.get(
                            "누적거래량",
                            0,
                        )
                        or 0
                    ),
                },
            }

        return None

    def manage_position(
        self,
        code,
        tick,
    ):
        signal = self.get_sell_signal(
            code,
            tick,
        )

        if signal is None:
            return False

        code_name = self.universe.get(
            code,
            {},
        ).get(
            "code_name",
            code,
        )

        signal_key = save_strategy_signal(
            strategy_name=self.strategy_name,
            code=code,
            code_name=code_name,
            signal_type="SELL",
            reason_code=signal[
                "reason_code"
            ],
            signal_reason=signal[
                "signal_reason"
            ],
            current_price=signal[
                "current_price"
            ],
            condition_data=signal[
                "condition_data"
            ],
        )

        order_info = self.kiwoom.order.get(
            code,
            {},
        )

        if int(
            order_info.get(
                "미체결수량",
                0,
            )
            or 0
        ) > 0:
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "동일 종목 미체결 주문 존재",
            )
            return False

        if not self._order_allowed():
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "StrategyManager 주문 안전조건 미충족",
            )
            return False

        ordered = self.order_sell(
            code,
            ratio=signal["ratio"],
            reason=signal[
                "signal_reason"
            ],
            signal_key=signal_key,
        )

        # 전고점 부분익절은 실제 주문 전송 성공 후에만 상태를 변경한다.
        if (
            ordered
            and signal["reason_code"]
            == "PREVIOUS_HIGH_PARTIAL_EXIT"
        ):
            data = signal[
                "condition_data"
            ]

            save_pullback_runtime_state(
                code=code,
                strategy_name=self.strategy_name,
                previous_high=float(
                    data.get(
                        "previous_high",
                        0,
                    )
                    or 0
                ),
                high_touch_date=(
                    data.get(
                        "high_touch_date"
                    )
                ),
                high_touch_volume=int(
                    data.get(
                        "current_volume",
                        0,
                    )
                    or 0
                ),
                partial_exit_requested=True,
            )

        return ordered

    def order_sell(
        self,
        code,
        ratio=1.0,
        reason="매도조건",
        signal_key=None,
    ):
        if code not in self.kiwoom.balance:
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "실제 계좌 잔고에서 종목을 찾을 수 없음",
            )
            return False

        balance_info = self.kiwoom.balance[
            code
        ]

        sellable_quantity = int(
            balance_info.get(
                "매매가능수량",
                0,
            )
            or balance_info.get(
                "보유수량",
                0,
            )
            or 0
        )

        if sellable_quantity <= 0:
            update_strategy_signal_order_result(
                signal_key,
                False,
                "BLOCKED",
                "매매가능수량이 0",
            )
            return False

        ratio = max(
            0.0,
            min(1.0, float(ratio)),
        )

        if ratio >= 1.0:
            quantity = sellable_quantity
        else:
            quantity = max(
                1,
                int(
                    math.floor(
                        sellable_quantity
                        * ratio
                    )
                ),
            )
            quantity = min(
                quantity,
                sellable_quantity,
            )

        result = self.kiwoom.send_order(
            "pullback_market_sell",
            "2102",
            2,
            code,
            quantity,
            0,
            "03",
            strategy_name=self.strategy_name,
        )

        if result != 0:
            update_strategy_signal_order_result(
                signal_key,
                True,
                "ORDER_FAILED",
                f"Kiwoom SendOrder 실패 result={result}",
            )

            send_message(
                f"[눌림목 매도 실패] "
                f"{self.universe[code]['code_name']}"
                f"({code}) "
                f"{quantity}주 / "
                f"{reason} / "
                f"result={result}"
            )
            return False

        update_strategy_signal_order_result(
            signal_key,
            True,
            "ORDER_SENT",
            None,
        )

        self.kiwoom.order[code] = {
            "주문구분": "매도",
            "주문가격": 0,
            "미체결수량": quantity,
            "strategy_name": self.strategy_name,
            "order_time": time.time(),
        }

        send_message(
            f"[눌림목 시장가 매도] "
            f"{self.universe[code]['code_name']}({code}) "
            f"{quantity}주 / 사유: {reason}"
        )

        return True

    def check_code(self, code):
        """Manager 호환용. 신규매수는 실시간 이벤트에서 처리한다."""
        code = (
            str(code)
            .strip()
            .upper()
            .zfill(6)
        )

        if code not in self.universe:
            return False

        order_info = self.kiwoom.order.get(
            code,
            {},
        )

        if int(
            order_info.get("미체결수량", 0)
            or 0
        ) > 0:
            return False

        if code in self.kiwoom.balance:
            if (
                get_position_strategy(code)
                != self.strategy_name
            ):
                return False

            rt = (
                self.kiwoom
                .universe_realtime_transaction_info
                .get(code, {})
            )

            if not rt:
                return False

            return self.manage_position(
                code,
                rt,
            )

        return False
