from datetime import datetime
import math
import time
import traceback

import numpy as np
import pandas as pd
from PyQt5.QtCore import QThread

from util.db_helper import (
    check_table_exists, execute_sql, insert_df_to_db,
    init_position_strategy_table, save_position_strategy, 
    get_position_strategy, delete_position_strategy,
)
from util.make_up_universe import get_universe
from util.notifier import send_message
from util.const import get_fid
from util.time_helper import (
    check_transaction_closed,
    check_transaction_open,
)


class RSIStrategy(QThread):
    def __init__(self, kiwoom, auto_init=True):
        super().__init__()
        self.strategy_name = "RSIStrategy"
        self.kiwoom = kiwoom
        self.universe = {}
        self.deposit = 0
        self.is_init_success = False

        if auto_init:
            self.init_strategy()

    def init_strategy(self):
        try:
            init_position_strategy_table()
            self.kiwoom.get_order()
            self.kiwoom.get_balance()
            self.check_and_get_universe()
            self.check_and_get_price_data()
            
            self.deposit = self.kiwoom.get_deposit()
            self.set_universe_real_time()
            self.is_init_success = True

            print("[RSIStrategy] 초기화 완료")
            send_message("[RSIStrategy] 초기화 완료")

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
        for idx, code in enumerate(self.universe.keys(), start=1):
            print(f"{idx}/{len(self.universe)}) {code}")

            if not check_table_exists(self.strategy_name, code):
                price_df = self.kiwoom.get_price_data(code)
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

        codes = list(self.universe.keys())
        chunk_size = 90

        for i in range(0, len(codes), chunk_size):
            screen_no = str(1000 + (i // chunk_size))
            code_chunk = ";".join(codes[i:i + chunk_size])
            self.kiwoom.set_real_reg(screen_no, code_chunk, fids, "0")

    def get_today_price_df(self, code):
        if code not in self.universe or "price_df" not in self.universe[code]:
            return None

        if code not in self.kiwoom.universe_realtime_transaction_info:
            return None

        rt = self.kiwoom.universe_realtime_transaction_info[code]
        today = datetime.now().strftime("%Y%m%d")

        df = self.universe[code]["price_df"].copy()
        df.loc[today] = [
            rt["시가"],
            rt["고가"],
            rt["저가"],
            rt["현재가"],
            rt["누적거래량"],
        ]

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    def get_rsi_df(self, code):
        df = self.get_today_price_df(code)

        if df is None or len(df) < 60:
            return None

        df = df.copy()

        diff = df["close"].diff(1)
        up = np.where(diff > 0, diff, 0)
        down = np.where(diff < 0, -diff, 0)

        au = pd.Series(up, index=df.index).rolling(window=14).mean()
        ad = pd.Series(down, index=df.index).rolling(window=14).mean()

        df["RSI(14)"] = au / (au + ad) * 100
        df["ma20"] = df["close"].rolling(window=20, min_periods=1).mean()
        df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()
        df["close_14days_ago"] = df["close"].shift(14)
        df["price_diff_14d_pct"] = (
            (df["close"] - df["close_14days_ago"]) / df["close_14days_ago"] * 100
        )

        return df

    def check_sell_signal(self, code):
        if code not in self.kiwoom.balance:
            return False

        df = self.get_rsi_df(code)
        if df is None:
            return False

        latest = df.iloc[-1]
        rsi = latest["RSI(14)"]
        close = latest["close"]

        if pd.isna(rsi) or pd.isna(close):
            return False

        balance_info = self.kiwoom.balance[code]
        purchase_price = balance_info.get("매입가", balance_info.get("매입단가", 0))

        if purchase_price <= 0:
            return False

        return bool(rsi > 75 and close > purchase_price)

    def order_sell(self, code):
        quantity = self.kiwoom.balance[code]["보유수량"]
        ask = self.kiwoom.universe_realtime_transaction_info[code]["(최우선)매도호가"]
        if quantity < 1:
            return False

        result = self.kiwoom.send_order("send_sell_order", "2001", 2, code, quantity, ask, "00")

        if result == 0:
            send_message(f"RSI 매도 주문: {self.universe[code]['code_name']} {quantity}주 {ask}원")
            self.kiwoom.order[code] = {
                "주문구분": "매도",
                "미체결수량": quantity,
                "strategy_name": self.strategy_name,
            }
            return True
        else:
            send_message(f"RSI 매도 주문 실패: {self.universe[code]['code_name']} result={result}")
            return False
        
    def check_buy_signal_and_order(self, code):
        if not check_transaction_open():
            return False

        if code in self.kiwoom.balance:
            return False

        if code in self.kiwoom.order and self.kiwoom.order[code].get("미체결수량", 0) > 0:
            return False

        df = self.get_rsi_df(code)
        if df is None:
            return False

        latest = df.iloc[-1]

        rsi = latest["RSI(14)"]
        ma20 = latest["ma20"]
        ma60 = latest["ma60"]
        close = latest["close"]
        price_diff = latest["price_diff_14d_pct"]

        if pd.isna(rsi) or pd.isna(ma20) or pd.isna(ma60) or pd.isna(close) or pd.isna(price_diff):
            return False

        if not (ma20 > ma60 and rsi < 25 and price_diff < -2):
            return False

        if (self.get_balance_count() + self.get_buy_order_count()) >= 10:
            return False

        rt = self.kiwoom.universe_realtime_transaction_info.get(code)
        if not rt:
            return False

        bid = rt["(최우선)매수호가"]
        if bid <= 0:
            return False

        budget = self.deposit / (10 - (self.get_balance_count() + self.get_buy_order_count()))
        quantity = math.floor(budget / bid)

        if quantity < 1:
            return False

        amount = quantity * bid
        estimated_amount = math.floor(amount * 1.00015)

        if self.deposit < estimated_amount:
            return False

        result = self.kiwoom.send_order("send_buy_order", "2001", 1, code, quantity, bid, "00")

        if result == 0:
            self.deposit -= estimated_amount

            save_position_strategy(
                code=code,
                code_name=self.universe[code]["code_name"],
                strategy_name=self.strategy_name,
                quantity=quantity,
                buy_price=bid,
            )

            send_message(f"RSI매수 주문: {self.universe[code]['code_name']} {quantity}주 {bid}원")
            self.kiwoom.order[code] = {
                "주문구분": "매수",
                "미체결수량": quantity,
                "strategy_name": self.strategy_name,
            }
            return True

        send_message(f"RSI매수 주문 실패: {self.universe[code]['code_name']} result={result}")
        return False

    def get_balance_count(self):
        balance_count = len(self.kiwoom.balance)
        for code in self.kiwoom.order.keys():
            if (
                code in self.kiwoom.balance 
                and self.kiwoom.order[code].get("주문구분") == "매도"
                and self.kiwoom.order[code].get("미체결수량") == 0
            ):
                balance_count -= 1
        return balance_count

    def get_buy_order_count(self):
        buy_order_count = 0
        for code in self.kiwoom.order.keys():
            if (code not in self.kiwoom.balance 
                and self.kiwoom.order[code].get("주문구분") == "매수"
                and self.kiwoom.order[code].get("미체결수량", 0) > 0):
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
    #     print(f"[{self.strategy_name}] run 시작 / is_init_success={self.is_init_success}")
    #     while self.is_init_success:
    #         try:
    #             if not check_transaction_open():
    #                 print("장 시간이 아니므로 대기합니다.")
    #                 send_message("장 시간이 아니므로 대기합니다.")
    #                 time.sleep(60)
    #                 continue
                
    #             # 장중 잔고 변동을 주기적으로 반영한다.
    #             self.kiwoom.get_balance()

    #             for idx, code in enumerate(self.universe.keys(), start=1):
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
