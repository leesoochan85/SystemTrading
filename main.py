from api.Kiwoom import *
from strategy.RSIStrategy import *
from strategy.BandStrategy import *
import sys

app=QApplication(sys.argv)
# kiwoom = Kiwoom()
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

# rsi_strategy = RSIStrategy()
# rsi_strategy.start()

# band_combine_strategy = BandCombine()
# band_combine_strategy.start()

band_trebd_strategy = BandTrendStrategy()
band_trebd_strategy.start()


app.exec_()
