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

    def get_required_realtime_fids(self):
        return {
            "현재가",
            "시가",
            "누적거래량",
            "(최우선)매수호가",
        }

    def set_universe_real_time(
        self,
        register_market=True,
        codes_override=None,
        force_refresh=False,
        fid_names_override=None,
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

        fid_names = (
            set(fid_names_override)
            if fid_names_override is not None
            else self.get_required_realtime_fids()
        )
        fids = ";".join(
            get_fid(name)
            for name in sorted(fid_names)
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

        if not self._order_allowed():
            return

        order_info = self.kiwoom.order.get(
            code,
            {},
        )

        if int(
            order_info.get("미체결수량", 0)
            or 0
        ) > 0:
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

            else:
                # 체결 반영/전량매도 정리 중 동일 전략의 DB row가 남아 있으면
                # 즉시 재매수하지 않는다.
                if (
                    get_position_strategy(code)
                    == self.strategy_name
                ):
                    return

                delete_pullback_runtime_state(
                    code
                )

                self.check_buy_signal_and_order(
                    code,
                    tick=tick,
                )

        except Exception as exc:
            print(
                "[PullbackTrend] 실시간 검사 오류 "
                f"{code}: {exc}"
            )

    def check_buy_signal_and_order(
        self,
        code,
        tick=None,
    ):
        if (
            check_adjacent_transaction_closed_for_buying()
            or not check_transaction_open()
        ):
            return False

        if code in self.kiwoom.balance:
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

        metric = self._metric(code)
        if not metric:
            return False

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
            return False

        mas = self._dynamic_mas(
            code,
            current_price,
        )

        if mas is None:
            return False

        ma5, ma20, ma60 = mas

        if ma20 <= 0:
            return False

        distance = (
            current_price
            / ma20
            * 100.0
        )

        if not (ma5 > ma20 > ma60):
            return False

        if not (
            self.DISTANCE_MIN_PCT
            <= distance
            <= self.DISTANCE_MAX_PCT
        ):
            return False

        if current_price < ma20:
            return False

        bid = int(
            rt.get(
                "(최우선)매수호가",
                0,
            )
            or 0
        )

        if bid <= 0:
            return False

        if self.buy_order_handler is None:
            print(
                "[PullbackTrend] "
                "Manager buy_order_handler가 "
                "연결되지 않았습니다."
            )
            return False

        order = self.buy_order_handler(
            strategy_name=self.strategy_name,
            code=code,
            code_name=self.universe[code][
                "code_name"
            ],
            price=bid,
            rqname="pullback_buy",
            screen_no="2101",
        )

        if order is None:
            return False

        quantity = int(
            order["quantity"]
        )

        save_position_strategy(
            code=code,
            code_name=self.universe[code][
                "code_name"
            ],
            strategy_name=self.strategy_name,
            quantity=0,
            buy_price=0,
        )

        # 과거 동일 종목 거래의 전고점 상태는 신규 진입 시 초기화.
        delete_pullback_runtime_state(code)

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

        send_message(
            f"[눌림목 매수] "
            f"{self.universe[code]['code_name']}"
            f"({code}) "
            f"{quantity}주 {bid:,}원 / "
            f"MA5 {ma5:,.1f} > "
            f"MA20 {ma20:,.1f} > "
            f"MA60 {ma60:,.1f} / "
            f"이격도 {distance:.2f}%"
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

    def manage_position(self, code, tick):
        current_price = float(tick.get("현재가", 0) or 0)
        if current_price <= 0:
            return False

        # 1. 위험 청산은 전고점 부분익절보다 우선한다.
        if self.is_stop_loss_triggered(code, tick=tick):
            return self.order_sell(
                code,
                ratio=1.0,
                reason="-5% 고정손절",
            )

        mas = self._dynamic_mas(code, current_price)
        if mas is not None:
            _, ma20, _ = mas
            if current_price < ma20:
                return self.order_sell(
                    code,
                    ratio=1.0,
                    reason="20일선 이탈",
                )

        if self._bearish_volume_exit(code, tick):
            return self.order_sell(
                code,
                ratio=1.0,
                reason="전일대비 거래량 15% 이상 증가 + 음봉",
            )

        metric = self._metric(code)
        if not metric:
            return False

        state = get_pullback_runtime_state(code) or {}
        previous_high = float(
            state.get("previous_high")
            or metric.get("previous_high", 0)
            or 0
        )

        # 전고점 터치 당일에는 이후 틱으로 거래량 스냅샷을 계속 갱신한다.
        # 그래야 다음 거래일의 거래량 감소 판단 기준이 '터치 순간 거래량'이 아니라
        # 터치 당일의 가능한 최신 누적거래량에 가깝게 유지된다.
        today = datetime.now().strftime("%Y%m%d")
        if state.get("high_touch_date") == today:
            save_pullback_runtime_state(
                code=code,
                strategy_name=self.strategy_name,
                high_touch_volume=int(
                    tick.get("누적거래량", 0) or 0
                ),
            )
            state = get_pullback_runtime_state(code) or state

        # 2. 전고점 도달 다음날 거래량 둔화 -> 잔량 전량 청산
        if (
            state.get("partial_exit_requested")
            and self._is_next_day_volume_fade(code, tick, state)
        ):
            return self.order_sell(
                code,
                ratio=1.0,
                reason="전고점 도달 다음날 거래량 감소",
            )

        # 3. 전고점 최초 도달 -> 30% 부분익절
        if (
            previous_high > 0
            and current_price >= previous_high
            and not state.get("partial_exit_requested", False)
        ):
            ordered = self.order_sell(
                code,
                ratio=self.PARTIAL_EXIT_RATIO,
                reason="전고점 도달 30% 부분익절",
            )

            # 실제 주문 전송에 성공했을 때만 부분익절 상태를 기록한다.
            if ordered:
                save_pullback_runtime_state(
                    code=code,
                    strategy_name=self.strategy_name,
                    previous_high=previous_high,
                    high_touch_date=today,
                    high_touch_volume=int(
                        tick.get("누적거래량", 0) or 0
                    ),
                    partial_exit_requested=True,
                )

            return ordered

        return False

    def order_sell(
        self,
        code,
        ratio=1.0,
        reason="매도조건",
    ):
        if code not in self.kiwoom.balance:
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
            send_message(
                f"[눌림목 매도 실패] "
                f"{self.universe[code]['code_name']}"
                f"({code}) "
                f"{quantity}주 / "
                f"{reason} / "
                f"result={result}"
            )
            return False

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
