import time
import traceback
from datetime import datetime

from PyQt5.QtCore import QObject, QTimer, QCoreApplication

from util.time_helper import (
    check_transaction_open,
    check_transaction_closed,
    milliseconds_until_market_open,
)
from util.notifier import send_error, send_message, get_updates
from util.const import Telegram_chat_ID
from util.telegram_poller import TelegramCommandPoller
from util.status_message import StatusMessageBuilder
from strategy.order_coordinator import OrderCoordinator
from strategy.account_state import AccountStateSynchronizer
from util.market_history import (
    build_realtime_daily_bars,
    get_db_path as get_market_history_db_path,
    save_realtime_daily_bars,
)
from util.db_helper import (
    POSITION_DB,
    init_position_strategy_table,
    init_monitoring_tables,
    get_position_strategy,
    save_daily_equity,
    save_strategy_daily_summaries,
    save_position_snapshot,
    save_strategy_equity_snapshots,
)
from util.hankyung_report_helper import (
    scan_new_hankyung_reports,
)
from util.virtual_strategy_engine import VirtualStrategyEngine
from util.const import get_fid
from util.virtual_trading import (
    init_virtual_trading_tables,
    save_virtual_strategy_daily_snapshots,
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
    우량주/모멘텀 운영 전략과 A/B/C 가상 비교를 관리한다.

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

        # 웹 전략 신호에서 "조건은 충족했지만 왜 주문되지 않았는지"를
        # 확인할 수 있도록 종목별 마지막 신규매수 거절 사유를 보존한다.
        self.last_buy_rejections = {}

        self.last_deposit_sync_at = 0
        self.deposit_sync_interval = 300  # 5분

        self.account_state_safe = False
        self.deposit_state_safe = False

        self.account_state_failure_reason = None
        self.last_account_failure_log_at = 0
        self.account_failure_log_interval = 30
        self.failed_account_retry_interval = 10

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

        # 웹 보유현황은 Kiwoom 메모리를 FastAPI가 직접 읽지 않도록
        # monitoring.db에 짧은 주기로 최신 스냅샷을 저장한다.
        self.last_position_snapshot_at = 0
        self.position_snapshot_interval = 5

        # 전략 성과 그래프는 1분 단위면 충분하다.
        # 전략별 공용현금 배분은 하지 않고 실제 매매원금/손익만 추적한다.
        self.last_strategy_equity_snapshot_at = 0
        self.strategy_equity_snapshot_interval = 60

        self.last_virtual_daily_snapshot_date = None

        self.buy_order_cancel_after = 600
        self.pending_buy_cancellations = {}

        self.pending_sold_position_deletions = {}
        self.sold_position_delete_confirmations = 2

        self.last_daily_price_refresh_date = None
        self.is_refreshing_daily_price_data = False

        # 장마감 일봉 저장이 가능한 날인지 구분하기 위한 상태.
        # 오늘 정규장 시간에 프로그램이 실제로 실행된 적이 있어야
        # Kiwoom 실시간 OHLCV가 메모리에 누적되었다고 판단한다.
        self.market_open_observed_date = None

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
        # This timer only checks a completed Future; HTTP runs outside Qt.
        self.command_check_interval = 250
        self.telegram_command_poller = TelegramCommandPoller(get_updates)
        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.telegram_command_poller.close)

        # 전략은 매수 조건만 판단한다.
        # 실제 주문 수량/예산/계좌 전체 포지션 제한은 Manager가 담당한다.
        # 웹 가상매매는 실계좌 잔고/예수금과 완전히 분리해 같은 실시간 틱을 병렬 처리한다.
        init_virtual_trading_tables()
        self.virtual_strategy_engine = VirtualStrategyEngine(self.strategies)
        self.kiwoom.add_realtime_listener(
            self.virtual_strategy_engine.on_realtime_tick
        )

        for strategy in self.strategies:
            setter = getattr(strategy, "set_buy_order_handler", None)
            if callable(setter):
                setter(self.request_buy_order)

            guard_setter = getattr(strategy, "set_order_guard", None)
            if callable(guard_setter):
                guard_setter(self.can_event_driven_order)

            rejection_setter = getattr(
                strategy,
                "set_buy_rejection_reason_handler",
                None,
            )
            if callable(rejection_setter):
                rejection_setter(self.get_buy_rejection_reason)

    # ------------------------------------------------------------------
    # 시작/타이머
    # ------------------------------------------------------------------

    def _orders(self):
        # Lightweight adapter; reservations and guards stay on this manager.
        return OrderCoordinator(
            self,
            notify=send_message,
            notify_error=send_error,
            market_is_open=check_transaction_open,
        )

    def _account_sync(self):
        return AccountStateSynchronizer(
            self,
            position_db=POSITION_DB,
            notify=send_message,
            notify_error=send_error,
        )

    def start(self):
        print("[StrategyManager] start 호출")
        self.is_running = True

        init_position_strategy_table()
        init_monitoring_tables()

        self.initialize_strategies()

        # 프로그램 재시작으로 복원된 가상 포지션 상태를 웹 DB에 즉시 1회 동기화.
        self.virtual_strategy_engine.flush_snapshot_if_due(force=True)

        # 초기 잔고 조회가 정상 완료된 경우 웹 보유현황을 즉시 1회 저장.
        self.save_position_snapshot_if_due(force=True)
        self.save_strategy_equity_snapshot_if_due(
            force=True
        )

        self.send_one_value_quality_report_if_needed(force=True)

        if check_transaction_open():
            self.market_open_observed_date = datetime.now().strftime("%Y%m%d")
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
            self.market_open_observed_date = datetime.now().strftime("%Y%m%d")
            self.set_timer_interval(self.market_open_interval)
            print("[StrategyManager] 장 시작 감지 - 1초 상태 검사로 전환")
            self.step()

    # ------------------------------------------------------------------
    # 중앙 자금 관리
    # ------------------------------------------------------------------

    def calculate_total_assets(self):
        return self._account_sync().calculate_total_assets()

    def get_reserved_buy_amount(self):
        return self._orders().get_reserved_buy_amount()

    def get_available_buy_cash(self):
        return self._orders().get_available_buy_cash()

    def get_account_position_codes(self):
        return self._orders().get_account_position_codes()

    def get_account_position_count(self):
        return self._orders().get_account_position_count()

    def has_pending_buy(self, code):
        return self._orders().has_pending_buy(code)

    def _set_buy_rejection(self, code, reason):
        return self._orders()._set_buy_rejection(code, reason)

    def get_buy_rejection_reason(self, code, clear=False):
        return self._orders().get_buy_rejection_reason(code, clear)

    def get_event_driven_order_block_reason(self):
        return self._orders().get_event_driven_order_block_reason()

    def request_buy_order(
        self,
        strategy_name,
        code,
        code_name,
        price,
        rqname,
        screen_no,
        buy_fee_rate=None,
        stop_price=None,
        risk_ratio=None,
        sell_cost_rate=0.0,
    ):
        return self._orders().request_buy_order(
            strategy_name,
            code,
            code_name,
            price,
            rqname,
            screen_no,
            buy_fee_rate,
            stop_price,
            risk_ratio,
            sell_cost_rate,
        )

    def sync_deposit_from_kiwoom(self):
        return self._account_sync().sync_deposit_from_kiwoom()

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

        codes = set(self.virtual_strategy_engine.paper_engine.get_candidate_codes())
        for strategy in strategies:
            if not getattr(strategy, "is_init_success", False):
                continue

            getter = getattr(strategy, "get_realtime_candidate_codes", None)
            if callable(getter):
                codes.update(getter())

        # 실제 계좌와 무관하게 열린 가상 포지션은 반드시 실시간 감시를 계속한다.
        # 특히 ValueQuality 후보가 다음 갱신에서 재무 필터를 벗어나더라도
        # 이미 가상 매수된 종목의 -10%/+30% 매도 관리는 유지돼야 한다.
        codes.update(
            self.virtual_strategy_engine.get_open_codes()
        )

        return sorted(
            str(code).strip().upper().zfill(6)
            for code in codes
        )

    def register_shared_event_realtime(self, strategies, force_refresh=False):
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

        # Operational strategies and A/B/C paper accounts share one tick stream.
        fid_names = ("체결시간", "현재가", "시가", "고가", "저가", "누적거래량", "거래량", "(최우선)매수호가", "(최우선)매도호가")
        self.kiwoom.set_stock_realtime_fid_names(fid_names)
        fids = ";".join(get_fid(name) for name in fid_names)
        screens = (len(union_codes) + 89) // 90
        if screens > 190:
            raise RuntimeError("공통 실시간 화면 안전 한도 초과")
        for index in range(0, len(union_codes), 90):
            result = self.kiwoom.set_real_reg(str(7000 + index // 90), ";".join(union_codes[index:index + 90]), fids, "0")
            if result not in (0, None):
                raise RuntimeError(f"공통 실시간 등록 실패: {result}")
        for strategy in active:
            registered = getattr(strategy, "on_realtime_registered", None)
            if callable(registered):
                registered()

        print(
            "[StrategyManager] event-driven 공통 실시간 등록: "
            f"{len(union_codes)}종목 / {len(active)}전략 공유"
        )

        return union_codes

    def can_event_driven_order(self):
        return self._orders().can_event_driven_order()

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

            # 프로그램 재시작 전에 매수 체결 이벤트가 일부 누락되어
            # strategy_position.db에 0주/0원 예약 row만 남은 경우,
            # 전략명이 이미 확정된 포지션에 한해서 실제 잔고로 안전 복구한다.
            # 일반 수량 불일치(수동 부분매도 등)도 같은 함수에서 실제 잔고 기준으로 보정한다.
            self.sync_position_quantities_from_balance()
            self.cleanup_sold_positions()
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
                    send_error(
                        f"[{strategy.strategy_name} 초기화 실패]\n{error_msg}",
                        event_key=f"strategy.StrategyManager.initialize_strategies:{strategy.strategy_name}",
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
            send_error(error_msg, event_key='strategy.StrategyManager.initialize_strategies')

    # ------------------------------------------------------------------
    # 계좌/미체결 동기화
    # ------------------------------------------------------------------

    def refresh_order_and_balance_state(self, allow_buy_cancel=True):
        return self._account_sync().refresh_order_and_balance_state(allow_buy_cancel)

    def cancel_stale_buy_orders(self):
        return self._orders().cancel_stale_buy_orders()

    def cleanup_cancelled_buy_positions(self):
        return self._orders().cleanup_cancelled_buy_positions()

    def sync_position_quantities_from_balance(self):
        return self._account_sync().sync_position_quantities_from_balance()

    def notify_strategy_position_closed(self, code, strategy_name, reason=""):
        return self._account_sync().notify_strategy_position_closed(
            code, strategy_name, reason
        )

    def cleanup_sold_positions(self):
        return self._account_sync().cleanup_sold_positions()

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

        codes.update(self.virtual_strategy_engine.paper_engine.get_candidate_codes())
        codes.update(self.virtual_strategy_engine.get_open_codes())
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

        target_count = len(target_codes)
        coverage = (
            saved_count / target_count
            if target_count > 0
            else 0.0
        )

        print(
            "[StrategyManager] Kiwoom 당일 일봉 저장 결과: "
            f"{saved_count}/{target_count}종목 "
            f"({coverage * 100:.1f}%) -> "
            f"{self.market_history_db_path}"
        )

        if target_count > 0 and saved_count == 0:
            raise RuntimeError(
                "Kiwoom 당일 일봉 저장 0건 - "
                "실시간 OHLCV(현재가/시가/고가/저가/누적거래량) "
                "수신 상태를 확인하세요."
            )

        if target_count > 0 and coverage < 0.95:
            warning = (
                "[경고] Kiwoom 당일 일봉 저장률이 낮습니다 - "
                f"{saved_count}/{target_count}종목 "
                f"({coverage * 100:.1f}%). "
                "누락 종목은 다음 64bit bootstrap 증분 수집으로 보충하세요."
            )
            print(f"[StrategyManager] {warning}")
            send_message(warning)

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

            # 오늘 정규장 시간에 프로그램이 한 번도 실행되지 않았다면
            # 실시간 OHLCV가 쌓이지 않은 것이 정상이다.
            # 이 경우에는 오류/traceback을 만들지 않고 장마감 저장만 건너뛴다.
            if self.market_open_observed_date != today:
                realtime_count = len(
                    self.kiwoom.universe_realtime_transaction_info
                )

                print(
                    "[StrategyManager] 장 종료 일봉 갱신 SKIP: "
                    f"{today} / 오늘 장중 실행 이력 없음 / "
                    f"실시간 캐시 {realtime_count}종목"
                )
                print(
                    "[StrategyManager] 다음 64bit bootstrap 증분 수집에서 "
                    f"{today} 누락 일봉을 보충하세요."
                )

                # 같은 장외 세션에서 5분마다 동일 SKIP 로그가 반복되지 않게 한다.
                self.last_daily_price_refresh_date = today
                return False

            print(
                f"[StrategyManager] 장 종료 일봉 갱신 시작: {today}"
            )

            # 여기까지 왔다는 것은 오늘 장중 실행 이력이 있다는 뜻이다.
            # 따라서 저장 0건은 정상 상황이 아니며,
            # save_today_realtime_bars_to_shared_db()의 RuntimeError를 유지하여
            # step() 최상위 except에서 traceback을 그대로 출력/전송한다.
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
                    send_error(
                        f"[유니버스/일봉 갱신 실패] "
                        f"{strategy.strategy_name}\n{error_msg}",
                        event_key=f"strategy.StrategyManager.refresh_daily_price_data_after_close:{strategy.strategy_name}",
                    )

            if failed:
                return False

            self.virtual_strategy_engine.paper_engine.get_candidate_codes(force=True)
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
        return self._account_sync().check_balance_position_db_consistency()


    def save_position_snapshot_if_due(
        self,
        force=False,
    ):
        """현재 실제 보유현황을 웹 조회용 DB에 저장한다."""
        if not self.kiwoom.last_balance_query_success:
            return False

        now = time.time()

        if (
            not force
            and now - self.last_position_snapshot_at
            < self.position_snapshot_interval
        ):
            return False

        count = save_position_snapshot(
            balance=self.kiwoom.balance,
            realtime_price_map=(
                self.kiwoom
                .universe_realtime_transaction_info
            ),
        )

        self.last_position_snapshot_at = now

        return True


    def save_strategy_equity_snapshot_if_due(
        self,
        force=False,
    ):
        """웹 전략별 누적 성과곡선을 1분 주기로 저장한다."""
        if not self.kiwoom.last_balance_query_success:
            return False

        now = time.time()

        if (
            not force
            and (
                now
                - self.last_strategy_equity_snapshot_at
                < self.strategy_equity_snapshot_interval
            )
        ):
            return False

        strategy_names = [
            strategy.strategy_name
            for strategy in self.strategies
        ]

        save_strategy_equity_snapshots(
            strategy_names
        )

        self.last_strategy_equity_snapshot_at = now

        return True

    def save_virtual_strategy_daily_snapshot_after_close(self):
        """오늘 장중 가격을 받은 경우 가상 전략 성과지수를 장마감 후 하루 1회 저장."""
        if not check_transaction_closed():
            return False

        today = datetime.now().strftime("%Y%m%d")

        if self.last_virtual_daily_snapshot_date == today:
            return False

        if self.market_open_observed_date != today:
            print(
                "[StrategyManager] 가상 전략 일별지수 SKIP: "
                f"{today} / 오늘 장중 실행 이력 없음"
            )
            self.last_virtual_daily_snapshot_date = today
            return False

        self.virtual_strategy_engine.flush_snapshot_if_due(force=True)

        strategy_names = [
            strategy.strategy_name
            for strategy in self.strategies
            if getattr(strategy, "is_init_success", False)
        ]

        snapshots = save_virtual_strategy_daily_snapshots(
            strategy_names,
            snapshot_date=today,
        )
        self.last_virtual_daily_snapshot_date = today

        for item in snapshots:
            print(
                "[StrategyManager] 가상 전략 일별지수 저장: "
                f"{item['strategy_name']} / "
                f"INDEX {item['strategy_index']:.2f} / "
                f"일간 {item['daily_return_pct']:+.2f}% / "
                f"DD {item['drawdown_pct']:.2f}%"
            )

        return bool(snapshots)


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
            updates = self.telegram_command_poller.poll(
                self.telegram_update_offset
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
        return StatusMessageBuilder(self).build()

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

    def sync_account_while_waiting_for_strategy(self, now):
        """Keep broker reconciliation alive without strategy orders/cancels."""
        self.set_timer_interval(
            self.market_open_interval if check_transaction_open()
            else self.market_closed_interval
        )
        if now - self.last_order_sync_at < self.order_sync_interval:
            return
        if not self.refresh_order_and_balance_state(allow_buy_cancel=False):
            return
        self.save_position_snapshot_if_due(force=True)
        self.save_strategy_equity_snapshot_if_due(force=True)
        if (not self.deposit_state_safe
                or now - self.last_deposit_sync_at >= self.deposit_sync_interval):
            if self.sync_deposit_from_kiwoom():
                self.save_performance_snapshot()
        print("[StrategyManager] 전략 초기화 대기 - 계좌 동기화/수동매도 DB 정리 유지")

    def step(self):
        if not self.is_running:
            return

        try:
            now = time.time()
            if not self.is_initialized:
                self.sync_account_while_waiting_for_strategy(now)
                return

            # 가상매매는 실계좌 상태와 독립적이다.
            # 실시간 틱에서는 메모리만 갱신하고, 여기서 최대 5초마다 SQLite에 일괄 저장한다.
            # 따라서 실제 계좌 잔고/예수금 동기화가 실패해도 웹 가상포지션 스냅샷은 유지된다.
            self.virtual_strategy_engine.flush_snapshot_if_due()

            self.send_one_value_quality_report_if_needed()

            # 장외에는 주문하지 않고 상태/장마감 데이터만 정리한다.
            if not check_transaction_open():
                self.set_timer_interval(
                    self.market_closed_interval
                )

                self.virtual_strategy_engine.flush_snapshot_if_due(
                    force=True
                )

                self.refresh_order_and_balance_state(
                    allow_buy_cancel=False
                )

                self.save_position_snapshot_if_due(
                    force=True
                )
                self.save_strategy_equity_snapshot_if_due(
                    force=True
                )

                if (
                    self.kiwoom.last_balance_query_success
                    and self.sync_deposit_from_kiwoom()
                ):
                    self.save_performance_snapshot()

                self.save_virtual_strategy_daily_snapshot_after_close()

                self.refresh_daily_price_data_after_close()

                print(
                    "[StrategyManager] 장 시간이 아니므로 "
                    "계좌 상태만 확인하고 대기합니다."
                )
                return

            # 정규장 루프에 실제 진입했다는 사실을 날짜별로 기록한다.
            # 프로그램을 장중에 시작했거나, 장 시작 전부터 켜둔 경우 모두 잡힌다.
            self.market_open_observed_date = datetime.now().strftime("%Y%m%d")

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

            # 실시간 틱을 반영한 현재가를 5초 간격으로 웹 DB에 저장한다.
            # 잔고 조회 자체는 기존 정책대로 60초 동기화를 유지한다.
            self.save_position_snapshot_if_due()
            self.save_strategy_equity_snapshot_if_due()

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
            send_error(error_msg, event_key='strategy.StrategyManager.step')
