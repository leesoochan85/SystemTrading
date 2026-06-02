from datetime import datetime, time as dt_time
import math
import time
import traceback

from PyQt5.QtCore import QThread

from util.const import get_fid
from util.db_helper import (
    delete_position_strategy,
    get_orb_runtime_state,
    get_position_detail,
    save_orb_runtime_state,
    save_position_strategy,
)
from util.notifier import send_message
from util.time_helper import check_transaction_open, is_market_business_day


class ORBStrategy(QThread):
    """KODEX 200 매수 전용 5분 ORB 실시간 모의투자 전략.

    매매 규칙
    ---------
    - 거래 대상: KODEX 200 (069500) 1종목
    - 장 시작 후 첫 5분이 경과하면, 첫 5분봉 방향과 관계없이 09:05 이후 시장가 매수
    - 음봉/도지인 날도 매수함(공매도/인버스 없음)
    - 매수 수량은 현금 100% 주문가능금액과 시장가 주문 버퍼를 기준으로 계산
    - 실제 투자금의 3%를 손절 예산으로 사용하되,
      계좌 총자산 1% 손실 상한은 절대 넘지 않게 제한
    - 매수 체결 후 실제 평균체결가를 기준으로 목표가 +5%를 확정
    - 손절/목표 도달 시 시장가 매도
    - 둘 다 미도달이면 15:20 종가 단일가 구간에 시장가 매도를 접수하여 종가 청산 시도

    주의
    ----
    시장가 주문의 실제 체결가가 신호가격과 달라질 수 있으므로,
    손절가와 목표가는 매수 체결가가 DB에 반영된 뒤 재계산한다.
    첫 5분봉은 당일 진입 시각 준수 및 기록 목적으로 저장하며,
    매수 여부나 손절가 결정에는 사용하지 않는다.
    """

    strategy_name = "ORBStrategy"
    CODE = "069500"
    CODE_NAME = "KODEX 200"

    OPENING_RANGE_START = dt_time(9, 0, 0)
    ENTRY_TIME = dt_time(9, 5, 0)
    ENTRY_DEADLINE = dt_time(9, 6, 0)
    CLOSE_AUCTION_EXIT_TIME = dt_time(15, 20, 0)

    ACCOUNT_RISK_PCT = 0.01
    INVESTED_AMOUNT_STOP_PCT = 0.03
    PROFIT_TARGET_PCT = 0.05

    # 시장가 매수는 주문 가능 금액 산정 시 예상 체결가보다 큰 증거금이 잡힐 수 있다.
    # 모의투자 확인 전에는 현재가 대비 30% 여유를 남기는 보수적 수량으로 주문한다.
    MARKET_BUY_CASH_BUFFER = 1.30

    # 첫 5분봉을 실제 장 시작부터 수집하지 못했다면 당일 신규 진입을 막는다.
    # KODEX 200은 유동성이 높은 ETF이므로 개장 후 30초 내 첫 체결을 수신하지 못하면
    # 프로그램 지연 또는 실시간 등록 지연 가능성이 있다고 보고 안전하게 건너뛴다.
    FIRST_TICK_LATEST_ALLOWED = dt_time(9, 0, 30)

    # 시장가 매도 요청 직후에는 체결/잔고 이벤트 반영 순서가 뒤바뀔 수 있다.
    # 바로 재주문하면 이미 체결된 수량까지 중복 매도 주문할 위험이 있으므로,
    # Manager의 60초 미체결·잔고 재조회가 한 번 지난 뒤에만 잔량 재청산을 허용한다.
    SELL_RETRY_DELAY_SECONDS = 65

    # SendOrder 자체가 실패한 경우 매초 동일 주문을 반복하지 않는다.
    # 시장가 청산 지연을 줄이기 위해 잔고 재조회 대기보다 짧게 재시도한다.
    SELL_ORDER_FAILURE_RETRY_DELAY_SECONDS = 10

    def __init__(self, kiwoom, auto_init=True):
        super().__init__()
        self.kiwoom = kiwoom
        self.universe = {}
        self.deposit = 0
        self.is_init_success = False
        self.listener_registered = False

        self.trading_date = None
        self.first_bar = None
        self.state = None
        self.alerted_missing_first_bar_date = None
        self.alerted_external_holding_date = None
        self.alerted_sell_unavailable_state = None
        self.last_sell_order_failure_at = 0
        self.last_sell_order_failure_reason = None

        if auto_init:
            self.init_strategy()

    def init_strategy(self):
        try:
            self.check_and_get_universe()
            self.check_and_get_price_data()
            self.set_universe_real_time()
            self.is_init_success = True
            send_message("[ORBStrategy] 초기화 완료 - KODEX 200(069500) 매수 전용")
        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    def check_and_get_universe(self):
        self.universe = {
            self.CODE: {
                "code_name": self.CODE_NAME,
                "holding": self.CODE in self.kiwoom.balance,
            }
        }

    def check_and_get_price_data(self):
        """ORB는 장중 첫 5분 실시간 틱을 사용하므로 일봉 초기화가 필요 없다."""
        return None

    def set_universe_real_time(self):
        fids = ";".join([
            get_fid("체결시간"),
            get_fid("현재가"),
            get_fid("시가"),
            get_fid("고가"),
            get_fid("저가"),
            get_fid("누적거래량"),
            get_fid("(최우선)매도호가"),
            get_fid("(최우선)매수호가"),
        ])

        if not self.listener_registered:
            self.kiwoom.add_realtime_listener(self.on_realtime_tick)
            self.listener_registered = True

        self.kiwoom.set_real_reg("1400", self.CODE, fids, "0")
        print(f"[ORBStrategy] 실시간 등록 완료: {self.CODE_NAME}({self.CODE})")

    def _today(self):
        return datetime.now().strftime("%Y%m%d")

    def _ensure_new_day(self):
        today = self._today()
        if self.trading_date == today:
            return

        self.trading_date = today
        self.first_bar = None
        self.state = get_orb_runtime_state(today, self.CODE)
        self.alerted_missing_first_bar_date = None
        self.alerted_external_holding_date = None
        self.alerted_sell_unavailable_state = None
        self.last_sell_order_failure_at = 0
        self.last_sell_order_failure_reason = None

    def on_realtime_tick(self, code, tick):
        """Kiwoom 주식체결 이벤트에서 09:00~09:04:59 틱을 당일 기록용 첫 5분봉으로 집계한다."""
        if str(code).zfill(6) != self.CODE:
            return

        self._ensure_new_day()
        if not is_market_business_day():
            return

        raw_time = str(tick.get("체결시간", "") or "").strip().zfill(6)
        if len(raw_time) != 6 or not raw_time.isdigit():
            return

        try:
            tick_time = datetime.strptime(raw_time, "%H%M%S").time()
        except ValueError:
            return

        if not (self.OPENING_RANGE_START <= tick_time < self.ENTRY_TIME):
            return

        price = int(tick.get("현재가", 0) or 0)
        volume = int(tick.get("누적거래량", 0) or 0)
        if price <= 0:
            return

        if self.first_bar is None:
            self.first_bar = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "first_tick_time": raw_time,
                "last_tick_time": raw_time,
                "accum_volume": volume,
            }
        else:
            self.first_bar["high"] = max(self.first_bar["high"], price)
            self.first_bar["low"] = min(self.first_bar["low"], price)
            self.first_bar["close"] = price
            self.first_bar["last_tick_time"] = raw_time
            self.first_bar["accum_volume"] = volume

    def _current_price(self):
        rt = self.kiwoom.universe_realtime_transaction_info.get(self.CODE, {})
        return int(rt.get("현재가", 0) or 0)

    def _account_equity(self):
        """D+2 추정예수금 + 실시간 평가금액으로 위험예산 기준 총자산을 계산한다."""
        cash = self.kiwoom.last_d2_estimated_deposit
        if cash is None:
            cash = self.deposit
        cash = int(cash or 0)

        evaluation_amount = 0
        for code, info in self.kiwoom.balance.items():
            quantity = int(info.get("보유수량", 0) or 0)
            rt = self.kiwoom.universe_realtime_transaction_info.get(code, {})
            current_price = int(rt.get("현재가") or info.get("현재가", 0) or 0)
            evaluation_amount += quantity * current_price

        return max(0, cash + evaluation_amount)

    def _pending_order(self):
        info = self.kiwoom.order.get(self.CODE, {})
        return int(info.get("미체결수량", 0) or 0) > 0

    def _position_detail(self):
        detail = get_position_detail(self.CODE)
        if not detail:
            return None
        if detail.get("strategy_name") != self.strategy_name:
            return None
        return detail

    def _skip_today(self, reason, message=None):
        if self.state and self.state.get("status") not in ("READY", None):
            return False

        save_orb_runtime_state(
            trade_date=self.trading_date,
            code=self.CODE,
            code_name=self.CODE_NAME,
            strategy_name=self.strategy_name,
            status="SKIPPED",
            skip_reason=reason,
            first_bar=self.first_bar,
        )
        self.state = get_orb_runtime_state(self.trading_date, self.CODE)

        if message:
            print(message)
            send_message(message)
        return False

    def _try_entry(self):
        now_time = datetime.now().time()

        if self.state and self.state.get("status") not in ("READY", None):
            return False

        if now_time < self.ENTRY_TIME:
            return False

        if now_time > self.ENTRY_DEADLINE:
            if self.first_bar is None:
                return self._skip_today(
                    "FIRST_BAR_MISSING",
                    "[ORB 미진입] 첫 5분봉 틱 데이터가 완성되지 않아 오늘 거래를 건너뜁니다.",
                )
            return self._skip_today(
                "ENTRY_TIME_MISSED",
                "[ORB 미진입] 09:05~09:06 진입 시간 범위를 놓쳐 오늘 거래를 건너뜁니다.",
            )

        if self.first_bar is None:
            return False

        if self.CODE in self.kiwoom.balance:
            if self.alerted_external_holding_date != self.trading_date:
                self.alerted_external_holding_date = self.trading_date
                return self._skip_today(
                    "EXISTING_HOLDING",
                    "[ORB 미진입] KODEX 200 기존 보유 잔고가 있어 신규 ORB 진입을 하지 않습니다.",
                )
            return False

        if self._pending_order():
            return False

        # 매일 진입 방식에서는 첫 5분봉의 방향(양봉/음봉/도지)을 진입 필터로 사용하지 않는다.
        # 다만 늦은 실행 또는 실시간 등록 지연으로 첫 5분봉을 온전히 수집하지 못한 날은
        # 잘못된 당일 기록을 남기지 않도록 신규 진입을 중단한다.
        first_tick_time = str(self.first_bar.get("first_tick_time", "") or "")
        try:
            first_tick = datetime.strptime(first_tick_time, "%H%M%S").time()
        except ValueError:
            return self._skip_today(
                "INVALID_FIRST_BAR_TIME",
                "[ORB 미진입] 첫 5분봉의 최초 체결시간을 확인할 수 없어 오늘 거래를 건너뜁니다.",
            )

        if first_tick > self.FIRST_TICK_LATEST_ALLOWED:
            return self._skip_today(
                "INCOMPLETE_FIRST_BAR",
                f"[ORB 미진입] 첫 실시간 체결 수신이 {first_tick_time}로 늦어 "
                "첫 5분봉이 불완전할 수 있으므로 오늘 거래를 건너뜁니다.",
            )

        signal_price = self._current_price()
        if signal_price <= 0:
            return self._skip_today(
                "INVALID_SIGNAL_PRICE",
                "[ORB 미진입] 진입 기준가격이 유효하지 않습니다.",
            )

        account_equity = self._account_equity()
        available_cash = int(self.deposit or 0)
        account_risk_cap = account_equity * self.ACCOUNT_RISK_PCT

        if account_equity <= 0 or available_cash <= 0:
            return self._skip_today(
                "INSUFFICIENT_ACCOUNT_DATA",
                "[ORB 미진입] 총자산 또는 주문가능금액 계산값이 유효하지 않습니다.",
            )

        # 첫 5분봉 저가까지의 손절폭으로 수량을 정하지 않는다.
        # 매일 진입하므로 시장가 매수 가능 현금을 기준으로 주문하고,
        # 체결 이후 실제 투자금 3%와 계좌자산 1% 중 작은 금액으로 손절가를 확정한다.
        quantity = int(
            available_cash // (signal_price * self.MARKET_BUY_CASH_BUFFER)
        )

        if quantity < 1:
            return self._skip_today(
                "ZERO_QUANTITY",
                "[ORB 미진입] 시장가 주문 여유금액 기준 주문 가능 수량이 0주입니다.",
            )

        estimated_purchase = signal_price * quantity
        sizing_mode = "DAILY_ENTRY_INVESTED_3PCT_CAPPED_BY_ACCOUNT_1PCT"
        planned_loss_budget = min(
            account_risk_cap,
            estimated_purchase * self.INVESTED_AMOUNT_STOP_PCT,
        )

        result = self.kiwoom.send_order(
            "orb_market_buy",
            "2401",
            1,
            self.CODE,
            quantity,
            0,
            "03",  # 시장가
            strategy_name=self.strategy_name,
        )

        if result != 0:
            return self._skip_today(
                f"BUY_ORDER_FAILED_{result}",
                f"[ORB 매수 실패] {self.CODE_NAME}({self.CODE}) 시장가 주문 result={result}",
            )

        save_position_strategy(
            code=self.CODE,
            code_name=self.CODE_NAME,
            strategy_name=self.strategy_name,
            quantity=0,
            buy_price=0,
        )
        save_orb_runtime_state(
            trade_date=self.trading_date,
            code=self.CODE,
            code_name=self.CODE_NAME,
            strategy_name=self.strategy_name,
            status="BUY_REQUESTED",
            first_bar=self.first_bar,
            signal_price=signal_price,
            planned_quantity=quantity,
            account_equity=account_equity,
            available_cash=available_cash,
            sizing_mode=sizing_mode,
            risk_budget=planned_loss_budget,
        )
        self.state = get_orb_runtime_state(self.trading_date, self.CODE)
        # Manager가 그룹 예산을 즉시 다시 계산할 수 있도록 시장가 매수 예약금액을 기록한다.
        # 실제 체결/예수금 조회 전에는 시장가 버퍼를 포함한 금액을 보수적으로 예약한다.
        reserved_cash = math.ceil(
            signal_price * quantity * self.MARKET_BUY_CASH_BUFFER
        )
        self.deposit = max(0, available_cash - reserved_cash)
        self.kiwoom.order[self.CODE] = {
            "주문구분": "매수",
            "주문가격": 0,
            "미체결수량": quantity,
            "strategy_name": self.strategy_name,
            "order_time": time.time(),
            "예산예약금액": reserved_cash,
        }

        send_message(
            f"[ORB 시장가 매수요청] {self.CODE_NAME}({self.CODE}) {quantity}주 / "
            f"첫 5분봉(기록용) {self.first_bar['open']:,}->{self.first_bar['close']:,} / "
            f"방향 무관 매일진입 / 계획 최대손실 {planned_loss_budget:,.0f}원"
        )
        return True

    def _sync_fill_and_price_levels(self):
        if not self.state or self.state.get("status") not in ("BUY_REQUESTED", "OPEN"):
            return False

        detail = self._position_detail()
        if not detail:
            return False

        quantity = int(detail.get("quantity", 0) or 0)
        entry_price = float(detail.get("buy_price", 0) or 0)
        if quantity <= 0 or entry_price <= 0:
            return False

        if (
            self.state.get("status") == "OPEN"
            and float(self.state.get("entry_price") or 0) == entry_price
            and int(self.state.get("quantity") or 0) == quantity
        ):
            return True

        sizing_mode = self.state.get("sizing_mode", "")
        account_equity = float(self.state.get("account_equity", 0) or 0)
        account_risk_cap = account_equity * self.ACCOUNT_RISK_PCT

        # 첫 5분봉 방향과 저가는 손절 계산에 사용하지 않는다.
        # 실제 체결금액의 3%와 계좌 총자산의 1% 중 더 작은 손실만 허용한다.
        allowed_loss = min(
            account_risk_cap,
            entry_price * quantity * self.INVESTED_AMOUNT_STOP_PCT,
        )
        stop_price = entry_price - (allowed_loss / quantity)

        if stop_price >= entry_price:
            send_message(
                f"[ORB 안전청산] 손절가 계산이 유효하지 않아 즉시 매도합니다: "
                f"{self.CODE_NAME} 체결가 {entry_price:,.0f}원 / 손절기준 {stop_price:,.0f}원"
            )
            self._send_market_sell("INVALID_ENTRY_AFTER_FILL", quantity)
            return False

        target_price = entry_price * (1 + self.PROFIT_TARGET_PCT)

        save_orb_runtime_state(
            trade_date=self.trading_date,
            code=self.CODE,
            code_name=self.CODE_NAME,
            strategy_name=self.strategy_name,
            status="OPEN",
            first_bar=self.first_bar,
            signal_price=self.state.get("signal_price"),
            planned_quantity=self.state.get("planned_quantity"),
            account_equity=account_equity,
            available_cash=self.state.get("available_cash"),
            sizing_mode=sizing_mode,
            risk_budget=allowed_loss,
            entry_price=entry_price,
            quantity=quantity,
            stop_price=stop_price,
            target_price=target_price,
        )
        self.state = get_orb_runtime_state(self.trading_date, self.CODE)

        send_message(
            f"[ORB 체결/청산기준 확정] {self.CODE_NAME} {quantity}주 / "
            f"체결가 {entry_price:,.0f}원 / 손절 {stop_price:,.1f}원 / "
            f"목표(+5%) {target_price:,.1f}원 / "
            f"최대손실 min(계좌1%, 투자금3%)={allowed_loss:,.0f}원"
        )
        return True

    def _sell_failure_cooldown_active(self):
        if not self.last_sell_order_failure_at:
            return False
        return (
            time.time() - self.last_sell_order_failure_at
            < self.SELL_ORDER_FAILURE_RETRY_DELAY_SECONDS
        )

    def _notify_sell_unavailable_once(self, remaining_quantity):
        warning_key = str((self.state or {}).get("updated_at", "") or "SELL_WAIT")
        if self.alerted_sell_unavailable_state == warning_key:
            return
        self.alerted_sell_unavailable_state = warning_key
        send_message(
            f"[ORB 청산 확인 필요] {self.CODE_NAME}({self.CODE}) "
            f"잔고 {remaining_quantity}주가 남아 있지만 매매가능수량이 없습니다."
        )

    def _send_market_sell(self, exit_reason, quantity=None):
        if self._pending_order():
            return False

        if self._sell_failure_cooldown_active():
            return False

        # 실제 잔고에서 매도 가능하다고 확인된 수량만 주문한다.
        # 포지션 DB 수량 또는 보유수량으로 대체하면 이미 매도 접수된 물량을
        # 중복 주문하거나 매매가능수량 0 상태에서 주문 실패를 반복할 수 있다.
        remaining_quantity = self._balance_quantity()
        sellable_quantity = self._available_sell_quantity()
        if remaining_quantity <= 0:
            return False
        if sellable_quantity <= 0:
            self._notify_sell_unavailable_once(remaining_quantity)
            return False

        if quantity is None:
            quantity = sellable_quantity
        else:
            quantity = min(int(quantity or 0), sellable_quantity)

        if quantity <= 0:
            return False

        result = self.kiwoom.send_order(
            "orb_market_sell",
            "2402",
            2,
            self.CODE,
            quantity,
            0,
            "03",  # 시장가
            strategy_name=self.strategy_name,
        )
        if result != 0:
            self.last_sell_order_failure_at = time.time()
            self.last_sell_order_failure_reason = exit_reason
            send_message(
                f"[ORB 매도 실패] {self.CODE_NAME}({self.CODE}) {exit_reason} "
                f"시장가 주문 result={result} / "
                f"{self.SELL_ORDER_FAILURE_RETRY_DELAY_SECONDS}초 후 재시도"
            )
            return False

        self.last_sell_order_failure_at = 0
        self.last_sell_order_failure_reason = None
        self.alerted_sell_unavailable_state = None

        save_orb_runtime_state(
            trade_date=self.trading_date,
            code=self.CODE,
            code_name=self.CODE_NAME,
            strategy_name=self.strategy_name,
            status="SELL_REQUESTED",
            first_bar=self.first_bar,
            signal_price=self.state.get("signal_price") if self.state else None,
            planned_quantity=self.state.get("planned_quantity") if self.state else quantity,
            account_equity=self.state.get("account_equity") if self.state else None,
            available_cash=self.state.get("available_cash") if self.state else None,
            sizing_mode=self.state.get("sizing_mode") if self.state else None,
            risk_budget=self.state.get("risk_budget") if self.state else None,
            entry_price=self.state.get("entry_price") if self.state else None,
            quantity=quantity,
            stop_price=self.state.get("stop_price") if self.state else None,
            target_price=self.state.get("target_price") if self.state else None,
            exit_reason=exit_reason,
        )
        self.state = get_orb_runtime_state(self.trading_date, self.CODE)
        self.kiwoom.order[self.CODE] = {
            "주문구분": "매도",
            "미체결수량": quantity,
            "strategy_name": self.strategy_name,
            "order_time": time.time(),
        }
        send_message(
            f"[ORB 시장가 매도요청] {self.CODE_NAME}({self.CODE}) {quantity}주 / 사유 {exit_reason}"
        )
        return True

    def _balance_quantity(self):
        """키움 잔고에 남아 있는 ORB 종목의 실제 보유수량을 반환한다."""
        balance_info = self.kiwoom.balance.get(self.CODE, {})
        return max(0, int(balance_info.get("보유수량", 0) or 0))

    def _available_sell_quantity(self):
        """재매도 시 실제로 주문 가능한 잔여 수량만 반환한다."""
        balance_info = self.kiwoom.balance.get(self.CODE, {})
        return max(0, int(balance_info.get("매매가능수량", 0) or 0))

    def _sell_request_elapsed_seconds(self):
        updated_at = str((self.state or {}).get("updated_at", "") or "")
        try:
            requested_at = datetime.strptime(updated_at, "%Y%m%d%H%M%S")
            return max(0, (datetime.now() - requested_at).total_seconds())
        except ValueError:
            # 기존 DB 상태에 시각이 비정상이면, 중복 주문보다는 재청산 허용을 우선한다.
            return self.SELL_RETRY_DELAY_SECONDS

    def _mark_closed_after_sell(self):
        """잔고가 0주로 확인된 ORB 거래를 당일 청산 완료 상태로 기록한다."""
        if self.state and self.state.get("status") == "CLOSED":
            return False

        save_orb_runtime_state(
            trade_date=self.trading_date,
            code=self.CODE,
            code_name=self.CODE_NAME,
            strategy_name=self.strategy_name,
            status="CLOSED",
            first_bar=self.first_bar,
            signal_price=self.state.get("signal_price") if self.state else None,
            planned_quantity=self.state.get("planned_quantity") if self.state else None,
            account_equity=self.state.get("account_equity") if self.state else None,
            available_cash=self.state.get("available_cash") if self.state else None,
            sizing_mode=self.state.get("sizing_mode") if self.state else None,
            risk_budget=self.state.get("risk_budget") if self.state else None,
            entry_price=self.state.get("entry_price") if self.state else None,
            quantity=0,
            stop_price=self.state.get("stop_price") if self.state else None,
            target_price=self.state.get("target_price") if self.state else None,
            exit_reason=self.state.get("exit_reason") if self.state else "SELL_FILLED",
        )
        self.state = get_orb_runtime_state(self.trading_date, self.CODE)
        send_message(f"[ORB 청산완료 확인] {self.CODE_NAME}({self.CODE}) 잔고 0주 확인")
        return True

    def _recover_sell_requested(self):
        """매도 요청 이후 미체결·부분체결·주문소멸 상황을 복구한다.

        - 매도 미체결 주문이 남아 있으면 중복 주문 없이 기다린다.
        - 실제 잔고가 0주면 CLOSED로 확정한다.
        - 잔고가 남았지만 매도 미체결이 사라졌다면 잔량을 시장가로 재청산한다.
        """
        if self._pending_order():
            return False

        remaining_quantity = self._balance_quantity()
        if remaining_quantity <= 0:
            return self._mark_closed_after_sell()

        if self._sell_request_elapsed_seconds() < self.SELL_RETRY_DELAY_SECONDS:
            return False

        sellable_quantity = self._available_sell_quantity()
        if sellable_quantity <= 0:
            self._notify_sell_unavailable_once(remaining_quantity)
            return False

        previous_reason = str(self.state.get("exit_reason", "") or "SELL_RECOVERY")
        send_message(
            f"[ORB 잔량 재청산] {self.CODE_NAME}({self.CODE}) "
            f"잔고 {remaining_quantity}주 / 재주문 {sellable_quantity}주 시장가"
        )
        return self._send_market_sell(
            f"{previous_reason}_RETRY",
            sellable_quantity,
        )

    def _monitor_exit(self):
        if not self.state:
            return False

        status = self.state.get("status")
        if status == "SELL_REQUESTED":
            return self._recover_sell_requested()

        self._sync_fill_and_price_levels()
        if self.state.get("status") != "OPEN":
            return False

        current_price = self._current_price()
        if current_price <= 0:
            return False

        now_time = datetime.now().time()
        stop_price = float(self.state.get("stop_price", 0) or 0)
        target_price = float(self.state.get("target_price", 0) or 0)

        # 15:20부터는 종가 단일가 구간이며, 여기서 시장가 매도를 접수해 종가 청산을 시도한다.
        if now_time >= self.CLOSE_AUCTION_EXIT_TIME:
            return self._send_market_sell("EOD_CLOSE_AUCTION")

        if stop_price > 0 and current_price <= stop_price:
            return self._send_market_sell("STOP_LOSS")

        if target_price > 0 and current_price >= target_price:
            return self._send_market_sell("TAKE_PROFIT_5PCT")

        return False

    def check_code(self, code):
        if str(code).zfill(6) != self.CODE:
            return False

        self._ensure_new_day()
        if not check_transaction_open():
            return False

        # 전 거래일에 청산되지 않은 ORB 보유분은 새 진입보다 먼저 시장가 청산한다.
        # 데이트레이딩 포지션을 다음 날까지 의도치 않게 유지하지 않기 위한 복구 안전장치다.
        owned_position = self._position_detail()
        if (
            owned_position
            and int(owned_position.get("quantity", 0) or 0) > 0
            and not self.state
        ):
            send_message(
                f"[ORB 긴급청산] 이전 거래일 포지션이 남아 있어 신규 진입 전 시장가 매도합니다: "
                f"{self.CODE_NAME}({self.CODE})"
            )
            return self._send_market_sell(
                "OVERNIGHT_POSITION_RECOVERY",
                int(owned_position.get("quantity", 0) or 0),
            )

        # 오늘 ORB가 보유 중이면 진입 판단보다 청산을 우선한다.
        if self.state and self.state.get("status") in ("BUY_REQUESTED", "OPEN", "SELL_REQUESTED"):
            return self._monitor_exit()

        return self._try_entry()
