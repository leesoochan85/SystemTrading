import math
import time
import traceback

from PyQt5.QtCore import QThread

from util.const import get_fid
from util.db_helper import (
    get_position_strategy,
    save_position_strategy,
)
from util.notifier import send_message
from util.time_helper import (
    check_adjacent_transaction_closed_for_buying,
    check_transaction_open,
)
from util.value_quality_data import (
    delete_value_position_state,
    ensure_value_position_state,
    get_db_path as get_value_db_path,
    load_value_candidates,
    save_value_position_state,
)


class ValueQualityStrategy(QThread):
    """
    저평가 우량주 전략.

    매수 조건
    ---------
    - 최근 4분기(TTM) PER: 0 초과 ~ 15배
    - PBR: 0 초과 ~ 1배
    - 최근연도 매출총이익률: 30% ~ 95%
    - 최근연도 총자산회전율: 1회 ~ 10회

    자동 매도 조건
    -------------
    1. 수익률 <= -10%: 전량 시장가
    2. 수익률 >= +30%: 전량 시장가

    수동 매도
    ---------
    - HTS/MTS에서 사용자가 직접 매도 가능.
    - StrategyManager가 실제 잔고를 재조회해 부분매도 수량을 DB와 맞추고,
      전량매도(잔고 0)를 2회 확인하면 strategy_position.db와
      value_quality.db의 상태를 함께 삭제한다.

    컨센서스는 매도 판단에 사용하지 않는다.
    한경 컨센서스 리포트는 StrategyManager가 정보성 Telegram 메시지로만 전송한다.

    자금/수량/계좌 전체 최대 보유수는 StrategyManager가 담당한다.
    """

    strategy_name = "ValueQualityStrategy"
    event_driven = True

    PER_MIN = 0.0
    PER_MAX = 15.0
    PBR_MIN = 0.0
    PBR_MAX = 1.0

    GROSS_MARGIN_MIN = 30.0
    GROSS_MARGIN_MAX = 95.0

    ASSET_TURNOVER_MIN = 1.0
    ASSET_TURNOVER_MAX = 10.0

    STOP_LOSS_PCT = -10.0
    TAKE_PROFIT_PCT = 30.0

    REALTIME_CHUNK_SIZE = 90
    REALTIME_SCREEN_START = 3400
    MAX_REALTIME_SCREENS = 190

    def __init__(self, kiwoom, auto_init=True):
        super().__init__()

        self.kiwoom = kiwoom
        self.universe = {}
        self.is_init_success = False

        self.listener_registered = False
        self.realtime_registered = False

        self.order_guard = None
        self.buy_order_handler = None

        self.value_db_path = get_value_db_path()

        if auto_init:
            self.init_strategy()

    def set_order_guard(self, callback):
        self.order_guard = callback

    def set_buy_order_handler(self, callback):
        self.buy_order_handler = callback

    def _order_allowed(self):
        if self.order_guard is None:
            return self.is_init_success

        try:
            return bool(self.order_guard())
        except Exception:
            return False

    def init_strategy(self):
        try:
            self.kiwoom.get_order()
            self.kiwoom.get_balance()

            self.check_and_get_universe()
            self.check_and_get_price_data()
            self.set_universe_real_time()

            self.is_init_success = True

            send_message(
                "[ValueQualityStrategy] "
                "저평가 우량주 전략 초기화 완료"
            )

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    def check_and_get_universe(
        self,
        shared_universe_df=None,
    ):
        candidates = load_value_candidates(
            per_min=self.PER_MIN,
            per_max=self.PER_MAX,
            pbr_min=self.PBR_MIN,
            pbr_max=self.PBR_MAX,
            gross_margin_min=self.GROSS_MARGIN_MIN,
            gross_margin_max=self.GROSS_MARGIN_MAX,
            asset_turnover_min=self.ASSET_TURNOVER_MIN,
            asset_turnover_max=self.ASSET_TURNOVER_MAX,
            db_path=self.value_db_path,
        )

        self.universe = {
            code: {
                **info,
                "holding": False,
            }
            for code, info in candidates.items()
        }

        # 현재 재무조건에서 벗어나더라도 이 전략의 보유종목은
        # -10%/+30% 매도 관리를 위해 실시간 감시에 유지한다.
        for raw_code, balance_info in self.kiwoom.balance.items():
            code = str(raw_code).strip().zfill(6)

            if (
                get_position_strategy(code)
                != self.strategy_name
            ):
                continue

            code_name = (
                balance_info.get("종목명")
                or self.kiwoom.get_master_code_name(code)
                or code
            )

            if code not in self.universe:
                self.universe[code] = {
                    "code": code,
                    "code_name": code_name,
                    "holding": True,
                    "qualified": False,
                }
            else:
                self.universe[code]["holding"] = True

            buy_price = float(
                balance_info.get("매입가")
                or balance_info.get("매입단가")
                or 0
            )

            ensure_value_position_state(
                code=code,
                strategy_name=self.strategy_name,
                entry_price=buy_price,
                db_path=self.value_db_path,
            )

        qualified_count = sum(
            1
            for info in self.universe.values()
            if self._fundamental_values_present(info)
        )

        print(
            "[ValueQuality] 저평가 우량주 후보 준비: "
            f"{qualified_count}종목 / "
            f"실시간 관리 {len(self.universe)}종목"
        )

    def check_and_get_price_data(
        self,
        shared_price_map=None,
    ):
        # 재무 필터는 value_quality.db,
        # 가격은 Kiwoom 실시간 체결을 사용한다.
        return None

    def get_realtime_candidate_codes(self):
        return sorted(self.universe.keys())

    def get_required_realtime_fids(self):
        return {
            "현재가",
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
                    str(code).strip().zfill(6)
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
                "저평가 우량주 실시간 등록에 "
                f"{required_screens}개 화면이 필요합니다. "
                f"안전 한도 {self.MAX_REALTIME_SCREENS}개를 초과했습니다."
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
                + i // self.REALTIME_CHUNK_SIZE
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
                    "저평가 우량주 실시간 등록 실패: "
                    f"screen={screen_no}, result={result}"
                )

        self.realtime_registered = True

        print(
            "[ValueQuality] 실시간 이벤트 등록 완료: "
            f"{len(codes)}종목 / "
            f"{required_screens}화면"
        )

    @staticmethod
    def _fundamental_values_present(info):
        return all(
            info.get(key) is not None
            for key in (
                "per_ttm",
                "pbr",
                "gross_margin_pct",
                "asset_turnover",
            )
        )

    def _is_qualified_candidate(self, code):
        info = self.universe.get(code, {})

        try:
            per_ttm = float(info.get("per_ttm"))
            pbr = float(info.get("pbr"))
            gross_margin = float(
                info.get("gross_margin_pct")
            )
            asset_turnover = float(
                info.get("asset_turnover")
            )
        except (TypeError, ValueError):
            return False

        return (
            self.PER_MIN < per_ttm <= self.PER_MAX
            and self.PBR_MIN < pbr <= self.PBR_MAX
            and self.GROSS_MARGIN_MIN
            <= gross_margin
            <= self.GROSS_MARGIN_MAX
            and self.ASSET_TURNOVER_MIN
            <= asset_turnover
            <= self.ASSET_TURNOVER_MAX
        )

    def on_realtime_tick(
        self,
        raw_code,
        tick,
    ):
        code = str(raw_code).strip().zfill(6)

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
                return

            # 전량 수동매도 후 Manager 잔고 동기화가 끝날 때까지
            # position_strategy가 남아 있으면 재매수를 막는다.
            if (
                get_position_strategy(code)
                == self.strategy_name
            ):
                return

            self.check_buy_signal_and_order(
                code,
                tick=tick,
            )

        except Exception as exc:
            print(
                "[ValueQuality] 실시간 검사 오류 "
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

        if not self._is_qualified_candidate(code):
            return False

        rt = (
            tick
            or self.kiwoom
            .universe_realtime_transaction_info
            .get(code, {})
        )

        current_price = int(
            rt.get("현재가", 0)
            or 0
        )
        bid = int(
            rt.get("(최우선)매수호가", 0)
            or 0
        )

        order_price = bid or current_price

        if order_price <= 0:
            return False

        if self.buy_order_handler is None:
            print(
                "[ValueQuality] "
                "Manager buy_order_handler가 연결되지 않았습니다."
            )
            return False

        info = self.universe[code]
        code_name = (
            info.get("code_name")
            or code
        )

        order = self.buy_order_handler(
            strategy_name=self.strategy_name,
            code=code,
            code_name=code_name,
            price=order_price,
            rqname="value_quality_buy",
            screen_no="2201",
        )

        if order is None:
            return False

        quantity = int(
            order["quantity"]
        )

        save_position_strategy(
            code=code,
            code_name=code_name,
            strategy_name=self.strategy_name,
            quantity=0,
            buy_price=0,
        )

        save_value_position_state(
            code=code,
            strategy_name=self.strategy_name,
            entry_price=order_price,
            db_path=self.value_db_path,
        )

        send_message(
            f"[저평가 우량주 매수] "
            f"{code_name}({code}) "
            f"{quantity}주 {order_price:,}원 / "
            f"PER {float(info['per_ttm']):.2f} / "
            f"PBR {float(info['pbr']):.2f} / "
            f"매출총이익률 "
            f"{float(info['gross_margin_pct']):.2f}% / "
            f"총자산회전율 "
            f"{float(info['asset_turnover']):.2f}회"
        )

        return True

    def _get_return_rate(
        self,
        code,
        tick=None,
    ):
        if code not in self.kiwoom.balance:
            return None

        balance_info = self.kiwoom.balance[
            code
        ]

        buy_price = float(
            balance_info.get("매입가")
            or balance_info.get("매입단가")
            or 0
        )

        rt = (
            tick
            or self.kiwoom
            .universe_realtime_transaction_info
            .get(code, {})
        )

        current_price = float(
            rt.get("현재가")
            or balance_info.get("현재가")
            or 0
        )

        if (
            buy_price <= 0
            or current_price <= 0
        ):
            return None

        return (
            (current_price - buy_price)
            / buy_price
            * 100.0
        )

    def manage_position(
        self,
        code,
        tick=None,
    ):
        return_rate = self._get_return_rate(
            code,
            tick=tick,
        )

        if return_rate is None:
            return False

        if (
            return_rate
            <= self.STOP_LOSS_PCT
        ):
            return self.order_sell(
                code,
                reason=(
                    f"손실률 {return_rate:.2f}% "
                    f"<= {self.STOP_LOSS_PCT:.0f}%"
                ),
            )

        if (
            return_rate
            >= self.TAKE_PROFIT_PCT
        ):
            return self.order_sell(
                code,
                reason=(
                    f"수익률 {return_rate:.2f}% "
                    f">= +{self.TAKE_PROFIT_PCT:.0f}%"
                ),
            )

        return False

    def order_sell(
        self,
        code,
        reason,
    ):
        if code not in self.kiwoom.balance:
            return False

        balance_info = self.kiwoom.balance[
            code
        ]

        quantity = int(
            balance_info.get("매매가능수량")
            or balance_info.get("보유수량")
            or 0
        )

        if quantity <= 0:
            return False

        result = self.kiwoom.send_order(
            "value_quality_sell",
            "2202",
            2,
            code,
            quantity,
            0,
            "03",
            strategy_name=self.strategy_name,
        )

        if result != 0:
            send_message(
                f"[저평가 우량주 매도 실패] "
                f"{self.universe[code].get('code_name', code)}"
                f"({code}) "
                f"{quantity}주 / "
                f"{reason} / result={result}"
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
            f"[저평가 우량주 시장가 매도] "
            f"{self.universe[code].get('code_name', code)}"
            f"({code}) "
            f"{quantity}주 / 사유: {reason}"
        )

        return True

    def on_position_closed(
        self,
        code,
        reason="",
    ):
        """
        StrategyManager가 실제 잔고 0을 확인한 뒤 호출한다.

        자동매도든 HTS/MTS 수동매도든 동일하게
        저평가 전략 전용 보유상태를 삭제한다.
        """
        code = str(code).strip().zfill(6)

        delete_value_position_state(
            code,
            db_path=self.value_db_path,
        )

        print(
            "[ValueQuality] 포지션 상태 DB 정리: "
            f"{code} / {reason or '전량매도 확인'}"
        )

    def check_code(self, code):
        code = str(code).strip().zfill(6)

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

        if (
            code in self.kiwoom.balance
            and get_position_strategy(code)
            == self.strategy_name
        ):
            rt = (
                self.kiwoom
                .universe_realtime_transaction_info
                .get(code, {})
            )

            if not rt:
                return False

            return self.manage_position(
                code,
                tick=rt,
            )

        return False
