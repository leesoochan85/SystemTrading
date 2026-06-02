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


class BandTrendStrategy(QThread):
    STOP_LOSS_PCT = -5.0

    def __init__(self, kiwoom, auto_init=True):
        super().__init__()
        self.strategy_name = "BandTrendStrategy"
        self.kiwoom = kiwoom
        self.universe = {}
        self.deposit = 0
        self.is_init_success = False

        if auto_init:
            self.init_strategy()

    def init_strategy(self):
        try:
            self.kiwoom.get_order()
            self.kiwoom.get_balance()

            self.check_and_get_universe()
            self.check_and_get_price_data()
            
            self.deposit = self.kiwoom.get_deposit()
            self.set_universe_real_time()

            self.is_init_success = True
            send_message("[BandTrendStrategy] 초기화 완료")
        except Exception:
            error_msg = traceback.format_exc()
            print(error_msg)
            send_message(error_msg)

    def check_and_get_universe(self, shared_universe_df=None):
        """공통 시총 유니버스를 적용하고, 현재 보유 종목은 매도 관리를 위해 유지한다."""
        if shared_universe_df is None:
            universe_df = get_universe(return_df=True).copy()
        else:
            universe_df = shared_universe_df.copy()
        universe_df["종목코드"] = universe_df["종목코드"].astype(str).str.strip().str.upper().str.zfill(6)
        universe_df = universe_df[universe_df["종목코드"].str.fullmatch(r"[0-9A-Za-z]{6}", na=False)]
        universe_df = universe_df.drop_duplicates(subset=["종목코드"])

        self.universe = {
            code: {"code_name": name, "holding": False}
            for code, name in zip(universe_df["종목코드"], universe_df["종목명"])
        }
        

        for code, balance_info in self.kiwoom.balance.items():
            code = str(code).strip().zfill(6)
            code_name = balance_info.get("종목명") or self.kiwoom.get_master_code_name(code) or code

            if code not in self.universe:
                self.universe[code] = {
                    "code_name": code_name,
                    "holding": True,
                }
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

    def check_and_get_price_data(self, shared_price_map=None):
        """일봉 데이터를 준비한다. Manager 실행 시에는 종목별 1회 조회된 공통 데이터를 공유한다."""
        if shared_price_map is not None:
            for code in self.universe.keys():
                normalized_code = str(code).strip().zfill(6)
                price_df = shared_price_map.get(normalized_code)

                if price_df is None:
                    raise KeyError(
                        f"[공통 일봉 누락] {self.strategy_name} / {normalized_code}"
                    )

                self.universe[code]["price_df"] = price_df.copy()

            print(
                f"[{self.strategy_name}] 공통 일봉 적용 완료: "
                f"{len(self.universe)}종목"
            )
            return

        # 전략 단독 실행 또는 기존 호환 경로: 전략 DB에서 자체적으로 일봉을 준비한다.
        for idx, code in enumerate(self.universe.keys(), start=1):
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
                    time.sleep(4) # 키움 장종료 후 일봉 연속 조회 제한 방지
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
            screen_no = str(1200 + (i // chunk_size))
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
        df.loc[today] = [rt["시가"], rt["고가"], rt["저가"], rt["현재가"], rt["누적거래량"]]
        return df

    def get_bollinger_df(self, code, mfi_period=14):
        df = self.get_today_price_df(code)
        if df is None or len(df) < 21:
            return None

        df = df.copy()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["mid20"] = df["close"].rolling(window=20).mean()
        df["std20"] = df["close"].rolling(window=20).std(ddof=0)
        df["upper"] = df["mid20"] + (df["std20"] * 2)
        df["lower"] = df["mid20"] - (df["std20"] * 2)

        # 표준 %B: (종가 - 하단) / (상단 - 하단)
        pb_denominator = df["upper"] - df["lower"]
        df["percent_b"] = np.where(
            pb_denominator != 0,
            (df["close"] - df["lower"]) / pb_denominator,
            np.nan,
        )

        df["band_width"] = np.where(
            df["mid20"] != 0,
            (df["upper"] - df["lower"]) / df["mid20"],
            np.nan,
        )

        df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
        df["money_flow"] = df["typical_price"] * df["volume"]
        tp_diff = df["typical_price"].diff()
        df["positive_money_flow"] = np.where(tp_diff > 0, df["money_flow"], 0)
        df["negative_money_flow"] = np.where(tp_diff < 0, df["money_flow"], 0)

        pos_sum = pd.Series(df["positive_money_flow"], index=df.index).rolling(window=mfi_period).sum()
        neg_sum = pd.Series(df["negative_money_flow"], index=df.index).rolling(window=mfi_period).sum()
        money_ratio = np.where(neg_sum != 0, pos_sum / neg_sum, np.nan)
        df["MFI"] = 100 - (100 / (1 + money_ratio))
        df.loc[(neg_sum == 0) & (pos_sum > 0), "MFI"] = 100
        df.loc[(neg_sum == 0) & (pos_sum == 0), "MFI"] = 50
        return df

    def is_stop_loss_triggered(self, code):
        """실시간 현재가 기준 수익률이 고정 손절선 이하인지 확인한다."""
        if code not in self.kiwoom.balance:
            return False

        balance_info = self.kiwoom.balance[code]
        purchase_price = balance_info.get("매입가", balance_info.get("매입단가", 0))
        rt = self.kiwoom.universe_realtime_transaction_info.get(code, {})
        current_price = rt.get("현재가", balance_info.get("현재가", 0))

        if purchase_price <= 0 or not current_price or current_price <= 0:
            return False

        return_rate = (current_price - purchase_price) / purchase_price * 100
        return return_rate <= self.STOP_LOSS_PCT

    def check_sell_signal(self, code):
        if code not in self.kiwoom.balance:
            return False

        # 고정 손절은 지표 계산 가능 여부와 무관하게 실시간 가격으로 먼저 판단한다.
        if self.is_stop_loss_triggered(code):
            return True

        df = self.get_bollinger_df(code)
        if df is None:
            return False

        latest = df.iloc[-1]
        pb = latest["percent_b"]
        mfi = latest["MFI"]
        if pd.isna(pb) or pd.isna(mfi):
            return False

        # 기존 추세 약화 청산 조건은 그대로 유지한다.
        return bool(pb < 0.2 and mfi < 20)

    def order_sell(self, code):
        quantity = self.kiwoom.balance[code]["보유수량"]
        if quantity < 1:
            return False

        is_stop_loss = self.is_stop_loss_triggered(code)
        if is_stop_loss:
            order_price = 0
            order_classification = "03"  # 시장가
            order_label = "[추세추종 손절 시장가 매도]"
        else:
            rt = self.kiwoom.universe_realtime_transaction_info.get(code, {})
            order_price = rt.get("(최우선)매도호가", 0)
            if order_price <= 0:
                return False
            order_classification = "00"  # 지정가
            order_label = "[추세추종 매도]"

        result = self.kiwoom.send_order(
            "send_sell_order", "2003", 2, code, quantity,
            order_price, order_classification, strategy_name=self.strategy_name
        )

        if result == 0:
            price_text = "시장가" if is_stop_loss else f"{order_price}원"
            send_message(f"{order_label} {self.universe[code]['code_name']} {quantity}주 {price_text}")
            self.kiwoom.order[code] = {
                "주문구분": "매도",
                "미체결수량": quantity,
                "strategy_name": self.strategy_name,
            }
            return True

        return False

    def check_buy_signal_and_order(self, code):
        if check_adjacent_transaction_closed_for_buying():
            return False
        if not check_transaction_open():
            return False
        if code in self.kiwoom.balance:
            return False
        if code in self.kiwoom.order and self.kiwoom.order[code].get("미체결수량", 0) > 0:
            return False

        df = self.get_bollinger_df(code)
        if df is None:
            return False
        latest = df.iloc[-1]
        pb = latest["percent_b"]
        mfi = latest["MFI"]
        if pd.isna(pb) or pd.isna(mfi):
            return False
        if not (pb > 0.8 and mfi > 80):
            return False

        if (self.get_balance_count() + self.get_buy_order_count()) >= 10:
            return False

        bid = self.kiwoom.universe_realtime_transaction_info[code]["(최우선)매수호가"]
        if bid <= 0:
            return False
        budget = self.deposit / (10 - (self.get_balance_count() + self.get_buy_order_count()))
        quantity = math.floor(budget / bid)
        if quantity < 1:
            return False

        amount = quantity * bid
        estimated_amount = math.floor(amount * 1.00035)
        if self.deposit < estimated_amount:
            return False

        result = self.kiwoom.send_order("send_buy_order", "2003", 1, code, quantity, bid, "00", strategy_name=self.strategy_name)
        if result == 0:            
            self.deposit -= estimated_amount
            save_position_strategy(
                code=code,
                code_name=self.universe[code]["code_name"],
                strategy_name=self.strategy_name,
                quantity=0,
                buy_price=0,
            )
            send_message(f"[추세추종 매수] {self.universe[code]['code_name']} {quantity}주 {bid}원")
            self.kiwoom.order[code] = {"주문구분": "매수", 
                                       "주문가격": bid,
                                       "미체결수량": quantity, 
                                       "strategy_name": self.strategy_name,
                                       "order_time": time.time(),}
            return True
        return False

    def get_balance_count(self):
        balance_count = len(self.kiwoom.balance)
        for code in self.kiwoom.order.keys():
            if code in self.kiwoom.balance and self.kiwoom.order[code].get("주문구분") == "매도" and self.kiwoom.order[code].get("미체결수량", 0) == 0:
                balance_count -= 1
        return balance_count

    def get_buy_order_count(self):
        buy_order_count = 0
        for code in self.kiwoom.order.keys():
            if code not in self.kiwoom.balance and self.kiwoom.order[code].get("주문구분") == "매수" and self.kiwoom.order[code].get("미체결수량", 0) > 0:
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
    #     print("[BandTrendStrategy] run 시작")
    #     while self.is_init_success:
    #         try:
    #             if not check_transaction_open():
    #                 print("장 시간이 아니므로 대기합니다.")
    #                 time.sleep(60)
    #                 continue

    #             # 장중 잔고 변동을 주기적으로 반영한다.
    #             self.kiwoom.get_balance()

    #             for idx, code in enumerate(self.universe.keys(), start=1):
    #                 print(f"[{self.strategy_name}] [{idx}/{len(self.universe)}_{self.universe[code]['code_name']}]")
    #                 time.sleep(0.5)
    #                 # if code in self.kiwoom.order and self.kiwoom.order[code].get("미체결수량", 0) > 0:
    #                 #     continue
    #                 # if code in self.kiwoom.balance:
    #                 #     owner_strategy =get_position_strategy(code)

    #                 #     if owner_strategy != self.strategy_name:
    #                 #         continue

    #                 #     if self.check_sell_signal(code):
    #                 #         self.order_sell(code)
    #                 # else:
    #                 #     self.check_buy_signal_and_order(code)
    #         except Exception:
    #             error_msg = traceback.format_exc()
    #             print(error_msg)
    #             send_message(error_msg)
    #             time.sleep(5)
