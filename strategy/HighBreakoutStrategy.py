from datetime import datetime
import math
import time
import traceback

import numpy as np
import pandas as pd
from PyQt5.QtCore import QThread

from util.const import get_fid
from util.db_helper import (
    check_table_exists,
    execute_sql,
    insert_df_to_db,
    save_position_strategy,
    get_position_strategy,
    delete_position_strategy,
)
from util.make_up_universe import get_universe
from util.notifier import send_message
from util.time_helper import check_adjacent_transaction_closed_for_buying, check_transaction_closed, check_transaction_open


class HighBreakoutStrategy(QThread):
    """
    신고가 돌파 전략

    매수 조건:
      - 현재가가 직전 BREAKOUT_WINDOW일 동안의 최고가를 돌파
      - 오늘 누적거래량이 직전 20일 평균 거래량 이상
      - 보유/미체결 포함 최대 MAX_POSITIONS개까지만 보유

    매도 조건:
      - 현재가가 20일 이동평균선 아래로 내려감
      - 또는 매입가 대비 STOP_LOSS_PCT 이하로 하락

    중요:
      - 매번 새 유니버스를 만들더라도 현재 보유 종목은 반드시 self.universe에 합친다.
      - 따라서 보유 종목은 실시간 등록 대상이 되고, run()에서 매도 조건을 계속 확인한다.
    """

    BREAKOUT_WINDOW = 60
    VOLUME_AVG_WINDOW = 20
    MAX_POSITIONS = 10
    BUY_FEE_RATE = 0.00035
    STOP_LOSS_PCT = -5.0

    def __init__(self, kiwoom, auto_init=True):
        super().__init__()
        self.strategy_name = "HighBreakoutStrategy"
        self.kiwoom = kiwoom
        self.universe = {}
        self.deposit = 0
        self.is_init_success = False

        if auto_init:
            self.init_strategy()

    def init_strategy(self):
        try:
            # 보유 종목을 유니버스에 강제 포함해야 하므로 잔고를 먼저 가져온다.
            self.kiwoom.get_order()
            self.kiwoom.get_balance()

            self.check_and_get_universe()
            self.check_and_get_price_data()

            self.deposit = self.kiwoom.get_deposit()
            self.set_universe_real_time()
            self.is_init_success = True
            send_message("[HighBreakoutStrategy] 초기화 완료")
        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    def check_and_get_universe(self):
        universe_df = get_universe(return_df=True).copy()
        universe_df["종목코드"] = universe_df["종목코드"].astype(str).str.strip().str.zfill(6)
        universe_df = universe_df[universe_df["종목코드"].str.fullmatch(r"\d{6}", na=False)]
        universe_df = universe_df.drop_duplicates(subset=["종목코드"])

        self.universe = {
            code: {"code_name": name, "holding": False}
            for code, name in zip(universe_df["종목코드"], universe_df["종목명"])
        }

        # 핵심 수정: 현재 보유 중인 종목은 새 유니버스 조건과 관계없이 강제 포함한다.
        for code, balance_info in self.kiwoom.balance.items():
            code = str(code).strip().zfill(6)
            code_name = balance_info.get("종목명") or self.kiwoom.get_master_code_name(code) or code
            if code not in self.universe:
                self.universe[code] = {"code_name": code_name, "holding": True}
                print(f"[보유종목 유니버스 추가] {code} {code_name}")
            else:
                self.universe[code]["holding"] = True

        now = datetime.now().strftime("%Y%m%d")
        save_df = pd.DataFrame(
            {
                "code": list(self.universe.keys()),
                "code_name": [v["code_name"] for v in self.universe.values()],
                "holding": [v.get("holding", False) for v in self.universe.values()],
                "created_at": [now] * len(self.universe),
            }
        )
        insert_df_to_db(self.strategy_name, "universe", save_df)
        print(self.universe)

    def check_and_get_price_data(self):
        for idx, code in enumerate(list(self.universe.keys()), start=1):
            print(f"{idx}/{len(self.universe)}) {code}")

            if not check_table_exists(self.strategy_name, code):
                price_df = self.kiwoom.get_price_data(code)
                time.sleep(4)
                insert_df_to_db(self.strategy_name, code, price_df)
                self.universe[code]["price_df"] = price_df
                continue

            if check_transaction_closed():
                sql = "select max('{}') from '{}'".format("date", code)
                cur = execute_sql(self.strategy_name, sql)
                last_date = cur.fetchone()
                now = datetime.now().strftime("%Y%m%d")
                if not last_date or last_date[0] != now:
                    price_df = self.kiwoom.get_price_data(code)
                    insert_df_to_db(self.strategy_name, code, price_df)

            sql = "select * from '{}'".format(code)
            cur = execute_sql(self.strategy_name, sql)
            cols = [column[0] for column in cur.description]
            price_df = pd.DataFrame.from_records(data=cur.fetchall(), columns=cols)
            price_df = price_df.set_index("index")
            self.universe[code]["price_df"] = price_df

    def set_universe_real_time(self):
        # 키움 실시간 등록은 종목 수가 많아질 수 있으므로 화면번호를 나누어 등록한다.
        codes = list(self.universe.keys())
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

        chunk_size = 90
        for i in range(0, len(codes), chunk_size):
            screen_no = str(1300 + (i // chunk_size))
            code_chunk = ";".join(codes[i : i + chunk_size])
            self.kiwoom.set_real_reg(screen_no, code_chunk, fids, "0")
            print(f"[실시간 등록] screen={screen_no}, count={len(codes[i : i + chunk_size])}")

    def get_today_price_df(self, code):
        if code not in self.universe or "price_df" not in self.universe[code]:
            return None
        if code not in self.kiwoom.universe_realtime_transaction_info:
            return None

        rt = self.kiwoom.universe_realtime_transaction_info[code]
        today = datetime.now().strftime("%Y%m%d")
        df = self.universe[code]["price_df"].copy()
        df.loc[today] = [rt["시가"], rt["고가"], rt["저가"], rt["현재가"], rt["누적거래량"]]

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def get_signal_df(self, code):
        df = self.get_today_price_df(code)
        min_len = max(self.BREAKOUT_WINDOW, self.VOLUME_AVG_WINDOW, 20) + 2
        if df is None or len(df) < min_len:
            return None

        df = df.copy()
        df["prev_highest_high"] = df["high"].shift(1).rolling(window=self.BREAKOUT_WINDOW).max()
        df["volume_ma20"] = df["volume"].shift(1).rolling(window=self.VOLUME_AVG_WINDOW).mean()
        df["ma20"] = df["close"].rolling(window=20).mean()
        return df

    def check_buy_signal_and_order(self, code):
        if check_adjacent_transaction_closed_for_buying():
            return False
        if not check_transaction_open():
            return False
        if code in self.kiwoom.balance:
            return False
        if code in self.kiwoom.order and self.kiwoom.order[code].get("미체결수량", 0) > 0:
            return False

        df = self.get_signal_df(code)
        if df is None:
            return False

        latest = df.iloc[-1]
        current_price = latest["close"]
        prev_highest_high = latest["prev_highest_high"]
        volume = latest["volume"]
        volume_ma20 = latest["volume_ma20"]

        if pd.isna(prev_highest_high) or pd.isna(volume_ma20):
            return False

        # 신고가 돌파: 오늘 현재가가 직전 N일 최고가를 넘어섰을 때
        if not (current_price > prev_highest_high and volume >= volume_ma20):
            return False

        if (self.get_balance_count() + self.get_buy_order_count()) >= self.MAX_POSITIONS:
            return False

        rt = self.kiwoom.universe_realtime_transaction_info.get(code)
        if not rt:
            return False

        bid = rt["(최우선)매수호가"]
        if bid <= 0:
            return False

        remain_slots = self.MAX_POSITIONS - (self.get_balance_count() + self.get_buy_order_count())
        budget = self.deposit / remain_slots
        quantity = math.floor(budget / bid)
        if quantity < 1:
            return False

        amount = quantity * bid
        estimated_amount = math.floor(amount * (1 + self.BUY_FEE_RATE))
        if self.deposit < estimated_amount:
            return False

        result = self.kiwoom.send_order("send_buy_order", "2004", 1, code, quantity, bid, "00")
        if result == 0:
            self.deposit -= estimated_amount

            save_position_strategy(
                code=code,
                code_name=self.universe[code]["code_name"],
                strategy_name=self.strategy_name,
                quantity=quantity,
                buy_price=bid,
            )
            send_message(
                f"[신고가돌파 매수] {self.universe[code]['code_name']}({code}) "
                f"{quantity}주 {bid}원 / 직전{self.BREAKOUT_WINDOW}일 최고가 {int(prev_highest_high)}원 돌파"
            )
            self.kiwoom.order[code] = {"주문구분": "매수", 
                                       "미체결수량": quantity, 
                                       "strategy_name": self.strategy_name,
                                       "order_time": time.time(),}
            return True

        send_message(f"[신고가돌파 매수 실패] {self.universe[code]['code_name']}({code}) result={result}")
        return False

    def check_sell_signal(self, code):
        if code not in self.kiwoom.balance:
            return False

        df = self.get_signal_df(code)
        if df is None:
            return False

        latest = df.iloc[-1]
        current_price = latest["close"]
        ma20 = latest["ma20"]
        if pd.isna(ma20):
            return False

        balance_info = self.kiwoom.balance[code]
        purchase_price = balance_info.get("매입가", balance_info.get("매입단가", 0))
        if purchase_price <= 0:
            return False

        return_rate = (current_price - purchase_price) / purchase_price * 100

        # 신고가 돌파 전략의 기본 청산: 20일선 이탈 또는 -5% 손절
        return bool(current_price < ma20 or return_rate <= self.STOP_LOSS_PCT)

    def order_sell(self, code):
        balance_info = self.kiwoom.balance[code]
        quantity = balance_info.get("매매가능수량", 0) or balance_info.get("보유수량", 0)
        if quantity < 1:
            return False

        rt = self.kiwoom.universe_realtime_transaction_info.get(code)
        if not rt:
            return False

        ask = rt["(최우선)매도호가"]
        if ask <= 0:
            return False

        result = self.kiwoom.send_order("send_sell_order", "2004", 2, code, quantity, ask, "00")
        if result == 0:
            send_message(f"[신고가돌파 매도] {self.universe[code]['code_name']}({code}) {quantity}주 {ask}원")
            self.kiwoom.order[code] = {
                "주문구분": "매도", 
                "미체결수량": quantity,
                "strategy_name": self.strategy_name, 
            }
            return True
        send_message(f"[신고가돌파 매도 실패] {self.universe[code]['code_name']}({code}) result={result}")
        return False

    def get_balance_count(self):
        balance_count = len(self.kiwoom.balance)
        for code in self.kiwoom.order.keys():
            if (
                code in self.kiwoom.balance
                and self.kiwoom.order[code].get("주문구분") == "매도"
                and self.kiwoom.order[code].get("미체결수량", 0) == 0
            ):
                balance_count -= 1
        return balance_count

    def get_buy_order_count(self):
        buy_order_count = 0
        for code in self.kiwoom.order.keys():
            if (
                code not in self.kiwoom.balance
                and self.kiwoom.order[code].get("주문구분") == "매수"
                and self.kiwoom.order[code].get("미체결수량", 0) > 0
            ):
                buy_order_count += 1
        return buy_order_count

    def check_code(self, code):
        if code not in self.universe:
            return False

        if code in self.kiwoom.order and self.kiwoom.order[code].get("미체결수량", 0) > 0:
            return False

        if code in self.kiwoom.balance:
            owner_strategy = get_position_strategy(code)

            if owner_strategy != self.strategy_name:
                return False

            if self.check_sell_signal(code):
                return self.order_sell(code)

            return False

        return self.check_buy_signal_and_order(code)

    # def run(self):
    #     print("[HighBreakoutStrategy] run 시작")
    #     while self.is_init_success:
    #         try:
    #             if not check_transaction_open():
    #                 print("장 시간이 아니므로 대기합니다.")
    #                 time.sleep(60)
    #                 continue

    #             # 장중 잔고 변동을 주기적으로 반영한다.
    #             self.kiwoom.get_balance()

    #             for idx, code in enumerate(list(self.universe.keys()), start=1):
    #                 print(f"[{self.strategy_name}] [{idx}/{len(self.universe)}_{self.universe[code]['code_name']}]")
    #                 time.sleep(0.5)

    #                 if code in self.kiwoom.order and self.kiwoom.order[code].get("미체결수량", 0) > 0:
    #                     continue

    #                 if code in self.kiwoom.balance:
    #                     owner_strategy = get_position_strategy(code)

    #                     if owner_strategy != self.strategy_name:
    #                         continue

    #                     if self.check_sell_signal(code):
    #                         self.order_sell(code)
    #                 else:
    #                     self.check_buy_signal_and_order(code)

    #         except Exception:
    #             error_msg = traceback.format_exc()
    #             print(error_msg)
    #             send_message(error_msg)
    #             time.sleep(5)
