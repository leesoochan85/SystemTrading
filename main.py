from api.Kiwoom import *
from strategy.RSIStrategy import *
from strategy.BandTrendStrategy import *
from strategy.BandReversionStrategy import *
from strategy.HighBreakoutStrategy import *
from strategy.ORBStrategy import *
from strategy.StrategyManager import StrategyManager
import sys

app=QApplication(sys.argv)
kiwoom = Kiwoom()
# kospi_code_list=kiwoom.get_code_list_by_market("0")
# print(kospi_code_list)
# for code in kospi_code_list:
#     code_name=kiwoom.get_master_code_name(code)
#     print(code, code_name)

# kosdaq_code_list=kiwoom.get_code_list_by_market("10")
# print(kosdaq_code_list)
# for code in kosdaq_code_list:
#     code_name=kiwoom.get_master_code_name(code)
#     print(code, code_name)

# kiwoom.get_account_number()

# df=kiwoom.get_price_data("005930")
# print(df)

# deposit = kiwoom.get_deposit()

# order_result = kiwoom.send_order('send_buy_order', "1001", 1,"094280", 1, 12500, "00")
# print(order_result)

# orders = kiwoom.get_order()
# print(orders)

# position = kiwoom.get_balance()
# print(position)

# kiwoom.set_real_reg("1000", "", get_fid("장운영구분"), "0")
# fids = get_fid("체결시간")
# codes = '005930;007700;000660;'
# kiwoom.set_real_reg("1000", codes, fids, "0")

print("RSI 객체 생성 시작")
rsi_strategy = RSIStrategy(kiwoom, auto_init=False)
print("RSI 객체 생성 완료")

print("HighBreakout 객체 생성 시작")
high_breakout_strategy = HighBreakoutStrategy(kiwoom, auto_init=False)
print("HighBreakout 객체 생성 완료")

print("BandTrend 객체 생성 시작")
band_trend_strategy = BandTrendStrategy(kiwoom, auto_init=False)
print("BandTrend 객체 생성 완료")

print("BandReversion 객체 생성 시작")
band_reversion_strategy = BandReversionStrategy(kiwoom, auto_init=False)
print("BandReversion 객체 생성 완료")

print("ORB 객체 생성 시작")
orb_strategy = ORBStrategy(kiwoom, auto_init=False)
print("ORB 객체 생성 완료")

print("전략 매니저 시작")
manager = StrategyManager(
    kiwoom,
    [
        orb_strategy,
        rsi_strategy,
        high_breakout_strategy,
        band_trend_strategy,
        band_reversion_strategy,
    ],
)
manager.start()
print("전략 매니저 시작 완료")

app.exec_()
