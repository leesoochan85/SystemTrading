from PyQt5.QAxContainer import *
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
import time
import pandas as pd
from util.const import *
from util.notifier import send_message
from util.db_helper import (
    get_position_detail,
    save_order_event,
    save_order_log,
    save_trade_fill,
)

class Kiwoom(QAxWidget):
    def __init__(self):
        super().__init__()
        self._make_kiwoom_instance()
        self._set_signal_slots()
        self.comm_connect()
        self.account_number = self.get_account_number()
        self.tr_event_loop = QEventLoop()
        self.order={}
        self.balance={}
        self.universe_realtime_transaction_info = {}
        self.pending_order_strategy = {}

    def _make_kiwoom_instance(self):
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")

    def _set_signal_slots(self):
        self.OnEventConnect.connect(self._login_slot)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveMsg.connect(self._on_receive_msg)
        self.OnReceiveChejanData.connect(self._on_chejan_slot)
        self.OnReceiveRealData.connect(self._on_receive_real_data)

    def _login_slot(self, err_code):
        if err_code == 0:
            print("로그인 성공")
        else:
            print("로그인 실패")

        self.login_event_loop.exit()

    def comm_connect(self):
        self.dynamicCall("CommConnect()")

        self.login_event_loop = QEventLoop()
        self.login_event_loop.exec_()

    def get_account_number(self, tag="ACCNO"):
        account_list = self.dynamicCall("GetLoginInfo(QString)", tag)
        account_number = account_list.split(';')[0]
        print(account_number)
        return account_number
    
    def get_code_list_by_market(self, market_type):
        code_list = self.dynamicCall("GetCodeListByMarket(QString)", market_type)
        code_list = code_list.split(';')[:-1]
        return code_list
    
    def get_master_code_name(self, code):
        code_name = self.dynamicCall("GetMasterCodeName(QString)", code)
        return code_name

    def _on_receive_tr_data(self, screen_no, rqname, trcode, record_name, next, unused1,unused2, unused3, unused4):
        print ("[Kiwoom] _on_receive_tr_data is called {} / {} / {}".format(screen_no,rqname, trcode))
        tr_data_cnt = self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)

        if next== '2':
            self.has_next_tr_data=True
        else:
            self.has_next_tr_data=False
        
        if rqname == "opt10081_req": #일봉 데이터 수신
            ohlcv ={'date': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': []}

            for i in range(tr_data_cnt):
                date = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "일자")
                open = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "시가")
                high = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "고가")
                low = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "저가")
                close = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "현재가")
                volume = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i,"거래량")

                ohlcv['date'].append(date.strip())
                ohlcv['open'].append(int(open.strip()))
                ohlcv['high'].append(int(high.strip()))
                ohlcv['low'].append(int(low.strip()))
                ohlcv['close'].append(int(close.strip()))
                ohlcv['volume'].append(int(volume.strip()))

            self.tr_data=ohlcv

        elif rqname == "opw00001_req": #예수금 데이터 수신
            deposit = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, 0, "예수금")
            self.tr_data = self._to_int (deposit)
            print (self.tr_data)

        elif rqname == "opt10075_req":   # 미체결 주문 수신
            for i in range(tr_data_cnt):
                code = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "종목코드")
                code_name = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "종목명")
                order_number = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "주문번호")
                order_status = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "주문상태")
                order_quantity = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "주문수량")
                order_price = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "주문가격")
                current_price = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "현재가")
                order_type = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "매매구분")
                left_quantity = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "미체결수량")
                executed_quantity = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "체결량")
                orderd_at = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "시간")
                fee = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "당일매매수수료")
                tax = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "당일매매세금")

                code = code.strip()
                if code.startswith("A"):
                    code = code[1:]
                code = code.zfill(6)

                code_name = code_name.strip()

                order_number = order_number.strip()
                order_number = str(int(order_number)) if order_number else ""

                order_status = order_status.strip()
                order_quantity = self._to_int(order_quantity)
                order_price = self._to_int(order_price)
                current_price = self._to_int(current_price.strip().lstrip("+").lstrip("-"))
                order_type = order_type.strip().lstrip("+").lstrip("-")
                left_quantity = self._to_int(left_quantity)
                executed_quantity = self._to_int(executed_quantity)
                orderd_at = orderd_at.strip()
                fee = self._to_int(fee)
                tax = self._to_int(tax)

                self.order[code] = {
                    "종목코드": code,
                    "종목명": code_name,
                    "주문번호": order_number,
                    "주문상태": order_status,
                    "주문수량": order_quantity,
                    "주문가격": order_price,
                    "현재가": current_price,
                    "매매구분": order_type,
                    "주문구분": order_type,
                    "미체결수량": left_quantity,
                    "체결량": executed_quantity,
                    "시간": orderd_at,
                    "당일매매수수료": fee,
                    "당일매매세금": tax,
                }

            # 중요: 미체결 주문이 0건이어도 항상 tr_data 세팅
            self.tr_data = self.order

        elif rqname == "opw00018_req": #잔고 데이터 수신
            for i in range(tr_data_cnt):
                code = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "종목번호")
                code_name = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "종목명")
                quantity = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "보유수량")
                purchase_price = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "매입가")
                return_rate = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "수익률(%)")
                current_price = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "현재가")
                total_purchase_price = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "총매입가")
                available_quantity = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "매매가능수량") 

                code = code.strip()

                if code.startswith("A"):
                    code = code[1:]

                code = code.zfill(6)
                code_name = code_name.strip()

                quantity = self._to_int(quantity)
                purchase_price = self._to_int(purchase_price)
                return_rate = self._to_float(return_rate)
                current_price = self._to_int(current_price)
                total_purchase_price = self._to_int(total_purchase_price)
                available_quantity = self._to_int(available_quantity)

                self.balance[code] = {'종목명' : code_name,
                                      '보유수량': quantity,
                                      '매입가': purchase_price,
                                      '수익률': return_rate,
                                      '현재가': current_price,
                                      '매입금액': total_purchase_price,
                                      '매매가능수량': available_quantity
                                      }
                self.tr_data=self.balance

        self.tr_event_loop.exit()
        time.sleep(0.5)

    def get_price_data(self, code):
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
        self.dynamicCall("CommRqData(QString, QString, int, QString)", "opt10081_req", "opt10081", 0, "0001")

        self.tr_event_loop.exec_()

        ohlcv = self.tr_data
        
        while self.has_next_tr_data:
            self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
            self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
            self.dynamicCall("CommRqData(QString,QString, int, QString)", "opt10081_req", "opt10081", 2, "0001")

            self.tr_event_loop.exec_()

            for key, val in self.tr_data.items():
                ohlcv[key][-1:]=val
        df = pd.DataFrame(ohlcv,columns=['open', 'high', 'low', 'close', 'volume'], index=ohlcv['date'])

        return df[::-1]
    
    def get_deposit(self):
        self.dynamicCall("SetInputValue(QString, QString)", "계좌번호", self.account_number)
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호입력매체구분", "00")
        self.dynamicCall("SetInputValue(QString, QString)", "조회구분", "2")
        self.dynamicCall("CommRqData(QString, QString, int, QString)", "opw00001_req", "opw00001", 0, "0002")
        self.tr_event_loop.exec_()
        return self.tr_data
    
    def send_order(self, rqname, #요청에 대한 별명, ex) 'send_buy_order'
                   screen_no, order_type,#매수/매도/취소 주문 구분, ex) 1: 매수, 2: 매도, 3: 매수취소, 4: 매도취소
                     code, #매매할 종목코드, ex) "005930"
                     order_quantity, #주문 수량
                     order_price, order_classification,#거래구분 ex) "00": 지정가, "03": 시장가, "05": 조건부지정가, "10": 최유리지정가, "20": 최우선지정가
                       origin_order_number="", #정정 주문의 주문번호 // 신규주문시는 빈 값
                       strategy_name=""
                       ):
        code = str(code).zfill(6)
        order_type_text = {1: "매수", 2: "매도", 3: "매수취소", 4: "매도취소"}.get(order_type, str(order_type))
        request_id = f"REQ_{time.time_ns()}_{code}"
        if strategy_name:
            self.pending_order_strategy[code] = strategy_name

        order_result = self.dynamicCall("SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
                                         [rqname, screen_no, self.account_number, order_type, code, order_quantity, order_price, order_classification, origin_order_number])

        save_order_event(
            event_key=request_id, request_id=request_id, original_order_no=origin_order_number,
            code=code, code_name=self.get_master_code_name(code), order_type=order_type_text,
            strategy_name=strategy_name, order_quantity=order_quantity, order_price=order_price,
            remaining_quantity=order_quantity,
            order_status="전송요청" if order_result == 0 else f"전송실패({order_result})",
            event_type="REQUESTED" if order_result == 0 else "FAILED",
        )
        return order_result
    
    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        print("[Kiwoom] _on_receive_msg is called {} / {} / {} / {}".format(screen_no, rqname, trcode, msg))

    def _on_chejan_slot(self, s_gubun, n_item_cnt, s_fid_list):
        print("[Kiwoom] _on_chejan_slot is called {} / {} / {}".format(s_gubun, n_item_cnt, s_fid_list))

        code = None
        
        for fid in s_fid_list.split(";"):
            if fid in FID_CODES:
                raw_code = str(self.dynamicCall("GetChejanData(int)", "9001")).strip()

                if raw_code.startswith("A"):
                    code = raw_code[1:]
                else:
                    code = raw_code

                code = code.zfill(6)

                data = self.dynamicCall("GetChejanData(int)", fid)
               
                raw_data = self.dynamicCall("GetChejanData(int)", fid)
                data = str(raw_data).strip().lstrip('+').lstrip('-').replace(",", "")
                item_name = FID_CODES[fid]

                numeric_fields = {
                    "주문수량", "주문가격", "미체결수량", "체결누계금액", "원주문번호",
                    "체결가", "체결량", "현재가", "(최우선)매도호가", "(최우선)매수호가",
                    "단위체결가", "단위체결량", "당일매매 수수료", "당일매매세금",
                    "보유수량", "매입단가", "총매입가", "주문가능수량"
                }

                if item_name in numeric_fields:
                    data = self._safe_int(data)
                
                item_name = FID_CODES[fid]
                print("{}: {}".format(item_name, data))

                if int(s_gubun) == 0:                    
                    if code not in self.order:
                        self.order[code]={}

                    self.order[code].update({item_name: data})

                elif int(s_gubun) == 1:
                    if code not in self.balance:
                        self.balance[code]={}

                    normalized_name = item_name
                    if item_name == "매입단가":
                        normalized_name = "매입가"
                    elif item_name == "주문가능수량":
                        normalized_name = "매매가능수량"
                        
                    self.balance[code].update({normalized_name: data})
            
        if int(s_gubun) == 0:
            print("* 주문 출력(self.order)")
            print(self.order)

            if code and code in self.order:
                order_info = self.order[code]
                order_status = order_info.get("주문상태", "")
                executed_quantity = order_info.get("체결량", 0)
                left_quantity = order_info.get("미체결수량", 0)
                executed_price = self._safe_int(order_info.get("체결가", 0))
                code_name = order_info.get("종목명", code)

                order_no = str(order_info.get("주문번호", "") or "").strip()
                fill_no = str(order_info.get("체결번호", "") or "").strip()

                order_type = (
                    str(order_info.get("주문구분", "") or "")
                    .strip()
                    .lstrip("+")
                    .lstrip("-")
                )

                strategy_name = order_info.get("strategy_name", "") or self.pending_order_strategy.get(code, "")

                save_order_log(
                    order_no=order_no,
                    code=code,
                    code_name=code_name,
                    order_type=order_type,
                    strategy_name=strategy_name,
                    order_quantity=order_info.get("주문수량", 0),
                    order_price=order_info.get("주문가격", 0),
                    remaining_quantity=left_quantity,
                    order_status=order_status,
                    filled_quantity=executed_quantity,
                    filled_price=executed_price,
                    fill_no=fill_no,
                )

                position_info = None

                if order_type == "매도":
                    position_info = get_position_detail(code)

                if fill_no and executed_quantity > 0 and executed_price > 0:
                    save_trade_fill(
                        fill_no=fill_no,
                        order_no=order_no,
                        code=code,
                        code_name=code_name,
                        order_type=order_type,
                        strategy_name=(
                            position_info["strategy_name"]
                            if position_info is not None
                            else strategy_name
                        ),
                        quantity=executed_quantity,
                        price=executed_price,
                        buy_price=(
                            position_info["buy_price"]
                            if position_info is not None
                            else None
                        ),
                        buy_date=(
                            position_info["created_at"]
                            if position_info is not None
                            else None
                        ),
                    )

                if order_status == "체결" or (executed_quantity > 0 and left_quantity == 0):
                    send_message(
                    f"체결완료: {code_name}({code}) "
                    f"{order_info.get('주문구분', '')} "
                    f"{executed_quantity}주 "
                    f"{order_info.get('체결가', 0)}원"
                )
                    
                order_type = str(order_info.get("주문구분", "")).strip().lstrip("+").lstrip("-")

        elif int(s_gubun)==1:
            print("* 잔고 출력(self.balance)")
            print(self.balance)
            
    def get_order(self):
        old_order_meta = {
            code: {
                "strategy_name": info.get("strategy_name"),
                "order_time": info.get("order_time"),
            }
            for code, info in self.order.items()
        }  
        self.order={}
        self.dynamicCall("SetInputValue(QString, QString)", "계좌번호", self.account_number)
        self.dynamicCall("SetInputValue(QString, QString)", "전체종목구분", "0")
        self.dynamicCall("SetInputValue(QString, QString)", "체결구분", "0")
        self.dynamicCall("SetInputValue(QString, QString)", "매매구분", "0")
        self.dynamicCall("CommRqData(QString, QString, int, QString)", "opt10075_req", "opt10075", 0, "0002")

        self.tr_event_loop.exec_()
        for code, old_info in old_order_meta.items():
            if code not in self.order:
                continue

            if old_info.get("strategy_name"):
                self.order[code]["strategy_name"] = old_info["strategy_name"]

            if old_info.get("order_time"):
                self.order[code]["order_time"] = old_info["order_time"]

        return self.tr_data

    def get_balance(self):
        self.balance={}

        self.dynamicCall("SetInputValue(QString, QString)", "계좌번호", self.account_number)
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호입력매체구분", "00")
        self.dynamicCall("SetInputValue(QString, QString)", "조회구분", "1")
        self.dynamicCall("CommRqData(QString, QString, int, QString)", "opw00018_req", "opw00018", 0, "0002")

        self.tr_event_loop.exec_()
        return self.tr_data
    
    def set_real_reg(self, str_screen_no, str_code_list, #실시간 체결 정보 얻어올 종목 전달
                      str_fid_list, #체결 정보 중 제공받을 항목에 해당하는 fid
                      str_opt_type #실시간 정보 등록/해제 구분, "0": 등록, "1": 해제
                      ):
        self.dynamicCall("SetRealReg(QString, QString, QString, QString)", str_screen_no, str_code_list, str_fid_list, str_opt_type)
        time.sleep(0.5)

    def _on_receive_real_data(self, s_code, real_type, real_data): #등록 후 응답 받아오는 슬롯
        if real_type=="장시작시간":
            pass        #일단 많이 사용하지 않아서 구현하지 않음. 필요하면 구현할 예정
        
        elif real_type == "주식체결":
            signed_at =self.dynamicCall("GetCommRealData(QString, int)",s_code, get_fid("체결시간"))
            close = self.dynamicCall("GetCommRealData(QString, int)", s_code, get_fid("현재가"))
            close = abs(int(close))
            high = self.dynamicCall("GetCommRealData(QString, int)", s_code, get_fid("고가"))
            high = abs(int(high))
            open = self.dynamicCall("GetCommRealData(QString, int)", s_code, get_fid("시가"))
            open = abs(int(open))
            low = self.dynamicCall("GetCommRealData(QString, int)", s_code, get_fid("저가"))
            low = abs(int(low))
            top_priority_ask = self.dynamicCall("GetCommRealData(QString, int)", s_code, get_fid("(최우선)매도호가"))
            top_priority_ask = abs(int(top_priority_ask))
            top_priority_bid = self.dynamicCall("GetCommRealData(QString, int)", s_code, get_fid("(최우선)매수호가"))
            top_priority_bid = abs(int(top_priority_bid))
            accum_volume = self.dynamicCall("GetCommRealData(QString, int)", s_code, get_fid("누적거래량"))
            accum_volume = abs(int(accum_volume))
            
            #print(s_code, signed_at, close, high, open, low, top_priority_ask, top_priority_bid, accum_volume)
            #수신 정보가 너무 많아져 프린트 주석처리
            
            if s_code not in self.universe_realtime_transaction_info:
                self.universe_realtime_transaction_info.update({s_code: {}})
            
            self.universe_realtime_transaction_info[s_code].update({
                "체결시간": signed_at,
                "현재가": close,
                "고가": high,
                "시가": open,
                "저가": low,
                "(최우선)매도호가": top_priority_ask,
                "(최우선)매수호가": top_priority_bid,
                "누적거래량": accum_volume
            })
    
    def get_fid(search_value): #const 파일에서 fid 이름으로 fid 번호 찾는 함수
        keys = [key for key, value in FID_CODES.items() if value == search_value]
        return keys[0]

    def _safe_int(self, value):
            value = str(value).strip().replace(",", "")
            if value == "":
                return 0
            try:
                return int(value)
            except ValueError:
                return 0     

    def _to_int(self, value):
        value = str(value).strip()
        if value == '':
            return 0
        return int(value)

    def _to_float(self, value):
        value = str(value).strip()
        if value == '':
            return 0.0
        return float(value)