import time
import traceback
from PyQt5.QtCore import QObject, QTimer

from util.time_helper import check_transaction_open
from util.notifier import send_message
from util.db_helper import (
    init_position_strategy_table,
    init_sent_news_table,
    is_news_sent,
    save_sent_news,
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

        self.universe_codes = []
        self.current_index = 0

        self.timer = QTimer()
        self.timer.timeout.connect(self.step)

        self.last_news_sent_at = 0
        self.news_send_interval = 600  # 10분

    def start(self):
        print("[StrategyManager] start 호출")
        self.is_running = True

        init_sent_news_table()

        # 프로그램 시작 직후 뉴스 1개 먼저 전송
        self.send_one_economy_news_if_needed()

        self.initialize_strategies()

        # 1초마다 한 종목씩 검사
        self.timer.start(1000)
        print("[StrategyManager] 타이머 시작")

    def get_code_name(self, code):
        for strategy in self.strategies:
            if code in strategy.universe:
                return strategy.universe[code].get("code_name", code)
        return code

    def sync_deposit_from_kiwoom(self):
        self.deposit = self.kiwoom.get_deposit()
        self.last_deposit_sync_at = time.time()
        self.sync_deposit_to_strategies()
        print(f"[StrategyManager] 예수금 동기화: {self.deposit}")

    def sync_deposit_to_strategies(self):
        for strategy in self.strategies:
            strategy.deposit = self.deposit

    def initialize_strategies(self):
        print("[StrategyManager] 전략 초기화 시작")

        try:
            init_position_strategy_table()
            init_sent_news_table()

            print("[StrategyManager] 미체결 조회 시작")
            self.kiwoom.get_order()
            print("[StrategyManager] 미체결 조회 완료")

            print("[StrategyManager] 잔고 조회 시작")
            self.kiwoom.get_balance()
            print("[StrategyManager] 잔고 조회 완료")

            print("[StrategyManager] 예수금 조회 시작")
            self.sync_deposit_from_kiwoom()
            print("[StrategyManager] 예수금 조회 완료")

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

    def step(self):
        if not self.is_running or not self.is_initialized:
            return

        try:
            # 장중이 아니어도 뉴스는 전송
            self.send_one_economy_news_if_needed()

            # 전략 검사는 장중에만 실행
            if not check_transaction_open():
                print("[StrategyManager] 장 시간이 아니므로 전략 검사는 대기합니다.")
                return

            if not self.universe_codes:
                print("[StrategyManager] 검사할 유니버스가 없습니다.")
                return

            # 한 바퀴 시작 시점에만 계좌 상태 갱신
            if self.current_index == 0:
                print("[StrategyManager] 루프 시작 - 미체결 조회 전")
                self.kiwoom.get_order()
                print("[StrategyManager] 미체결 조회 완료")

                print("[StrategyManager] 잔고 조회 전")
                self.kiwoom.get_balance()
                print("[StrategyManager] 잔고 조회 완료")

                if time.time() - self.last_deposit_sync_at >= self.deposit_sync_interval:
                    print("[StrategyManager] 예수금 동기화 전")
                    self.sync_deposit_from_kiwoom()
                    print("[StrategyManager] 예수금 동기화 완료")
                else:
                    self.sync_deposit_to_strategies()

            code = self.universe_codes[self.current_index]
            code_name = self.get_code_name(code)

            print(f"[Manager] [{self.current_index + 1}/{len(self.universe_codes)}_{code_name}]")

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