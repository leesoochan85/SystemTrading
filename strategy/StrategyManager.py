import time
import traceback
import sqlite3
from datetime import datetime

from PyQt5.QtCore import QObject, QTimer

from util.time_helper import (
    check_transaction_open,
    check_transaction_closed,
    milliseconds_until_market_open,
)
from util.notifier import send_message, get_updates
from util.const import Telegram_chat_ID
from util.market_history import (
    build_realtime_daily_bars,
    get_db_path as get_market_history_db_path,
    save_realtime_daily_bars,
)
from util.db_helper import (
    POSITION_DB,
    init_position_strategy_table,
    init_sent_news_table,
    init_monitoring_tables,
    delete_position_strategy,
    get_position_strategy,
    get_strategy_position_counts,
    get_filled_position_codes,
    get_all_position_details,
    get_today_order_count,
    get_today_realized_pnl,
    save_daily_equity,
    save_strategy_daily_summaries,
)
from util.hankyung_report_helper import (
    scan_new_hankyung_reports,
)
from util.value_quality_data import (
    get_last_sent_value_report_at,
    get_last_scanned_hankyung_report_id,
    is_value_report_sent,
    save_last_scanned_hankyung_report_id,
    save_sent_value_report,
)


class StrategyManager(QObject):
    """
    신고가/눌림목/저평가 우량주 실시간 전략을 통합 관리한다.

    핵심 자금 정책
    -------------
    1. 전략별 예산 배분 없음
    2. 모든 전략이 하나의 계좌 공용 현금을 사용
    3. 신규 종목 1개당 총자산의 최대 10%까지 매수
    4. 계좌 전체 보유 + 신규매수 미체결 + Manager 예약 종목은 최대 10개
    5. SendOrder 직전에 Manager가 예산을 선예약하여 동시 주문의 중복 사용 방지
    """

    MAX_POSITION_RATIO = 0.10
    MAX_ACCOUNT_POSITIONS = 10
    DEFAULT_BUY_FEE_RATE = 0.00035

    def __init__(self, kiwoom, strategies):
        super().__init__()
        self.kiwoom = kiwoom
        self.strategies = strategies

        self.is_initialized = False
        self.is_running = False

        # 키움 OPW00001에서 조회한 100% 종목 주문가능금액.
        self.deposit = 0

        # 상태 표시/총자산 계산용.
        self.cash_deposit = 0
        self.general_orderable_cash = 0
        self.d2_estimated_deposit = 0
        self.total_assets = 0

        # Manager 내부 임시 예약.
        # 키움 주문가능금액이 다음 조회에 반영되기 전까지 중복 사용을 막는다.
        # {
        #   code: {
        #       "amount": int,
        #       "strategy_name": str,
        #       "quantity": int,
        #       "price": int,
        #       "reserved_at": float,
        #   }
        # }
        self.reserved_buy_orders = {}

        # 동일 code의 실시간 콜백이 짧은 간격으로 중첩되는 것을 막기 위한 예약.
        self.buy_request_in_progress = set()

        self.last_deposit_sync_at = 0
        self.deposit_sync_interval = 300  # 5분

        self.account_state_safe = False
        self.deposit_state_safe = False

        self.account_state_failure_reason = None
        self.last_account_failure_log_at = 0
        self.account_failure_log_interval = 30
        self.failed_account_retry_interval = 10

        self.last_deposit_failure_notice_at = 0
        self.deposit_failure_notice_interval = 60

        self.market_history_db_path = get_market_history_db_path()
        self.market_data_ready = False
        self.market_data_deferred_notice_sent = False

        self.timer = QTimer()
        self.timer.timeout.connect(self.step)

        self.market_open_interval = 1000
        self.market_closed_interval = 300000
        self.current_timer_interval = None

        self.last_order_sync_at = 0
        self.order_sync_interval = 60

        self.buy_order_cancel_after = 600
        self.pending_buy_cancellations = {}

        self.pending_sold_position_deletions = {}
        self.sold_position_delete_confirmations = 2

        self.last_daily_price_refresh_date = None
        self.is_refreshing_daily_price_data = False

        self.last_position_db_mismatch_signature = None

        self.last_value_report_check_at = 0
        self.value_report_check_interval = 1800  # 30분
        self.value_report_lookback_days = 30
        self.value_report_max_pages = 10
        self.last_value_report_error_log_at = 0
        self.value_report_error_log_interval = 3600

        self.telegram_update_offset = None
        self.command_timer = QTimer()
        self.command_timer.timeout.connect(self.poll_telegram_commands)
        self.command_check_interval = 10000

        # 전략은 매수 조건만 판단한다.
        # 실제 주문 수량/예산/계좌 전체 포지션 제한은 Manager가 담당한다.
        for strategy in self.strategies:
            setter = getattr(strategy, "set_buy_order_handler", None)
            if callable(setter):
                setter(self.request_buy_order)

            guard_setter = getattr(strategy, "set_order_guard", None)
            if callable(guard_setter):
                guard_setter(self.can_event_driven_order)

    # ------------------------------------------------------------------
    # 시작/타이머
    # ------------------------------------------------------------------

    def start(self):
        print("[StrategyManager] start 호출")
        self.is_running = True

        init_position_strategy_table()
        init_sent_news_table()
        init_monitoring_tables()

        self.initialize_strategies()
        self.send_one_value_quality_report_if_needed(force=True)

        if check_transaction_open():
            self.set_timer_interval(self.market_open_interval)
        else:
            self.set_timer_interval(self.market_closed_interval)
            wake_after_ms = milliseconds_until_market_open()
            if wake_after_ms is not None:
                QTimer.singleShot(wake_after_ms + 100, self.wake_at_market_open)
                print(
                    "[StrategyManager] 장 시작 타이머 예약: "
                    f"{(wake_after_ms + 100) / 1000:.1f}초 후 전략 검사 전환"
                )

        self.command_timer.start(self.command_check_interval)
        print("[StrategyManager] 타이머 시작")
        print("[StrategyManager] Telegram /status 명령대기")

    def set_timer_interval(self, interval_ms):
        if self.current_timer_interval == interval_ms:
            return

        self.timer.start(interval_ms)
        self.current_timer_interval = interval_ms

    def wake_at_market_open(self):
        if not self.is_running:
            return

        if check_transaction_open():
            self.set_timer_interval(self.market_open_interval)
            print("[StrategyManager] 장 시작 감지 - 1초 상태 검사로 전환")
            self.step()

    # ------------------------------------------------------------------
    # 중앙 자금 관리
    # ------------------------------------------------------------------

    def calculate_total_assets(self):
        """
        총자산 = D+2 추정예수금 + 현재 보유주식 평가금액.

        현재가가 없으면 잔고 현재가, 그것도 없으면 매입가를 마지막 대체값으로 사용한다.
        """
        evaluation_amount = 0

        for raw_code, info in self.kiwoom.balance.items():
            code = str(raw_code).strip().zfill(6)
            quantity = int(info.get("보유수량", 0) or 0)

            if quantity <= 0:
                continue

            rt = self.kiwoom.universe_realtime_transaction_info.get(code, {})
            current_price = int(
                rt.get("현재가")
                or info.get("현재가", 0)
                or info.get("매입가", 0)
                or info.get("매입단가", 0)
                or 0
            )

            evaluation_amount += quantity * max(0, current_price)

        return max(
            0,
            int(self.d2_estimated_deposit or 0) + evaluation_amount,
        )

    def get_reserved_buy_amount(self):
        """키움의 최신 예수금 조회에 아직 반영되지 않은 Manager 임시 예약금 합계."""
        return sum(
            max(0, int(info.get("amount", 0) or 0))
            for info in self.reserved_buy_orders.values()
        )

    def get_available_buy_cash(self):
        """
        현재 Manager가 신규 주문에 사용할 수 있다고 판단하는 현금.

        self.deposit은 마지막 정상 키움 조회값이고,
        그 이후 발생한 주문의 임시 예약금을 차감한다.
        """
        return max(
            0,
            int(self.deposit or 0) - self.get_reserved_buy_amount(),
        )

    def get_account_position_codes(self):
        """
        신규 포지션 슬롯을 사용 중인 종목을 반환한다.

        - 실제 보유
        - 매수 미체결
        - SendOrder 직전/직후 Manager 예약
        """
        codes = set()

        for raw_code, info in self.kiwoom.balance.items():
            quantity = int(info.get("보유수량", 0) or 0)
            if quantity > 0:
                codes.add(str(raw_code).strip().zfill(6))

        for raw_code, info in self.kiwoom.order.items():
            order_type = str(info.get("주문구분", "") or "").strip()
            remain = int(info.get("미체결수량", 0) or 0)

            if order_type == "매수" and remain > 0:
                codes.add(str(raw_code).strip().zfill(6))

        codes.update(
            str(code).strip().zfill(6)
            for code in self.reserved_buy_orders.keys()
        )
        return codes

    def get_account_position_count(self):
        return len(self.get_account_position_codes())

    def has_pending_buy(self, code):
        code = str(code).strip().zfill(6)

        if code in self.reserved_buy_orders:
            return True

        info = self.kiwoom.order.get(code, {})
        return (
            str(info.get("주문구분", "") or "").strip() == "매수"
            and int(info.get("미체결수량", 0) or 0) > 0
        )

    def request_buy_order(
        self,
        strategy_name,
        code,
        code_name,
        price,
        rqname,
        screen_no,
        buy_fee_rate=None,
    ):
        """
        모든 전략의 신규 매수 요청이 통과해야 하는 단일 관문.

        반환
        ----
        성공:
            {
                "quantity": ...,
                "price": ...,
                "estimated_amount": ...,
                "max_position_amount": ...,
                "total_assets": ...
            }

        거절/실패:
            None
        """
        code = str(code).strip().zfill(6)
        code_name = str(code_name or code)
        price = int(price or 0)

        if buy_fee_rate is None:
            buy_fee_rate = self.DEFAULT_BUY_FEE_RATE
        buy_fee_rate = max(0.0, float(buy_fee_rate))

        if price <= 0:
            return None

        if not self.can_event_driven_order():
            return None

        # 같은 종목에 대한 전략 간 동시 진입을 먼저 막는다.
        if code in self.buy_request_in_progress:
            return None

        self.buy_request_in_progress.add(code)

        try:
            balance_info = self.kiwoom.balance.get(code, {})
            if int(balance_info.get("보유수량", 0) or 0) > 0:
                return None

            if self.has_pending_buy(code):
                return None

            current_position_count = self.get_account_position_count()
            if current_position_count >= self.MAX_ACCOUNT_POSITIONS:
                print(
                    "[StrategyManager] 신규매수 거절 - "
                    f"계좌 최대 보유/대기 {self.MAX_ACCOUNT_POSITIONS}종목 도달 / "
                    f"{code_name}({code}) / {strategy_name}"
                )
                return None

            total_assets = self.calculate_total_assets()
            if total_assets <= 0:
                print(
                    "[StrategyManager] 신규매수 거절 - 총자산 계산값 0원 / "
                    f"{code_name}({code})"
                )
                return None

            max_position_amount = int(
                total_assets * self.MAX_POSITION_RATIO
            )
            available_cash = self.get_available_buy_cash()

            buy_budget = min(
                max_position_amount,
                available_cash,
            )

            if buy_budget <= 0:
                print(
                    "[StrategyManager] 신규매수 거절 - 사용가능 현금 없음 / "
                    f"{code_name}({code}) / {strategy_name}"
                )
                return None

            # 지정가 주문이므로 매수가 + 예상 매수수수료를 포함한 비용이
            # 종목당 10%와 현재 현금을 모두 넘지 않게 한다.
            unit_cost = price * (1.0 + buy_fee_rate)
            quantity = int(buy_budget // unit_cost)

            if quantity <= 0:
                print(
                    "[StrategyManager] 신규매수 거절 - 1주 매수 불가 / "
                    f"{code_name}({code}) / 가격 {price:,}원 / "
                    f"가용 {buy_budget:,}원"
                )
                return None

            estimated_amount = int(
                quantity * price * (1.0 + buy_fee_rate)
            )

            if estimated_amount <= 0:
                return None

            # SendOrder보다 먼저 예약한다.
            self.reserved_buy_orders[code] = {
                "amount": estimated_amount,
                "strategy_name": strategy_name,
                "quantity": quantity,
                "price": price,
                "reserved_at": time.time(),
            }

            result = self.kiwoom.send_order(
                rqname,
                screen_no,
                1,
                code,
                quantity,
                price,
                "00",
                strategy_name=strategy_name,
            )

            if result != 0:
                self.reserved_buy_orders.pop(code, None)

                print(
                    "[StrategyManager] 매수 주문 전송 실패 - "
                    f"{code_name}({code}) / {strategy_name} / result={result}"
                )
                return None

            # 키움 미체결 TR이 갱신되기 전에도 즉시 중복 주문을 막는다.
            self.kiwoom.order[code] = {
                "주문구분": "매수",
                "주문가격": price,
                "미체결수량": quantity,
                "strategy_name": strategy_name,
                "order_time": time.time(),
                "예산예약금액": estimated_amount,
            }

            print(
                "[StrategyManager] 매수 승인 - "
                f"{code_name}({code}) / {strategy_name} / "
                f"{quantity}주 × {price:,}원 / "
                f"예약 {estimated_amount:,}원 / "
                f"총자산 {total_assets:,}원 / "
                f"종목한도 {max_position_amount:,}원 / "
                f"남은 공용현금 {self.get_available_buy_cash():,}원"
            )

            return {
                "quantity": quantity,
                "price": price,
                "estimated_amount": estimated_amount,
                "max_position_amount": max_position_amount,
                "total_assets": total_assets,
            }

        except Exception:
            # 예외 발생 시 임시예약을 반드시 회수한다.
            self.reserved_buy_orders.pop(code, None)
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(
                f"[StrategyManager 매수요청 오류] "
                f"{code_name}({code}) / {strategy_name}\n{error_msg}"
            )
            return None

        finally:
            self.buy_request_in_progress.discard(code)

    def sync_deposit_from_kiwoom(self):
        """
        주문가능금액과 총자산 계산용 현금값을 갱신한다.

        키움의 새 주문가능금액 조회가 정상 완료되면,
        이전 조회 이후 Manager가 보유하던 임시 예약은 폐기한다.
        그 시점에는 이미 실제 주문/체결 상태가 키움 값에 반영된 것으로 본다.
        """
        queried_orderable_cash = self.kiwoom.get_deposit()

        if (
            not self.kiwoom.last_deposit_query_success
            or queried_orderable_cash is None
        ):
            self.deposit_state_safe = False

            now = time.time()
            if (
                now - self.last_deposit_failure_notice_at
                >= self.deposit_failure_notice_interval
            ):
                message = (
                    "[안전 중지] 주문가능금액 조회 실패 - "
                    "마지막 정상 금액을 유지하고 신규 주문을 중단합니다."
                )
                print(f"[StrategyManager] {message}")
                send_message(message)
                self.last_deposit_failure_notice_at = now

            return False

        self.deposit = max(0, int(queried_orderable_cash or 0))

        self.cash_deposit = int(
            self.kiwoom.last_cash_deposit
            if self.kiwoom.last_cash_deposit is not None
            else 0
        )

        self.general_orderable_cash = int(
            self.kiwoom.last_general_orderable_cash
            if self.kiwoom.last_general_orderable_cash is not None
            else self.deposit
        )

        self.d2_estimated_deposit = int(
            self.kiwoom.last_d2_estimated_deposit
            if self.kiwoom.last_d2_estimated_deposit is not None
            else self.cash_deposit
        )

        self.deposit_state_safe = True
        self.last_deposit_sync_at = time.time()

        # 새 키움 주문가능금액을 기준으로 다시 시작하므로 이전 임시예약은 제거.
        self.reserved_buy_orders.clear()

        self.total_assets = self.calculate_total_assets()

        print(
            "[StrategyManager] 자금 동기화 완료: "
            f"주문가능금액 {self.deposit:,}원 / "
            f"예수금 {self.cash_deposit:,}원 / "
            f"D+2추정예수금 {self.d2_estimated_deposit:,}원 / "
            f"총자산 {self.total_assets:,}원"
        )

        return True

    # ------------------------------------------------------------------
    # 전략 초기화 / 실시간 등록
    # ------------------------------------------------------------------

    def get_event_driven_strategies(self):
        return [
            strategy
            for strategy in self.strategies
            if getattr(strategy, "event_driven", False)
        ]

    def get_event_realtime_union_codes(self, strategies=None):
        strategies = strategies or self.get_event_driven_strategies()

        codes = set()
        for strategy in strategies:
            if not getattr(strategy, "is_init_success", False):
                continue

            getter = getattr(strategy, "get_realtime_candidate_codes", None)
            if callable(getter):
                codes.update(getter())

        return sorted(
            str(code).strip().upper().zfill(6)
            for code in codes
        )

    def get_event_realtime_union_fids(self, strategies=None):
        strategies = strategies or self.get_event_driven_strategies()
        fid_names = set()

        for strategy in strategies:
            if not getattr(strategy, "is_init_success", False):
                continue

            getter = getattr(strategy, "get_required_realtime_fids", None)
            if not callable(getter):
                raise RuntimeError(
                    f"{strategy.strategy_name}에 get_required_realtime_fids()가 없습니다."
                )

            required = set(getter() or [])
            if not required:
                raise RuntimeError(
                    f"{strategy.strategy_name}의 실시간 FID 목록이 비어 있습니다."
                )

            fid_names.update(required)

        return sorted(fid_names)

    def register_shared_event_realtime(
        self,
        strategies,
        force_refresh=False,
    ):
        active = [
            strategy
            for strategy in strategies
            if getattr(strategy, "is_init_success", False)
        ]

        if not active:
            return []

        union_codes = self.get_event_realtime_union_codes(active)
        if not union_codes:
            raise RuntimeError(
                "event-driven 전략의 실시간 등록 가능 종목이 없습니다."
            )

        union_fid_names = self.get_event_realtime_union_fids(active)

        fid_setter = getattr(
            self.kiwoom,
            "set_stock_realtime_fid_names",
            None,
        )
        if callable(fid_setter):
            fid_setter(union_fid_names)

        registrar = active[0]
        registrar.set_universe_real_time(
            register_market=True,
            codes_override=union_codes,
            force_refresh=force_refresh,
            fid_names_override=union_fid_names,
        )

        print(
            "[StrategyManager] event-driven 공통 실시간 등록: "
            f"{len(union_codes)}종목 / "
            f"{len(active)}전략 공유 / "
            f"FID {len(union_fid_names)}개 {union_fid_names}"
        )

        return union_codes

    def can_event_driven_order(self):
        return bool(
            self.is_running
            and self.is_initialized
            and self.account_state_safe
            and self.deposit_state_safe
            and self.market_data_ready
            and check_transaction_open()
        )

    def initialize_strategies(self):
        print("[StrategyManager] 전략 초기화 시작")

        try:
            print("[StrategyManager] 미체결 조회 시작")
            self.kiwoom.get_order()

            if not self.kiwoom.last_order_query_success:
                self.account_state_safe = False
                raise RuntimeError(
                    "초기 미체결 주문 조회 실패 - "
                    "중복 주문 방지를 위해 자동매매 시작을 중단합니다."
                )

            print("[StrategyManager] 미체결 조회 완료")

            print("[StrategyManager] 잔고 조회 시작")
            self.kiwoom.get_balance()

            if not self.kiwoom.last_balance_query_success:
                self.account_state_safe = False
                raise RuntimeError(
                    "초기 잔고 조회 실패 - "
                    "보유 종목 오인 방지를 위해 자동매매 시작을 중단합니다."
                )

            self.account_state_safe = True
            self.account_state_failure_reason = None
            self.last_order_sync_at = time.time()
            print("[StrategyManager] 잔고 조회 완료")

            self.check_balance_position_db_consistency()

            print("[StrategyManager] 예수금 조회 시작")
            if not self.sync_deposit_from_kiwoom():
                raise RuntimeError(
                    "초기 예수금 조회 실패 - "
                    "주문 가능 금액 오인 방지를 위해 자동매매 시작을 중단합니다."
                )
            print("[StrategyManager] 예수금 조회 완료")

            event_strategies = self.get_event_driven_strategies()
            init_success_count = 0

            # 주문 guard는 is_initialized=True가 된 뒤에만 주문을 통과시킨다.
            # 초기화 중에는 데이터 준비/리스너 연결만 한다.
            for strategy in event_strategies:
                try:
                    print(
                        f"[StrategyManager] {strategy.strategy_name} "
                        "전체시장 초기화 시작"
                    )

                    strategy.check_and_get_universe()
                    strategy.check_and_get_price_data()
                    strategy.set_universe_real_time(register_market=False)
                    strategy.is_init_success = True

                    init_success_count += 1
                    print(
                        f"[StrategyManager] {strategy.strategy_name} "
                        "기준값/listener 준비 완료"
                    )

                except Exception:
                    strategy.is_init_success = False
                    error_msg = traceback.format_exc()
                    print(error_msg)
                    send_message(
                        f"[{strategy.strategy_name} 초기화 실패]\n{error_msg}"
                    )

            ready = [
                strategy
                for strategy in event_strategies
                if strategy.is_init_success
            ]

            if not ready:
                raise RuntimeError(
                    "실행 가능한 event-driven 전략이 하나도 없습니다."
                )

            # 모든 전략의 지표 준비가 끝난 뒤 후보 합집합을 실시간 등록.
            self.register_shared_event_realtime(ready)

            self.market_data_ready = True
            self.market_data_deferred_notice_sent = False
            self.is_initialized = True

            for strategy in ready:
                send_message(
                    f"[{strategy.strategy_name}] 전체시장 실시간 초기화 완료"
                )

            self.save_performance_snapshot()

            print(
                "[StrategyManager] 전략 초기화 완료: "
                f"{init_success_count}/{len(event_strategies)}전략"
            )

        except Exception:
            self.is_initialized = False
            self.market_data_ready = False
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    # ------------------------------------------------------------------
    # 계좌/미체결 동기화
    # ------------------------------------------------------------------

    def refresh_order_and_balance_state(self, allow_buy_cancel=True):
        self.kiwoom.get_order()

        if not self.kiwoom.last_order_query_success:
            now = time.time()
            first_failure = (
                self.account_state_safe
                or self.account_state_failure_reason != "미체결 주문 조회 실패"
            )

            self.account_state_safe = False
            self.account_state_failure_reason = "미체결 주문 조회 실패"
            self.last_order_sync_at = now - (
                self.order_sync_interval - self.failed_account_retry_interval
            )

            message = (
                "[안전 중지] 미체결 주문 조회 실패 - "
                "기존 주문 정보를 유지하고 신규 전략 주문을 중단합니다."
            )

            print(f"[StrategyManager] {message}")
            if first_failure:
                send_message(message)

            return False

        self.kiwoom.get_balance()
        self.last_order_sync_at = time.time()

        if not self.kiwoom.last_balance_query_success:
            now = time.time()
            first_failure = (
                self.account_state_safe
                or self.account_state_failure_reason != "잔고 조회 실패"
            )

            self.account_state_safe = False
            self.account_state_failure_reason = "잔고 조회 실패"
            self.last_order_sync_at = now - (
                self.order_sync_interval - self.failed_account_retry_interval
            )

            message = (
                "[안전 중지] 잔고 조회 실패 - "
                "기존 잔고를 유지하고 신규 전략 주문을 중단합니다."
            )

            print(f"[StrategyManager] {message}")
            if first_failure:
                send_message(message)

            return False

        was_unsafe = not self.account_state_safe
        previous_reason = self.account_state_failure_reason

        self.account_state_safe = True
        self.account_state_failure_reason = None
        self.last_account_failure_log_at = 0

        if was_unsafe:
            message = (
                "[안전 중지 해제] 미체결 주문 및 잔고 조회 정상 완료 - "
                "전략 주문을 재개합니다."
            )
            if previous_reason:
                message += f" / 직전 원인: {previous_reason}"

            print(f"[StrategyManager] {message}")
            send_message(message)

        # 키움의 최신 미체결 목록에 이미 들어간 종목은 Manager 임시예약의
        # 역할이 끝났다. 다만 예수금을 새로 조회하기 전에는 중복 차감을 피하기 위해
        # 여기서 모두 지우지 않고 sync_deposit_from_kiwoom()에서 일괄 초기화한다.
        if allow_buy_cancel:
            self.cancel_stale_buy_orders()

        # HTS/MTS 수동 부분매도 체결 이벤트를 놓친 경우에도
        # 실제 키움 잔고를 기준으로 strategy_position.db 수량을 보정한다.
        self.sync_position_quantities_from_balance()

        self.cleanup_cancelled_buy_positions()
        self.cleanup_sold_positions()
        self.check_balance_position_db_consistency()

        if self.deposit_state_safe:
            self.save_performance_snapshot()

        return True

    def cancel_stale_buy_orders(self):
        now = time.time()

        for raw_code, order_info in list(self.kiwoom.order.items()):
            code = str(raw_code).strip().zfill(6)

            try:
                order_type = str(
                    order_info.get("주문구분", "") or ""
                ).strip()
                remain_qty = int(
                    order_info.get("미체결수량", 0) or 0
                )
                strategy_name = str(
                    order_info.get("strategy_name", "") or ""
                )
                order_time = float(
                    order_info.get("order_time", 0) or 0
                )
                order_no = (
                    order_info.get("주문번호")
                    or order_info.get("order_no")
                    or ""
                )

                if order_type != "매수" or remain_qty <= 0:
                    continue

                if code in self.pending_buy_cancellations:
                    continue

                if not order_time:
                    order_info["order_time"] = now
                    continue

                if now - order_time < self.buy_order_cancel_after:
                    continue

                if not order_no:
                    print(
                        "[StrategyManager] 매수취소 불가 - "
                        f"주문번호 없음: {code}"
                    )
                    continue

                result = self.kiwoom.send_order(
                    "send_buy_cancel_order",
                    "3001",
                    3,
                    code,
                    remain_qty,
                    0,
                    "00",
                    order_no,
                    strategy_name=strategy_name,
                )

                if result == 0:
                    self.pending_buy_cancellations[code] = {
                        "strategy_name": strategy_name,
                        "requested_at": now,
                        "remaining_quantity": remain_qty,
                        "missing_confirmations": 0,
                    }

                    send_message(
                        f"[매수 미체결 취소요청] "
                        f"{self.get_code_name(code)}({code}) "
                        f"잔량 {remain_qty}주 / 전략: {strategy_name}"
                    )
                else:
                    send_message(
                        f"[매수 미체결 자동취소 실패] "
                        f"{self.get_code_name(code)}({code}) "
                        f"result={result}"
                    )

            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(error_msg)

    def cleanup_cancelled_buy_positions(self):
        for code, cancel_info in list(
            self.pending_buy_cancellations.items()
        ):
            try:
                current_order = self.kiwoom.order.get(code, {})
                remain = int(
                    current_order.get("미체결수량", 0) or 0
                )

                # 취소 후에도 미체결이 남아 있으면 아직 처리 중.
                if remain > 0:
                    cancel_info["missing_confirmations"] = 0
                    continue

                balance = self.kiwoom.balance.get(code, {})
                quantity = int(
                    balance.get("보유수량", 0) or 0
                )

                # 일부 체결 후 잔량만 취소된 경우 포지션 유지.
                if quantity > 0:
                    self.pending_buy_cancellations.pop(code, None)
                    self.reserved_buy_orders.pop(code, None)

                    send_message(
                        f"[일부 체결 후 잔량 취소 완료] "
                        f"{self.get_code_name(code)}({code}) "
                        f"보유수량 {quantity}주 / 포지션 유지"
                    )
                    continue

                cancel_info["missing_confirmations"] = (
                    int(cancel_info.get("missing_confirmations", 0) or 0)
                    + 1
                )

                if cancel_info["missing_confirmations"] < 2:
                    continue

                delete_position_strategy(code)
                self.pending_buy_cancellations.pop(code, None)
                self.reserved_buy_orders.pop(code, None)

                send_message(
                    f"[전량 미체결 매수취소 완료] "
                    f"{self.get_code_name(code)}({code}) "
                    "포지션 DB 삭제"
                )

            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(error_msg)

    def sync_position_quantities_from_balance(self):
        """
        실제 키움 잔고를 strategy_position.db의 최종 기준으로 사용한다.

        목적
        ----
        - HTS/MTS 수동 부분매도
        - 체결 이벤트 일시 누락
        - 프로그램 재접속

        위 상황에서도 보유수량이 실제 잔고와 다르면 DB의 quantity만 보정한다.
        매입가/created_at/전략명은 건드리지 않는다.
        전량매도(실제 0주)는 cleanup_sold_positions()의 2회 확인 후 삭제한다.
        """
        if not self.account_state_safe:
            return

        db_positions = get_all_position_details()

        corrections = []

        with sqlite3.connect(POSITION_DB) as con:
            for raw_code, balance_info in self.kiwoom.balance.items():
                code = str(raw_code).strip().zfill(6)

                actual_quantity = int(
                    balance_info.get("보유수량", 0)
                    or 0
                )

                if actual_quantity <= 0:
                    continue

                db_position = db_positions.get(code)
                if db_position is None:
                    continue

                db_quantity = int(
                    db_position.get("quantity", 0)
                    or 0
                )

                # 매수 직후 예약 row(0주)는 체결 이벤트에서 매입가와 함께
                # 정상 반영되어야 하므로 여기서 임의로 올리지 않는다.
                if db_quantity <= 0:
                    continue

                if db_quantity == actual_quantity:
                    continue

                con.execute("""
                    UPDATE position_strategy
                    SET quantity = ?
                    WHERE code = ?
                """, (
                    actual_quantity,
                    code,
                ))

                corrections.append(
                    (
                        code,
                        db_position.get("code_name")
                        or self.get_code_name(code),
                        db_quantity,
                        actual_quantity,
                    )
                )

        for code, code_name, before, after in corrections:
            message = (
                "[수동매도/잔고 DB 자동보정] "
                f"{code_name}({code}) "
                f"전략DB {before}주 -> 실제잔고 {after}주"
            )
            print(f"[StrategyManager] {message}")
            send_message(message)

    def notify_strategy_position_closed(
        self,
        code,
        strategy_name,
        reason="",
    ):
        if not strategy_name:
            return

        for strategy in self.strategies:
            if (
                getattr(strategy, "strategy_name", None)
                != strategy_name
            ):
                continue

            callback = getattr(
                strategy,
                "on_position_closed",
                None,
            )

            if not callable(callback):
                return

            try:
                callback(
                    code,
                    reason=reason,
                )
            except TypeError:
                callback(code)
            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(
                    f"[전략 포지션 종료 후처리 실패] "
                    f"{strategy_name} / {code}\n"
                    f"{error_msg}"
                )
            return

    def cleanup_sold_positions(self):
        tracked_codes = set(get_filled_position_codes())

        for code in list(self.pending_sold_position_deletions.keys()):
            if code not in tracked_codes:
                self.pending_sold_position_deletions.pop(code, None)

        for raw_code in tracked_codes:
            code = str(raw_code).strip().zfill(6)

            try:
                balance = self.kiwoom.balance.get(code, {})
                quantity = int(
                    balance.get("보유수량", 0) or 0
                )

                if quantity > 0:
                    self.pending_sold_position_deletions.pop(code, None)
                    continue

                order_info = self.kiwoom.order.get(code, {})
                order_type = str(
                    order_info.get("주문구분", "") or ""
                ).strip()
                remain = int(
                    order_info.get("미체결수량", 0) or 0
                )

                if order_type == "매도" and remain > 0:
                    self.pending_sold_position_deletions.pop(code, None)
                    continue

                count = (
                    self.pending_sold_position_deletions.get(code, 0)
                    + 1
                )
                self.pending_sold_position_deletions[code] = count

                if count < self.sold_position_delete_confirmations:
                    continue

                strategy_name = get_position_strategy(code)

                delete_position_strategy(code)
                self.kiwoom.order.pop(code, None)
                self.reserved_buy_orders.pop(code, None)
                self.pending_sold_position_deletions.pop(code, None)

                self.notify_strategy_position_closed(
                    code,
                    strategy_name,
                    reason="실제 잔고 0 확인",
                )

                send_message(
                    f"[전량 매도 완료] "
                    f"{self.get_code_name(code)}({code}) "
                    f"포지션 DB 삭제 / 전략: "
                    f"{strategy_name or '확인불가'}"
                )

            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(error_msg)

    # ------------------------------------------------------------------
    # 일봉 갱신
    # ------------------------------------------------------------------

    def get_all_realtime_target_codes(self):
        codes = set(
            str(code).strip().upper().zfill(6)
            for code in self.kiwoom.balance.keys()
        )

        for strategy in self.get_event_driven_strategies():
            getter = getattr(
                strategy,
                "get_realtime_candidate_codes",
                None,
            )
            if callable(getter):
                codes.update(getter())

        return sorted(codes)

    def save_today_realtime_bars_to_shared_db(self, codes=None):
        target_codes = (
            self.get_all_realtime_target_codes()
            if codes is None
            else sorted(
                set(
                    str(code).strip().upper().zfill(6)
                    for code in codes
                )
            )
        )

        bars = build_realtime_daily_bars(
            self.kiwoom.universe_realtime_transaction_info,
            target_codes,
        )

        saved_count = save_realtime_daily_bars(
            bars,
            trade_date=datetime.now().strftime("%Y%m%d"),
            db_path=self.market_history_db_path,
        )

        print(
            "[StrategyManager] Kiwoom 당일 일봉 저장 완료: "
            f"{saved_count}/{len(target_codes)}종목 -> "
            f"{self.market_history_db_path}"
        )

        return saved_count

    def refresh_daily_price_data_after_close(self):
        if not check_transaction_closed():
            return False

        today = datetime.now().strftime("%Y%m%d")

        if self.last_daily_price_refresh_date == today:
            return False

        if self.is_refreshing_daily_price_data:
            return False

        self.is_refreshing_daily_price_data = True

        try:
            active = [
                strategy
                for strategy in self.get_event_driven_strategies()
                if strategy.is_init_success
            ]

            if not active:
                return False

            print(
                f"[StrategyManager] 장 종료 일봉 갱신 시작: {today}"
            )

            self.save_today_realtime_bars_to_shared_db()

            success = []
            failed = []

            for strategy in active:
                try:
                    strategy.check_and_get_universe()
                    strategy.check_and_get_price_data()
                    strategy.set_universe_real_time(
                        register_market=False
                    )
                    success.append(strategy.strategy_name)

                except Exception:
                    failed.append(strategy.strategy_name)
                    error_msg = traceback.format_exc()
                    print(error_msg)
                    send_message(
                        f"[유니버스/일봉 갱신 실패] "
                        f"{strategy.strategy_name}\n{error_msg}"
                    )

            if failed:
                return False

            self.register_shared_event_realtime(
                active,
                force_refresh=True,
            )

            self.market_data_ready = True
            self.last_daily_price_refresh_date = today

            send_message(
                f"[장 종료 갱신 완료] {today} / "
                f"{', '.join(success)}"
            )

            return True

        finally:
            self.is_refreshing_daily_price_data = False

    # ------------------------------------------------------------------
    # 상태/DB 일관성/성과
    # ------------------------------------------------------------------

    def get_code_name(self, code):
        code = str(code).strip().zfill(6)

        balance = self.kiwoom.balance.get(code, {})
        if balance.get("종목명"):
            return balance["종목명"]

        for strategy in self.strategies:
            if code in getattr(strategy, "universe", {}):
                return (
                    strategy.universe[code].get("code_name")
                    or code
                )

        try:
            return self.kiwoom.get_master_code_name(code) or code
        except Exception:
            return code

    def check_balance_position_db_consistency(self):
        if not self.account_state_safe:
            return False

        if not self.kiwoom.last_balance_query_success:
            return False

        actual_balance = {}

        for raw_code, info in self.kiwoom.balance.items():
            code = str(raw_code).strip().zfill(6)
            quantity = int(info.get("보유수량", 0) or 0)

            if quantity <= 0:
                continue

            actual_balance[code] = {
                "code_name": info.get("종목명")
                or self.get_code_name(code),
                "quantity": quantity,
                "buy_price": int(
                    info.get("매입가", 0)
                    or info.get("매입단가", 0)
                    or 0
                ),
            }

        db_positions = get_all_position_details()
        issues = []

        for code, actual in sorted(actual_balance.items()):
            db_position = db_positions.get(code)

            if db_position is None:
                issues.append(
                    f"- [미관리 보유] "
                    f"{actual['code_name']}({code}) "
                    f"실제 {actual['quantity']}주 / 전략 DB 기록 없음"
                )
                continue

            strategy_name = db_position.get(
                "strategy_name",
                "-",
            )
            db_quantity = int(
                db_position.get("quantity", 0) or 0
            )
            db_buy_price = float(
                db_position.get("buy_price", 0) or 0
            )

            if db_quantity != actual["quantity"]:
                issues.append(
                    f"- [수량 불일치] "
                    f"{actual['code_name']}({code}) "
                    f"실제 {actual['quantity']}주 / "
                    f"DB {db_quantity}주 / "
                    f"{strategy_name}"
                )

            if db_buy_price <= 0:
                issues.append(
                    f"- [매입가 누락] "
                    f"{actual['code_name']}({code}) "
                    f"실제 보유 중 / DB 매입가 없음 / "
                    f"{strategy_name}"
                )

        for code, db_position in sorted(db_positions.items()):
            db_quantity = int(
                db_position.get("quantity", 0) or 0
            )
            db_buy_price = float(
                db_position.get("buy_price", 0) or 0
            )

            # 매수 주문 직후 아직 체결되지 않은 예약 row는 제외.
            if db_quantity <= 0 and db_buy_price <= 0:
                continue

            if code in actual_balance:
                continue

            if code in self.pending_sold_position_deletions:
                continue

            order_info = self.kiwoom.order.get(code, {})
            order_type = str(
                order_info.get("주문구분", "") or ""
            ).strip()
            remain = int(
                order_info.get("미체결수량", 0) or 0
            )

            if order_type == "매도" and remain > 0:
                continue

            issues.append(
                f"- [잔고 없음/DB 잔존] "
                f"{db_position.get('code_name') or self.get_code_name(code)}"
                f"({code}) DB {db_quantity}주 / "
                f"{db_position.get('strategy_name', '-')}"
            )

        signature = tuple(sorted(issues))

        if signature == self.last_position_db_mismatch_signature:
            return bool(issues)

        previous_signature = self.last_position_db_mismatch_signature
        self.last_position_db_mismatch_signature = signature

        if issues:
            message = (
                "[경고] 실제 잔고와 전략 DB 불일치 발견\n\n"
                + "\n".join(issues)
                + "\n\n자동 수정하지 않았습니다. 실제 잔고와 전략 기록을 확인하세요."
            )
            print(f"[StrategyManager] {message}")
            send_message(message)
            return True

        if previous_signature:
            send_message(
                "[정상화] 실제 잔고와 전략 DB 불일치가 해소되었습니다."
            )

        return False

    def save_performance_snapshot(self):
        evaluation_amount, total_assets = save_daily_equity(
            deposit=self.d2_estimated_deposit,
            balance=self.kiwoom.balance,
        )

        save_strategy_daily_summaries(
            [strategy.strategy_name for strategy in self.strategies]
        )

        self.total_assets = int(total_assets or 0)

        print(
            "[StrategyManager] 실전 성과 저장: "
            f"D+2추정예수금 {self.d2_estimated_deposit:,}원 / "
            f"주문가능금액 {self.deposit:,}원 / "
            f"평가금액 {evaluation_amount:,}원 / "
            f"총자산 {total_assets:,}원"
        )

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    def poll_telegram_commands(self):
        try:
            updates = get_updates(
                offset=self.telegram_update_offset
            )

            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    self.telegram_update_offset = update_id + 1

                message = update.get("message", {})
                chat = message.get("chat", {})
                incoming_chat_id = str(chat.get("id", ""))
                text = str(
                    message.get("text", "") or ""
                ).strip()

                if incoming_chat_id != str(Telegram_chat_ID):
                    continue

                command = text.split("@")[0].lower()

                if command == "/status":
                    send_message(
                        self.build_status_message(),
                        chat_id=incoming_chat_id,
                    )

                elif command == "/help":
                    send_message(
                        "[사용 가능한 명령]\n"
                        "/status - 자동매매 현재 상태 확인",
                        chat_id=incoming_chat_id,
                    )

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)

    def build_status_message(self):
        now_text = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        if self.last_order_sync_at:
            balance_sync_text = datetime.fromtimestamp(
                self.last_order_sync_at
            ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            balance_sync_text = "아직 동기화되지 않음"

        total_assets = self.calculate_total_assets()
        reserved = self.get_reserved_buy_amount()
        available_cash = self.get_available_buy_cash()
        max_position_amount = int(
            total_assets * self.MAX_POSITION_RATIO
        )

        lines = [
            "[자동매매 상태]",
            f"조회 시각: {now_text}",
            f"잔고/미체결 기준: {balance_sync_text}",
            f"총자산: {total_assets:,}원",
            f"예수금: {self.cash_deposit:,}원",
            f"D+2 추정예수금: {self.d2_estimated_deposit:,}원",
            f"키움 주문가능금액: {self.deposit:,}원",
            f"Manager 매수 예약금: {reserved:,}원",
            f"실제 신규매수 가용현금: {available_cash:,}원",
            (
                f"종목당 최대 비중: "
                f"{self.MAX_POSITION_RATIO * 100:.0f}% "
                f"({max_position_amount:,}원)"
            ),
            (
                f"계좌 보유/매수대기: "
                f"{self.get_account_position_count()}/"
                f"{self.MAX_ACCOUNT_POSITIONS}종목"
            ),
            "",
        ]

        self._append_balance_status(lines)
        self._append_strategy_status(lines)
        self._append_pending_order_status(lines)
        self._append_today_summary(lines)

        return "\n".join(lines)

    def _append_balance_status(self, lines):
        holdings = []

        for code, info in self.kiwoom.balance.items():
            quantity = int(info.get("보유수량", 0) or 0)
            if quantity > 0:
                holdings.append((code, info))

        lines.append(
            f"[현재 보유 종목] {len(holdings)}개"
        )

        if not holdings:
            lines.append("- 없음")
            lines.append("")
            return

        for code, info in holdings:
            code = str(code).strip().zfill(6)
            code_name = (
                info.get("종목명")
                or self.get_code_name(code)
            )
            quantity = int(
                info.get("보유수량", 0) or 0
            )
            buy_price = int(
                info.get("매입가", 0)
                or info.get("매입단가", 0)
                or 0
            )

            rt = self.kiwoom.universe_realtime_transaction_info.get(
                code,
                {},
            )
            current_price = int(
                rt.get("현재가")
                or info.get("현재가", 0)
                or 0
            )

            if buy_price > 0 and current_price > 0:
                return_rate = (
                    (current_price - buy_price)
                    / buy_price
                    * 100
                )
            else:
                return_rate = float(
                    info.get("수익률", 0) or 0
                )

            lines.append(
                f"- {code_name}({code}) {quantity}주 | "
                f"매입 {buy_price:,} / 현재 {current_price:,} | "
                f"{return_rate:+.2f}%"
            )

        lines.append("")

    def _append_strategy_status(self, lines):
        counts = get_strategy_position_counts()

        lines.append("[전략별 보유 현황]")
        for strategy in self.strategies:
            count = counts.get(
                strategy.strategy_name,
                0,
            )
            lines.append(
                f"- {strategy.strategy_name}: {count}개"
            )
        lines.append("")

    def _append_pending_order_status(self, lines):
        pending = []

        for code, info in self.kiwoom.order.items():
            remain = int(
                info.get("미체결수량", 0) or 0
            )
            if remain > 0:
                pending.append(
                    (str(code).zfill(6), info)
                )

        lines.append(f"[미체결 주문] {len(pending)}건")

        if not pending:
            lines.append("- 없음")
            lines.append("")
            return

        for code, info in pending:
            code_name = (
                info.get("종목명")
                or self.get_code_name(code)
            )
            order_type = info.get("주문구분", "-")
            remain = int(
                info.get("미체결수량", 0) or 0
            )
            order_price = int(
                info.get("주문가격", 0) or 0
            )
            elapsed = self._get_order_elapsed_text(info)

            lines.append(
                f"- {code_name}({code}) {order_type} "
                f"{remain}주 {order_price:,}원 | "
                f"경과 {elapsed}"
            )

        lines.append("")

    def _get_order_elapsed_text(self, order_info):
        order_time = order_info.get("order_time")

        if order_time:
            try:
                elapsed_seconds = max(
                    0,
                    int(
                        time.time()
                        - float(order_time)
                    ),
                )
                minutes, seconds = divmod(
                    elapsed_seconds,
                    60,
                )
                return f"{minutes}분 {seconds}초"
            except Exception:
                pass

        broker_time = str(
            order_info.get("시간", "") or ""
        ).strip()

        if len(broker_time) == 6 and broker_time.isdigit():
            try:
                today = datetime.now().strftime(
                    "%Y%m%d"
                )
                ordered_at = datetime.strptime(
                    today + broker_time,
                    "%Y%m%d%H%M%S",
                )
                elapsed_seconds = max(
                    0,
                    int(
                        (
                            datetime.now()
                            - ordered_at
                        ).total_seconds()
                    ),
                )
                minutes, seconds = divmod(
                    elapsed_seconds,
                    60,
                )
                return f"{minutes}분 {seconds}초"

            except ValueError:
                pass

        return "확인 불가"

    def _append_today_summary(self, lines):
        order_count = get_today_order_count()

        (
            gross_realized_pnl,
            net_realized_pnl,
            missing_buy_price_count,
            missing_cost_count,
        ) = get_today_realized_pnl()

        last_report_sent_at = (
            get_last_sent_value_report_at()
        )

        if last_report_sent_at:
            last_report_text = (
                f"{last_report_sent_at[0:4]}-"
                f"{last_report_sent_at[4:6]}-"
                f"{last_report_sent_at[6:8]} "
                f"{last_report_sent_at[8:10]}:"
                f"{last_report_sent_at[10:12]}:"
                f"{last_report_sent_at[12:14]}"
            )
        else:
            last_report_text = "전송 기록 없음"

        lines.append("[오늘 요약]")
        lines.append(
            "- 매도 체결 총손익(수수료·세금 전): "
            f"{gross_realized_pnl:+,}원"
        )
        lines.append(
            "- 매도 체결 순손익(비용 확인 거래 합산): "
            f"{net_realized_pnl:+,}원"
        )
        lines.append(f"- 주문 횟수: {order_count}건")
        lines.append(
            "- 마지막 저평가 리포트 전송: "
            f"{last_report_text}"
        )

        if missing_buy_price_count > 0:
            lines.append(
                "- 총손익 계산 제외 매도 거래: "
                f"{missing_buy_price_count}건 "
                "(매입가 기록 없음)"
            )

        if missing_cost_count > 0:
            lines.append(
                "- 순손익 계산 보류 거래: "
                f"{missing_cost_count}건 "
                "(수수료·세금 수신 대기 또는 미확인)"
            )

    def get_value_quality_holding_codes(self):
        codes = []

        for raw_code, balance_info in self.kiwoom.balance.items():
            code = str(raw_code).strip().zfill(6)
            quantity = int(
                balance_info.get("보유수량", 0)
                or 0
            )

            if quantity <= 0:
                continue

            if (
                get_position_strategy(code)
                != "ValueQualityStrategy"
            ):
                continue

            codes.append(code)

        return sorted(set(codes))

    def send_one_value_quality_report_if_needed(
        self,
        force=False,
    ):
        """
        기존 무관한 경제뉴스 대신,
        현재 ValueQualityStrategy로 실제 보유 중인 종목의
        한경 컨센서스 기업 리포트만 Telegram으로 전송한다.

        - 30분마다 확인
        - 마지막 확인 리포트 ID 이후의 공개 상세페이지만 검사
        - 한 번에 최대 1건 전송
        - value_quality.db에서 중복 전송 방지
        """
        now = time.time()

        if (
            not force
            and now - self.last_value_report_check_at
            < self.value_report_check_interval
        ):
            return

        self.last_value_report_check_at = now

        holding_codes = (
            self.get_value_quality_holding_codes()
        )

        if not holding_codes:
            return

        try:
            last_scanned_report_id = (
                get_last_scanned_hankyung_report_id()
            )

            scan_result = scan_new_hankyung_reports(
                target_codes=holding_codes,
                last_scanned_report_id=last_scanned_report_id,
                max_scan_count=120,
                consecutive_miss_limit=15,
            )

            reports = scan_result["reports"]
            new_last_scanned_id = int(
                scan_result["last_scanned_report_id"]
            )

            # 새 리포트 상세페이지가 확인된 범위까지만 high-water mark 전진.
            # 전송할 보유종목 리포트가 없어도 다음 검사에서 같은 전체 시장
            # 리포트를 반복 조회하지 않는다.
            if (
                last_scanned_report_id is None
                or new_last_scanned_id
                > int(last_scanned_report_id)
            ):
                save_last_scanned_hankyung_report_id(
                    new_last_scanned_id
                )

            target = None

            # 오래된 것부터 처리해야 새 리포트가 여러 개 있을 때
            # 30분마다 순서대로 하나씩 전달된다.
            for report in reports:
                report_key = report.get(
                    "report_key"
                )
                code = str(
                    report.get("code", "")
                ).zfill(6)

                if (
                    not report_key
                    or code not in holding_codes
                ):
                    continue

                if is_value_report_sent(
                    report_key
                ):
                    continue

                target = report
                break

            if target is None:
                return

            code = str(
                target.get("code", "")
            ).zfill(6)

            title = (
                target.get("title")
                or "기업 리포트"
            )
            broker = (
                target.get("broker")
                or "-"
            )
            analyst = (
                target.get("analyst")
                or "-"
            )
            report_date = (
                target.get("report_date")
                or "-"
            )
            opinion = (
                target.get("opinion")
                or "-"
            )
            target_price = (
                target.get("target_price")
            )
            url = target["url"]

            code_name = self.get_code_name(code)

            if target_price not in (
                None,
                "",
                0,
                "0",
            ):
                target_price_text = str(
                    target_price
                )
            else:
                target_price_text = "-"

            message = (
                "[저평가 우량주 리포트]\n\n"
                f"{code_name}({code})\n"
                f"{title}\n"
                f"발행기관: {broker}\n"
                f"작성자: {analyst}\n"
                f"발표일: {report_date}\n"
                f"투자의견: {opinion}\n"
                f"목표주가: {target_price_text}\n\n"
                f"{url}"
            )

            result = send_message(message)

            if result is not None:
                save_sent_value_report(
                    report_key=target[
                        "report_key"
                    ],
                    code=code,
                    title=title,
                    broker=broker,
                    report_date=report_date,
                    url=url,
                )

                print(
                    "[StrategyManager] "
                    "저평가 우량주 리포트 전송 완료: "
                    f"{code_name} / {title}"
                )

        except Exception as exc:
            # 사이트/API 변경이나 일시적인 네트워크 오류가
            # 자동매매 자체를 멈추게 해서는 안 된다.
            if (
                now - self.last_value_report_error_log_at
                >= self.value_report_error_log_interval
            ):
                print(
                    "[StrategyManager] "
                    "한경 컨센서스 리포트 조회 실패 - "
                    "매매는 계속 진행합니다: "
                    f"{exc}"
                )
                self.last_value_report_error_log_at = now

    # ------------------------------------------------------------------
    # 주기 실행
    # ------------------------------------------------------------------

    def step(self):
        if not self.is_running or not self.is_initialized:
            return

        try:
            now = time.time()

            self.send_one_value_quality_report_if_needed()

            # 장외에는 주문하지 않고 상태/장마감 데이터만 정리한다.
            if not check_transaction_open():
                self.set_timer_interval(
                    self.market_closed_interval
                )

                self.refresh_order_and_balance_state(
                    allow_buy_cancel=False
                )

                if (
                    self.kiwoom.last_balance_query_success
                    and self.sync_deposit_from_kiwoom()
                ):
                    self.save_performance_snapshot()

                self.refresh_daily_price_data_after_close()

                print(
                    "[StrategyManager] 장 시간이 아니므로 "
                    "계좌 상태만 확인하고 대기합니다."
                )
                return

            self.set_timer_interval(
                self.market_open_interval
            )

            # 60초마다 키움 미체결/잔고를 실제 상태와 동기화.
            if (
                now - self.last_order_sync_at
                >= self.order_sync_interval
            ):
                if not self.refresh_order_and_balance_state(
                    allow_buy_cancel=True
                ):
                    return

            if not self.account_state_safe:
                if (
                    now - self.last_account_failure_log_at
                    >= self.account_failure_log_interval
                ):
                    reason = (
                        self.account_state_failure_reason
                        or "계좌 상태 조회 실패"
                    )
                    print(
                        f"[StrategyManager] {reason} - "
                        "신규 주문 중단"
                    )
                    self.last_account_failure_log_at = now
                return

            # 5분마다 실제 주문가능금액을 재조회한다.
            if (
                not self.deposit_state_safe
                or now - self.last_deposit_sync_at
                >= self.deposit_sync_interval
            ):
                if not self.sync_deposit_from_kiwoom():
                    return

                self.save_performance_snapshot()

            # 전략의 신규 매수/매도 판단은 Kiwoom 실시간 이벤트에서 실행된다.
            # Manager의 1초 timer는 계좌/예산/뉴스/장마감 상태 관리에만 사용한다.

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)
