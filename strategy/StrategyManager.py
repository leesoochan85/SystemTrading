import time
import traceback
from PyQt5.QtCore import QObject, QTimer

from util.time_helper import (
    check_transaction_open,
    check_transaction_closed,
)
from util.notifier import send_message, get_updates
from util.const import Telegram_chat_ID
from util.db_helper import (
    init_position_strategy_table,
    init_sent_news_table,
    init_monitoring_tables,
    is_news_sent,
    save_sent_news,
    delete_position_strategy,
    get_strategy_position_counts,
    get_filled_position_codes,
    get_all_position_details,
    get_today_order_count,
    get_today_realized_pnl,
    get_last_sent_news_at,
    save_daily_equity,
    save_strategy_daily_summaries,
)
from datetime import datetime
from util.news_helper import (
    fetch_naver_economy_news,
    format_naver_economy_news_message,
)


class StrategyManager(QObject):
    def __init__(self, kiwoom, strategies):
        super().__init__()
        self.kiwoom = kiwoom
        self.strategies = strategies

        self.is_initialized = False
        self.is_running = False

        self.deposit = 0
        self.last_deposit_sync_at = 0
        self.deposit_sync_interval = 300  # 5분
        
        # 잔고 조회가 정상 완료된 경우에만 전략 주문 허용
        self.account_state_safe = False

        # 예수금 조회가 정상 완료된 경우에만 신규 주문 금액 계산 허용
        self.deposit_state_safe = False
        self.last_deposit_failure_notice_at = 0
        self.deposit_failure_notice_interval = 60  # 실패 알림은 최대 1분에 1회
        
        self.universe_codes = []
        self.current_index = 0

        self.timer = QTimer()
        self.timer.timeout.connect(self.step)

        self.last_news_sent_at = 0
        self.news_send_interval = 600  # 10분

        self.market_open_interval = 1000        # 1초
        self.market_closed_interval = 300000    # 5분
        self.current_timer_interval = None

        self.buy_order_cancel_after = 600 #10분 후 자동취소
        self.pending_buy_cancellations = {}
        
        # 전량 매도 후 포지션 삭제는 잔고 없음이 2회 연속 확인될 때만 수행
        self.pending_sold_position_deletions = {}
        self.sold_position_delete_confirmations = 2

        self.last_order_sync_at = 0
        self.order_sync_interval = 60  # 장중 미체결/잔고 점검 주기: 60초
        
        # 장 종료 후 일봉 데이터 갱신 관리
        self.last_daily_price_refresh_date = None
        self.is_refreshing_daily_price_data = False

        # 실제 잔고와 전략 DB 불일치 알림 중복 방지
        self.last_position_db_mismatch_signature = None
        
        self.telegram_update_offset = None



        self.command_timer = QTimer()
        self.command_timer.timeout.connect(self.poll_telegram_commands)
        self.command_check_interval = 3000  # 3초

    def start(self):
        print("[StrategyManager] start 호출")
        self.is_running = True

        init_sent_news_table()
        init_monitoring_tables()

        # 프로그램 시작 직후 뉴스 1개 먼저 전송
        self.send_one_economy_news_if_needed()

        self.initialize_strategies()

        # 1초마다 한 종목씩 검사
        if check_transaction_open():
            self.set_timer_interval(self.market_open_interval)
        else:
            self.set_timer_interval(self.market_closed_interval)

        self.command_timer.start(self.command_check_interval)
        print("[StrategyManager] 타이머 시작")
        print("[StrategyManager] Telegram / status 명령대기")

    def set_timer_interval(self, interval_ms):
        if self.current_timer_interval == interval_ms:
            return

        self.timer.start(interval_ms)
        self.current_timer_interval = interval_ms

    def get_code_name(self, code):
        for strategy in self.strategies:
            if code in strategy.universe:
                return strategy.universe[code].get("code_name", code)
        return code

    def poll_telegram_commands(self):
        """
        Telegram에서 /status, /help 명령을 확인한다.

        주의:
        - 지정된 TELEGRAM_CHAT_ID의 메시지만 처리한다.
        - 상태 출력 중 키움 TR 조회를 새로 요청하지 않는다.
        """
        try:
            updates = get_updates(offset=self.telegram_update_offset)

            for update in updates:
                update_id = update.get("update_id")

                if update_id is not None:
                    self.telegram_update_offset = update_id + 1

                message = update.get("message", {})
                chat = message.get("chat", {})
                incoming_chat_id = str(chat.get("id", ""))
                text = str(message.get("text", "") or "").strip()

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
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if self.last_order_sync_at:
            balance_sync_text = datetime.fromtimestamp(
                self.last_order_sync_at
            ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            balance_sync_text = "아직 동기화되지 않음"

        lines = [
            "[자동매매 상태]",
            f"조회 시각: {now_text}",
            f"잔고/미체결 기준: {balance_sync_text}",
            f"예수금: {self.deposit:,}원",
            "",
        ]

        self._append_balance_status(lines)
        self._append_strategy_status(lines)
        self._append_pending_order_status(lines)
        self._append_today_summary(lines)

        return "\n".join(lines)

    def _append_balance_status(self, lines):
        balance = self.kiwoom.balance

        lines.append(f"[현재 보유 종목] {len(balance)}개")

        if not balance:
            lines.append("- 없음")
            lines.append("")
            return

        for code, info in balance.items():
            code_name = info.get("종목명", code)
            quantity = int(info.get("보유수량", 0) or 0)
            buy_price = int(info.get("매입가", 0) or 0)

            realtime_info = self.kiwoom.universe_realtime_transaction_info.get(code, {})
            current_price = int(
                realtime_info.get("현재가")
                or info.get("현재가", 0)
                or 0
            )

            if buy_price > 0:
                return_rate = (current_price - buy_price) / buy_price * 100
            else:
                return_rate = float(info.get("수익률", 0) or 0)

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
            count = counts.get(strategy.strategy_name, 0)
            lines.append(f"- {strategy.strategy_name}: {count}개")

        lines.append("")

    def _append_pending_order_status(self, lines):
        pending_orders = []

        for code, info in self.kiwoom.order.items():
            remain_quantity = int(info.get("미체결수량", 0) or 0)

            if remain_quantity <= 0:
                continue

            pending_orders.append((code, info))

        lines.append(f"[미체결 주문] {len(pending_orders)}건")

        if not pending_orders:
            lines.append("- 없음")
            lines.append("")
            return

        for code, info in pending_orders:
            code_name = info.get("종목명") or self.get_code_name(code)
            order_type = info.get("주문구분", "-")
            remain_quantity = int(info.get("미체결수량", 0) or 0)
            order_price = int(info.get("주문가격", 0) or 0)
            elapsed_text = self._get_order_elapsed_text(info)

            lines.append(
                f"- {code_name}({code}) {order_type} "
                f"{remain_quantity}주 {order_price:,}원 | "
                f"경과 {elapsed_text}"
            )

        lines.append("")

    def _get_order_elapsed_text(self, order_info):
        order_time = order_info.get("order_time")

        if order_time:
            elapsed_seconds = max(0, int(time.time() - float(order_time)))
            minutes, seconds = divmod(elapsed_seconds, 60)
            return f"{minutes}분 {seconds}초"

        broker_time = str(order_info.get("시간", "") or "").strip()

        if len(broker_time) == 6 and broker_time.isdigit():
            try:
                today = datetime.now().strftime("%Y%m%d")
                ordered_at = datetime.strptime(
                    today + broker_time,
                    "%Y%m%d%H%M%S",
                )
                elapsed_seconds = max(
                    0,
                    int((datetime.now() - ordered_at).total_seconds()),
                )
                minutes, seconds = divmod(elapsed_seconds, 60)
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

        last_news_sent_at = get_last_sent_news_at()

        if last_news_sent_at:
            last_news_text = (
                f"{last_news_sent_at[0:4]}-{last_news_sent_at[4:6]}-"
                f"{last_news_sent_at[6:8]} "
                f"{last_news_sent_at[8:10]}:{last_news_sent_at[10:12]}:"
                f"{last_news_sent_at[12:14]}"
            )
        else:
            last_news_text = "전송 기록 없음"

        lines.append("[오늘 요약]")
        lines.append(
            f"- 매도 체결 총손익(수수료·세금 전): "
            f"{gross_realized_pnl:+,}원"
        )
        lines.append(
            f"- 매도 체결 순손익(비용 확인 거래 합산): "
            f"{net_realized_pnl:+,}원"
        )
        lines.append(f"- 주문 횟수: {order_count}건")
        lines.append(f"- 마지막 뉴스 전송: {last_news_text}")

        if missing_buy_price_count > 0:
            lines.append(
                f"- 총손익 계산 제외 매도 거래: "
                f"{missing_buy_price_count}건 "
                f"(매입가 기록 없음)"
            )

        if missing_cost_count > 0:
            lines.append(
                f"- 순손익 계산 보류 거래: "
                f"{missing_cost_count}건 "
                f"(수수료·세금 수신 대기 또는 미확인)"
            )

    def sync_deposit_from_kiwoom(self):
        """
        예수금 조회가 정상 완료된 경우에만 manager와 전략의 예수금을 갱신한다.

        실패 시:
        - 마지막 정상 예수금은 변경하지 않는다.
        - 신규 주문 검사는 현재 주기에서 수행하지 않는다.
        - 다음 step에서 다시 조회를 시도한다.
        """
        queried_deposit = self.kiwoom.get_deposit()

        if (
            not self.kiwoom.last_deposit_query_success
            or queried_deposit is None
        ):
            self.deposit_state_safe = False

            now = time.time()

            if (
                now - self.last_deposit_failure_notice_at
                >= self.deposit_failure_notice_interval
            ):
                message = (
                    "[안전 중지] 예수금 조회 실패 - "
                    "마지막 정상 예수금을 유지하고 신규 주문을 중단합니다."
                )
                print(f"[StrategyManager] {message}")
                send_message(message)
                self.last_deposit_failure_notice_at = now

            return False

        self.deposit = int(queried_deposit)
        self.deposit_state_safe = True
        self.last_deposit_sync_at = time.time()
        self.sync_deposit_to_strategies()

        print(f"[StrategyManager] 예수금 동기화: {self.deposit:,}원")

        return True

    def sync_deposit_to_strategies(self):
        for strategy in self.strategies:
            strategy.deposit = self.deposit

    def initialize_strategies(self):
        print("[StrategyManager] 전략 초기화 시작")

        try:
            init_position_strategy_table()
            init_sent_news_table()
            init_monitoring_tables()

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
            print("[StrategyManager] 잔고 조회 완료")
            self.last_order_sync_at = time.time()

            # 프로그램 시작 시 실제 보유 종목과 전략 DB 불일치를 즉시 경고
            self.check_balance_position_db_consistency()
            
            print("[StrategyManager] 예수금 조회 시작")

            if not self.sync_deposit_from_kiwoom():
                raise RuntimeError(
                    "초기 예수금 조회 실패 - "
                    "주문 가능 금액 오인 방지를 위해 자동매매 시작을 중단합니다."
                )

            print("[StrategyManager] 예수금 조회 완료")
            self.save_performance_snapshot()

            for strategy in self.strategies:
                try:
                    print(f"[StrategyManager] {strategy.strategy_name} 초기화 시작")

                    strategy.check_and_get_universe()
                    strategy.check_and_get_price_data()
                    strategy.deposit = self.deposit
                    strategy.set_universe_real_time()
                    strategy.is_init_success = True

                    print(f"[StrategyManager] {strategy.strategy_name} 초기화 완료")
                    send_message(f"[{strategy.strategy_name}] 초기화 완료")

                except Exception:
                    error_msg = traceback.format_exc()
                    print(error_msg)
                    send_message(error_msg)
                    strategy.is_init_success = False

            self.refresh_universe_codes()

            self.is_initialized = True
            print("[StrategyManager] 전략 초기화 완료")

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)
            self.is_initialized = False

    def refresh_universe_codes(self):
        universe_codes = []

        for strategy in self.strategies:
            if not strategy.is_init_success:
                continue
            universe_codes.extend(strategy.universe.keys())

        self.universe_codes = list(dict.fromkeys(universe_codes))
        self.current_index = 0

        print(f"[StrategyManager] 통합 유니버스 수: {len(self.universe_codes)}")
    
    def refresh_daily_price_data_after_close(self):
        """
        거래일 장 종료 후 각 전략의 일봉 데이터를 하루 한 번 갱신한다.

        - 프로그램을 며칠 동안 계속 실행해도 다음 거래일 신호 계산에
        직전 거래일의 완성 일봉이 반영되도록 한다.
        - 주말·휴장일에는 실행하지 않는다.
        - 같은 날짜에 이미 완료된 경우 다시 실행하지 않는다.
        - 일부 전략 갱신이 실패하면 완료 처리하지 않고 다음 상태 점검 때 재시도한다.
        """
        if not check_transaction_closed():
            return False

        today = datetime.now().strftime("%Y%m%d")

        if self.last_daily_price_refresh_date == today:
            return False

        if self.is_refreshing_daily_price_data:
            return False

        self.is_refreshing_daily_price_data = True
        success_names = []
        failed_names = []

        try:
            print(f"[StrategyManager] 장 종료 일봉 갱신 시작: {today}")

            for strategy in self.strategies:
                if not strategy.is_init_success:
                    continue

                try:
                    print(
                        f"[StrategyManager] "
                        f"{strategy.strategy_name} 일봉 갱신 시작"
                    )

                    strategy.check_and_get_price_data()
                    success_names.append(strategy.strategy_name)

                    print(
                        f"[StrategyManager] "
                        f"{strategy.strategy_name} 일봉 갱신 완료"
                    )

                except Exception:
                    failed_names.append(strategy.strategy_name)
                    error_msg = traceback.format_exc()
                    print(error_msg)
                    send_message(
                        f"[일봉 갱신 실패] {strategy.strategy_name}\n"
                        f"{error_msg}"
                    )

            if failed_names:
                print(
                    f"[StrategyManager] 일부 전략 일봉 갱신 실패 - "
                    f"다음 상태 점검 때 재시도: {', '.join(failed_names)}"
                )
                return False

            self.last_daily_price_refresh_date = today

            message = (
                f"[일봉 갱신 완료] {today} / "
                f"{len(success_names)}개 전략 갱신 완료"
            )
            print(f"[StrategyManager] {message}")
            send_message(message)

            return True

        finally:
            self.is_refreshing_daily_price_data = False
    
    def check_balance_position_db_consistency(self):
        """
        실제 키움 잔고와 strategy_position.db의 포지션 정보를 비교하고,
        불일치가 새로 발생하거나 내용이 바뀐 경우에만 경고를 전송한다.

        자동으로 전략을 배정하거나 DB를 수정하지 않는다.
        """
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
                "code_name": info.get("종목명") or self.get_code_name(code),
                "quantity": quantity,
                "buy_price": int(info.get("매입가", 0) or 0),
            }

        db_positions = get_all_position_details()
        issues = []

        # 1. 실제 잔고에 있는 종목이 전략 DB에서도 정상 관리되는지 확인
        for code, actual in sorted(actual_balance.items()):
            code_name = actual["code_name"]
            actual_quantity = actual["quantity"]

            db_position = db_positions.get(code)

            if db_position is None:
                issues.append(
                    f"- [미관리 보유] {code_name}({code}) "
                    f"실제 {actual_quantity}주 / 전략 DB 기록 없음"
                )
                continue

            strategy_name = db_position.get("strategy_name", "-")
            db_quantity = int(db_position.get("quantity", 0) or 0)
            db_buy_price = float(db_position.get("buy_price", 0) or 0)

            if db_quantity != actual_quantity:
                issues.append(
                    f"- [수량 불일치] {code_name}({code}) "
                    f"실제 {actual_quantity}주 / DB {db_quantity}주 / "
                    f"{strategy_name}"
                )

            if db_buy_price <= 0:
                issues.append(
                    f"- [매입가 누락] {code_name}({code}) "
                    f"실제 보유 중 / DB 매입가 없음 / {strategy_name}"
                )

        # 2. 전략 DB에는 실제 체결 포지션이 있는데 잔고에는 없는지 확인
        for code, db_position in sorted(db_positions.items()):
            db_quantity = int(db_position.get("quantity", 0) or 0)
            db_buy_price = float(db_position.get("buy_price", 0) or 0)

            # 아직 체결되지 않은 단순 매수 예약 행은 제외
            if db_quantity <= 0 and db_buy_price <= 0:
                continue

            if code in actual_balance:
                continue
            
            # 전량 매도 후 잔고 없음 확인 중인 포지션은
            # 정상적인 삭제 대기 상태이므로 불일치 경고에서 제외한다.
            if code in self.pending_sold_position_deletions:
                continue

            order_info = self.kiwoom.order.get(code, {})
            order_type = str(order_info.get("주문구분", "") or "").strip()
            remain_quantity = int(order_info.get("미체결수량", 0) or 0)

            # 실제 매도 주문이 아직 미체결 중이면 삭제 대기 상태이므로 경고하지 않음
            if order_type == "매도" and remain_quantity > 0:
                continue

            code_name = (
                db_position.get("code_name")
                or self.get_code_name(code)
                or code
            )
            strategy_name = db_position.get("strategy_name", "-")

            issues.append(
                f"- [잔고 없음/DB 잔존] {code_name}({code}) "
                f"DB {db_quantity}주 / {strategy_name}"
            )

        signature = tuple(sorted(issues))

        # 이전 검사와 결과가 완전히 같으면 중복 알림을 보내지 않음
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
            message = "[정상화] 실제 잔고와 전략 DB 불일치가 해소되었습니다."
            print(f"[StrategyManager] {message}")
            send_message(message)

        return False
    
    def send_one_economy_news_if_needed(self):
        now = time.time()

        if now - self.last_news_sent_at < self.news_send_interval:
            return

        try:
            news_items = fetch_naver_economy_news(max_items=20)

            if not news_items:
                send_message("[네이버 경제 뉴스]\n가져온 뉴스가 없습니다.")
                self.last_news_sent_at = now
                return

            target_news = None

            for item in news_items:
                title = item.get("title")
                link = item.get("link")

                if not title or not link:
                    continue

                # 핵심: DB에 이미 보낸 링크가 있으면 건너뜀
                if is_news_sent(link):
                    continue

                target_news = item
                break

            if target_news is None:
                print("[StrategyManager] 새로 보낼 경제 뉴스가 없습니다.")
                self.last_news_sent_at = now
                return

            title = target_news["title"]
            link = target_news["link"]

            message = f"[네이버 경제 뉴스]\n\n{title}\n{link}"
            result = send_message(message)

            # 텔레그램 전송 함수는 성공 시 result를 반환하고, 실패/스킵 시 None 반환
            # 실제 전송 성공한 경우에만 DB에 저장
            if result is not None:
                save_sent_news(
                    link=link,
                    title=title,
                    source="naver_economy",
                )
                print(f"[StrategyManager] 경제 뉴스 전송 완료: {title}")
            else:
                print(f"[StrategyManager] 경제 뉴스 전송 실패 또는 스킵: {title}")

            self.last_news_sent_at = now

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)
            self.last_news_sent_at = now

    def save_performance_snapshot(self):
        """현재 계좌 상태와 전략별 당일 실적을 monitoring.db에 갱신한다."""
        evaluation_amount, total_assets = save_daily_equity(
            deposit=self.deposit,
            balance=self.kiwoom.balance,
        )
        save_strategy_daily_summaries(
            [strategy.strategy_name for strategy in self.strategies]
        )
        print(
            f"[StrategyManager] 실전 성과 저장: 예수금 {self.deposit:,}원 / "
            f"평가금액 {evaluation_amount:,}원 / 총자산 {total_assets:,}원"
        )

    def refresh_order_and_balance_state(self, allow_buy_cancel=True):
        """
        미체결 주문과 잔고를 최신화한 뒤,
        취소 완료 여부와 전량 매도 완료 여부를 정리한다.

        잔고 조회 실패 시:
        - 기존 잔고는 Kiwoom이 유지
        - 포지션 삭제 수행 안 함
        - 성과 스냅샷 저장 안 함
        - 신규 전략 주문도 중단
        """
        self.kiwoom.get_order()

        if not self.kiwoom.last_order_query_success:
            self.last_order_sync_at = time.time()
            self.account_state_safe = False

            message = (
                "[안전 중지] 미체결 주문 조회 실패 - "
                "기존 주문 정보를 유지하고 포지션 정리 및 전략 주문을 중단합니다."
            )
            print(f"[StrategyManager] {message}")
            send_message(message)

            return False

        self.kiwoom.get_balance()
        self.last_order_sync_at = time.time()

        if not self.kiwoom.last_balance_query_success:
            self.account_state_safe = False

            message = (
                "[안전 중지] 잔고 조회 실패 - "
                "기존 잔고를 유지하고 포지션 정리 및 전략 주문을 중단합니다."
            )
            print(f"[StrategyManager] {message}")
            send_message(message)

            return False

        self.account_state_safe = True

        if allow_buy_cancel:
            self.cancel_stale_buy_orders()

        self.cleanup_cancelled_buy_positions()
        self.cleanup_sold_positions()
        
        # 수동매매, 프로그램 재시작, 체결 반영 누락으로 인한
        # 실제 잔고와 전략 DB 불일치를 감지한다.
        self.check_balance_position_db_consistency()

        if self.deposit_state_safe:
            self.save_performance_snapshot()
        else:
            print(
                "[StrategyManager] 예수금 상태 미확인 - "
                "성과 스냅샷 저장을 보류합니다."
            )

        return True

    def cancel_stale_buy_orders(self):
        now = time.time()

        for code, order_info in list(self.kiwoom.order.items()):
            try:
                order_type = str(order_info.get("주문구분", "")).strip()
                remain_qty = int(order_info.get("미체결수량", 0) or 0)
                strategy_name = order_info.get("strategy_name", "")
                order_time = order_info.get("order_time", 0)
                order_no = order_info.get("주문번호") or order_info.get("order_no") or ""

                # 매수 미체결 주문만 관리
                if order_type != "매수":
                    continue

                if remain_qty <= 0:
                    continue

                # 이미 취소 요청을 보낸 주문이면 다시 보내지 않음
                if code in self.pending_buy_cancellations:
                    continue

                # 프로그램 실행 전부터 존재하던 미체결 주문은
                # 최초 확인 시점부터 10분을 새로 계산
                if not order_time:
                    order_info["order_time"] = now
                    continue

                if now - order_time < self.buy_order_cancel_after:
                    continue

                if not order_no:
                    print(f"[StrategyManager] 매수취소 불가 - 주문번호 없음: {code}")
                    continue

                code_name = self.get_code_name(code)

                result = self.kiwoom.send_order(
                    "send_buy_cancel_order",
                    "3001",
                    3,              # 매수취소
                    code,
                    remain_qty,     # 남아 있는 수량만 취소
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
                        f"[매수 미체결 취소요청] {code_name}({code}) "
                        f"잔량 {remain_qty}주 / 전략: {strategy_name}"
                    )
                    print(
                        f"[StrategyManager] 매수 미체결 취소 요청 완료: "
                        f"{code} / 잔량 {remain_qty}"
                    )

                else:
                    send_message(
                        f"[매수 미체결 자동취소 실패] {code_name}({code}) "
                        f"result={result}"
                    )

            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(error_msg)

    def cleanup_cancelled_buy_positions(self):
        """
        매수 취소 요청 이후 실제 결과를 확인한다.

        - 일부라도 체결되어 잔고가 있으면 전략 DB 유지
        - 미체결 주문에서도 사라지고 잔고도 없으면
        전량 미체결 취소 완료로 판단하여 전략 DB 삭제
        """
        for code, cancel_info in list(self.pending_buy_cancellations.items()):
            try:
                # 아직 미체결 주문 목록에 남아 있으면 취소 처리 중
                if code in self.kiwoom.order:
                    cancel_info["missing_confirmations"] = 0
                    continue

                # 미체결 목록에서 사라졌고 실제 보유가 있으면 일부 체결된 상태
                if code in self.kiwoom.balance:
                    quantity = int(self.kiwoom.balance[code].get("보유수량", 0) or 0)

                    if quantity > 0:
                        self.pending_buy_cancellations.pop(code, None)

                        print(
                            f"[StrategyManager] 일부 체결 후 잔량 취소 완료 - "
                            f"포지션 DB 유지: {code} / 보유수량 {quantity}"
                        )
                        send_message(
                            f"[일부 체결 후 잔량 취소 완료] "
                            f"{self.get_code_name(code)}({code}) "
                            f"보유수량 {quantity}주 / 포지션 유지"
                        )
                        continue

                # 조회 반영 지연 가능성이 있으므로 2회 연속 확인 후 삭제
                cancel_info["missing_confirmations"] = (
                    cancel_info.get("missing_confirmations", 0) + 1
                )

                if cancel_info["missing_confirmations"] < 2:
                    continue

                delete_position_strategy(code)
                self.pending_buy_cancellations.pop(code, None)

                print(
                    f"[StrategyManager] 전량 미체결 매수취소 완료 - "
                    f"포지션 DB 삭제: {code}"
                )
                send_message(
                    f"[전량 미체결 매수취소 완료] "
                    f"{self.get_code_name(code)}({code}) 포지션 DB 삭제"
                )

            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(error_msg)

    def cleanup_sold_positions(self):
        """
        실제 체결 포지션 중 현재 계좌 잔고에 더 이상 없는 종목을 정리한다.

        안전장치:
        - 일부 매도 후 잔고가 남아 있으면 삭제하지 않는다.
        - 매도 미체결 수량이 남아 있으면 삭제하지 않는다.
        - 잔고 없음이 2회 연속 확인된 경우에만 포지션 DB를 삭제한다.
        """
        tracked_codes = set(get_filled_position_codes())

        # 이미 삭제 대기 중인데 DB 대상에서 사라진 코드가 있으면 상태 정리
        for code in list(self.pending_sold_position_deletions.keys()):
            if code not in tracked_codes:
                self.pending_sold_position_deletions.pop(code, None)

        for code in tracked_codes:
            try:
                # 실제 잔고에 있으면 아직 보유 중이므로 삭제 대기 상태 초기화
                if code in self.kiwoom.balance:
                    self.pending_sold_position_deletions.pop(code, None)
                    continue

                order_info = self.kiwoom.order.get(code, {})
                order_type = str(order_info.get("주문구분", "") or "").strip()
                remain_qty = int(order_info.get("미체결수량", 0) or 0)

                # 매도 주문이 아직 일부 미체결 상태이면 삭제하지 않음
                if order_type == "매도" and remain_qty > 0:
                    self.pending_sold_position_deletions.pop(code, None)
                    continue

                missing_count = (
                    self.pending_sold_position_deletions.get(code, 0) + 1
                )
                self.pending_sold_position_deletions[code] = missing_count

                if missing_count < self.sold_position_delete_confirmations:
                    print(
                        f"[StrategyManager] 잔고 없음 1차 확인 - "
                        f"포지션 삭제 대기: {code}"
                    )
                    continue

                delete_position_strategy(code)
                self.kiwoom.order.pop(code, None)
                self.pending_sold_position_deletions.pop(code, None)

                print(
                    f"[StrategyManager] 잔고 없음 2회 확인 - "
                    f"포지션 DB 삭제: {code}"
                )
                send_message(
                    f"[전량 매도 완료] "
                    f"{self.get_code_name(code)}({code}) 포지션 DB 삭제"
                )

            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(error_msg)
    
    def step(self):
        if not self.is_running or not self.is_initialized:
            return

        try:
            now = time.time()

            # 뉴스는 장중 여부와 관계없이 10분 간격으로 확인
            self.send_one_economy_news_if_needed()

            # 장외: 전략 검사는 하지 않고, 5분마다 상태 정리만 수행
            if not check_transaction_open():
                self.set_timer_interval(self.market_closed_interval)

                self.refresh_order_and_balance_state(allow_buy_cancel=False)

                # 실제 거래일 장 종료 후에는 완성된 일봉 데이터를 하루 한 번 반영한다.
                # 주말·휴장일·장 시작 전에는 check_transaction_closed()가 False이므로 실행되지 않는다.
                self.refresh_daily_price_data_after_close()

                print(
                    "[StrategyManager] 장 시간이 아니므로 "
                    "주문/잔고 상태만 확인하고 대기합니다."
                )
                return

            # 장중: 종목 검사는 1초마다 진행
            self.set_timer_interval(self.market_open_interval)

            # 미체결/잔고 조회는 매초가 아니라 60초마다 수행
            if now - self.last_order_sync_at >= self.order_sync_interval:
                self.refresh_order_and_balance_state(allow_buy_cancel=True)

            # 직전 잔고 조회가 실패했다면 안전을 위해 신규 전략 주문을 수행하지 않는다.
            if not self.account_state_safe:
                print(
                    "[StrategyManager] 잔고 상태가 확인되지 않아 "
                    "전략 검사를 건너뜁니다."
                )
                return
            
            # 예수금이 미확인 상태이거나, 마지막 정상 동기화 후 5분이 지났으면 다시 조회
            if (
                not self.deposit_state_safe
                or now - self.last_deposit_sync_at >= self.deposit_sync_interval
            ):
                if not self.sync_deposit_from_kiwoom():
                    print(
                        "[StrategyManager] 예수금 상태가 확인되지 않아 "
                        "전략 검사를 건너뜁니다."
                    )
                    return

                # 정상 예수금으로 갱신된 시점의 일별 자산 기록도 갱신
                self.save_performance_snapshot()

            else:
                self.sync_deposit_to_strategies()

            if not self.universe_codes:
                print("[StrategyManager] 검사할 유니버스가 없습니다.")
                return

            code = self.universe_codes[self.current_index]
            code_name = self.get_code_name(code)

            print(
                f"[Manager] "
                f"[{self.current_index + 1}/{len(self.universe_codes)}_{code_name}]"
            )

            for strategy in self.strategies:
                if not strategy.is_init_success:
                    continue

                print(f"[Manager] {strategy.strategy_name} 검사: {code}")

                strategy.deposit = self.deposit
                ordered = strategy.check_code(code)

                if ordered:
                    self.deposit = strategy.deposit
                    self.sync_deposit_to_strategies()
                    print(f"[StrategyManager] 주문 발생 후 예수금 반영: {self.deposit}")

            self.current_index += 1

            if self.current_index >= len(self.universe_codes):
                self.current_index = 0
                print("[StrategyManager] 전체 유니버스 1회 검사 완료")

        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)