from datetime import datetime
import math
import time
import traceback

import numpy as np
import pandas as pd
from PyQt5.QtCore import QThread

from api.Kiwoom import Kiwoom
from util.db_helper import check_table_exists, execute_sql, insert_df_to_db
from util.make_up_universe import get_universe
from util.notifier import send_message
from util.time_helper import (
    check_adjacent_transaction_closed_for_buying,
    check_transaction_closed,
    check_transaction_open,
)


class RSIStrategy(QThread):
    def __init__(self):
        super().__init__()
        self.strategy_name = "RSIStrategy"
        self.kiwoom = Kiwoom()
        self.universe = {}
        self.deposit = 0
        self.is_init_success = False
        self.init_strategy()

    def init_strategy(self):
        try:
            self.check_and_get_universe()
            self.check_and_get_price_data()
            self.kiwoom.get_order()
            self.kiwoom.get_balance()
            self.deposit = self.kiwoom.get_deposit()
            self.set_universe_real_time()
            self.is_init_success = True
        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    def check_and_get_universe(self):
        universe_df = get_universe(return_df=True).copy()
        universe_df["종목코드"] = universe_df["종목코드"].astype(str).str.strip().str.zfill(6)
        universe_df = universe_df[universe_df["종목코드"].str.fullmatch(r"\d{6}", na=False)]
        universe_df = universe_df.drop_duplicates(subset=["종목코드"])

        now = datetime.now().strftime("%Y%m%d")
        save_df = pd.DataFrame(
            {
                "code": universe_df["종목코드"].tolist(),
                "code_name": universe_df["종목명"].tolist(),
                "created_at": [now] * len(universe_df),
            }
        )
        insert_df_to_db(self.strategy_name, "universe", save_df)

        self.universe = {
            code: {"code_name": name}
            for code, name in zip(universe_df["종목코드"], universe_df["종목명"])
        }
        print(self.universe)

    def check_and_get_price_data(self):
        for idx, code in enumerate(self.universe.keys(), start=1):
            print(f"{idx}/{len(self.universe)}) {code}")

            if check_transaction_closed() and not check_table_exists(self.strategy_name, code):
                price_df = self.kiwoom.get_price_data(code)
                insert_df_to_db(self.strategy_name, code, price_df)
                continue

            if check_transaction_closed():
                sql = "select max('{}') from '{}'".format("date", code)
                cur = execute_sql(self.strategy_name, sql)
                last_date = cur.fetchone()
                now = datetime.now().strftime("%Y%m%d")

                if not last_date or last_date[0] != now:
                    price_df = self.kiwoom.get_price_data(code)
                    insert_df_to_db(self.strategy_name, code, price_df)
            else:
                sql = "select * from '{}'".format(code)
                cur = execute_sql(self.strategy_name, sql)
                cols = [column[0] for column in cur.description]
                price_df = pd.DataFrame.from_records(data=cur.fetchall(), columns=cols)
                price_df = price_df.set_index("index")
                self.universe[code]["price_df"] = price_df

    def set_universe_real_time(self):
        fids = self.kiwoom.get_fid("체결시간")
        codes = ";".join(map(str, self.universe.keys()))
        self.kiwoom.set_real_reg("1000", codes, fids, "0")

    def check_sell_signal(self, code):
        if code not in self.kiwoom.universe_realtime_transaction_info:
            print("매도대상 확인 과정에서 아직 체결정보가 없습니다.")
            return False

        rt = self.kiwoom.universe_realtime_transaction_info[code]
        today_price_data = [rt["시가"], rt["고가"], rt["저가"], rt["현재가"], rt["누적거래량"]]

        df = self.universe[code]["price_df"].copy()
        df.loc[datetime.now().strftime("%Y%m%d")] = today_price_data

        diff = df["close"].diff(1)
        up = np.where(diff > 0, diff, 0)
        down = np.where(diff < 0, diff, 0)
        au = pd.Series(up, index=df.index.astype(str)).rolling(window=2).mean()
        ad = pd.Series(down, index=df.index.astype(str)).rolling(window=2).mean()
        df["RSI(2)"] = au / (au + ad) * 100

        purchase_price = self.kiwoom.balance[code]["매입가"]
        rsi = df.iloc[-1]["RSI(2)"]
        close = rt["현재가"]
        return bool(rsi > 80 and close > purchase_price)

    def order_sell(self, code):
        quantity = self.kiwoom.balance[code]["보유수량"]
        ask = self.kiwoom.universe_realtime_transaction_info[code]["(최우선)매도호가"]
        self.kiwoom.send_order("send_sell_order", "1001", 2, code, quantity, ask, "00")
        send_message(f"매도 주문: {self.universe[code]['code_name']} {quantity}주 {ask}원")

    def check_buy_signal_and_order(self, code):
        if not check_adjacent_transaction_closed_for_buying():
            return False
        if code not in self.kiwoom.universe_realtime_transaction_info:
            print("매수대상 확인 과정에서 아직 체결정보가 없습니다.")
            return False

        rt = self.kiwoom.universe_realtime_transaction_info[code]
        today_price_data = [rt["시가"], rt["고가"], rt["저가"], rt["현재가"], rt["누적거래량"]]

        df = self.universe[code]["price_df"].copy()
        today = datetime.now().strftime("%Y%m%d")
        df.loc[today] = today_price_data

        diff = df["close"].diff(1)
        up = np.where(diff > 0, diff, 0)
        down = np.where(diff < 0, diff, 0)
        au = pd.Series(up, index=df.index.astype(str)).rolling(window=2).mean()
        ad = pd.Series(down, index=df.index.astype(str)).rolling(window=2).mean()
        df["RSI(2)"] = au / (au + ad) * 100
        df["ma20"] = df["close"].rolling(window=20, min_periods=1).mean()
        df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()

        rsi = df.iloc[-1]["RSI(2)"]
        ma20 = df.iloc[-1]["ma20"]
        ma60 = df.iloc[-1]["ma60"]
        close = rt["현재가"]

        idx = df.index.get_loc(today) - 2
        close_2days_ago = df.iloc[idx]["close"]
        price_diff = (close - close_2days_ago) / close_2days_ago * 100

        if not (ma20 > ma60 and rsi < 20 and price_diff < -2):
            return False

        if (self.get_balance_count() + self.get_buy_order_count()) >= 10:
            return False

        budget = self.deposit / (10 - (self.get_balance_count() + self.get_buy_order_count()))
        bid = rt["(최우선)매수호가"]
        quantity = math.floor(budget / bid)
        if quantity < 1:
            return False

        amount = quantity * bid
        self.deposit = math.floor(self.deposit - amount * 1.00015)
        if self.deposit < 0:
            return False

        self.kiwoom.send_order("send_buy_order", "1001", 1, code, quantity, bid, "00")
        send_message(f"매수 주문: {self.universe[code]['code_name']} {quantity}주 {bid}원")
        self.kiwoom.order[code] = {"주문구분": "매수", "미체결수량": quantity}
        return True

    def get_balance_count(self):
        balance_count = len(self.kiwoom.balance)
        for code in self.kiwoom.order.keys():
            if code in self.kiwoom.balance and self.kiwoom.order[code]["주문구분"] == "매도" and self.kiwoom.order[code]["미체결수량"] == 0:
                balance_count -= 1
        return balance_count

    def get_buy_order_count(self):
        buy_order_count = 0
        for code in self.kiwoom.order.keys():
            if code not in self.kiwoom.balance and self.kiwoom.order[code]["주문구분"] == "매수":
                buy_order_count += 1
        return buy_order_count

    def run(self):
        while self.is_init_success:
            try:
                if not check_transaction_open():
                    print("장 시간이 아니므로 대기합니다.")
                    send_message("장 시간이 아니므로 대기합니다.")
                    time.sleep(60)
                    continue

                for idx, code in enumerate(self.universe.keys(), start=1):
                    print(f"[{idx}/{len(self.universe)}_{self.universe[code]['code_name']}]")
                    time.sleep(0.5)

                    if code in self.kiwoom.order and self.kiwoom.order[code]["미체결수량"] > 0:
                        continue
                    if code in self.kiwoom.balance:
                        if self.check_sell_signal(code):
                            self.order_sell(code)
                    else:
                        self.check_buy_signal_and_order(code)

            except Exception:
                error_msg = traceback.format_exc()
                print(error_msg)
                send_message(error_msg)
