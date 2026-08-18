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
    update_latest_sell_trade_costs,
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

        # 주식체결 실시간 틱을 전략이 직접 구독할 수 있도록 한다.
        # ORBStrategy는 09:00~09:04:59 틱만 집계해 첫 5분봉을 정확히 확정한다.
        self.realtime_listeners = []

        self.stock_realtime_fid_names = {
            "현재가",
            "시가",
            "누적거래량",
            "(최우선)매수호가",
        }
        
        # 잔고 조회 안전성 관리
        self.last_balance_query_success = False
        self._waiting_for_balance_response = False
        self.balance_request_timeout_ms = 10000  # 10초
        
        # 미체결 주문 조회 안전성 관리
        self.last_order_query_success = False
        self._waiting_for_order_response = False
        self.order_request_timeout_ms = 10000  # 10초
        
        # 자금 조회 안전성 관리
        # last_deposit:
        #   기존 전략 코드와의 호환성을 위해 "신규 매수에 사용할 금액"을 저장한다.
        #   실제 값은 100%종목주문가능금액이다.
        self.last_deposit = None

        # 화면 및 성과 기록용 원본 값
        self.last_cash_deposit = None              # 예수금
        self.last_general_orderable_cash = None    # 주문가능금액
        self.last_orderable_cash = None            # 100%종목주문가능금액
        self.last_d2_estimated_deposit = None      # d+2추정예수금

        self.last_deposit_query_success = False
        self._waiting_for_deposit_response = False
        self.deposit_request_timeout_ms = 10000  # 10초
        
        # 지연 응답이 새 요청을 덮어쓰지 않도록 요청별 식별자 관리
        self._safe_tr_request_seq = 0
        self._active_order_rqname = None
        self._active_balance_rqname = None
        self._active_deposit_rqname = None

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

    def _next_safe_rqname(self, prefix):
        """
        같은 TR을 반복 요청하더라도 응답을 구분할 수 있도록
        짧은 고유 요청명을 생성한다.
        """
        self._safe_tr_request_seq += 1

        if self._safe_tr_request_seq > 999999:
            self._safe_tr_request_seq = 1

        return f"{prefix}_{self._safe_tr_request_seq:06d}"

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

        elif rqname.startswith("DEP_"):  # 자금 데이터 수신
            # 현재 대기 중인 자금 요청의 응답만 반영한다.
            if (
                rqname != self._active_deposit_rqname
                or not self._waiting_for_deposit_response
            ):
                print(f"[Kiwoom] 지연된 자금 응답 무시: {rqname}")
                return

            raw_cash_deposit = self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode,
                rqname,
                0,
                "예수금",
            )

            raw_general_orderable_cash = self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode,
                rqname,
                0,
                "주문가능금액",
            )

            raw_orderable_cash = self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode,
                rqname,
                0,
                "100%종목주문가능금액",
            )

            raw_d2_estimated_deposit = self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode,
                rqname,
                0,
                "d+2추정예수금",
            )

            cash_deposit = self._to_int(raw_cash_deposit)
            general_orderable_cash = self._to_int(raw_general_orderable_cash)
            orderable_cash = self._to_int(raw_orderable_cash)
            d2_estimated_deposit = self._to_int(raw_d2_estimated_deposit)

            # 자동매수는 미수 사용 없이 현금 100% 기준 금액만 사용한다.
            # 음수값이 들어오는 경우 신규 매수를 허용하지 않는다.
            orderable_cash = max(0, orderable_cash)

            self.last_cash_deposit = cash_deposit
            self.last_general_orderable_cash = general_orderable_cash
            self.last_orderable_cash = orderable_cash
            self.last_d2_estimated_deposit = d2_estimated_deposit

            # 기존 전략 코드에서는 last_deposit / get_deposit() 반환값을
            # 매수 예산으로 사용하므로 100%종목주문가능금액을 전달한다.
            self.last_deposit = orderable_cash
            self.tr_data = orderable_cash

            self.last_deposit_query_success = True
            self._waiting_for_deposit_response = False
            self._active_deposit_rqname = None

            print(
                "[Kiwoom] 자금 조회 정상 완료: "
                f"예수금 {cash_deposit:,}원 / "
                f"주문가능금액 {general_orderable_cash:,}원 / "
                f"100%종목주문가능금액 {orderable_cash:,}원 / "
                f"D+2추정예수금 {d2_estimated_deposit:,}원"
            )

        elif rqname.startswith("ORD_"):  # 미체결 주문 수신
            # 현재 대기 중인 미체결 조회의 응답만 반영한다.
            if (
                rqname != self._active_order_rqname
                or not self._waiting_for_order_response
            ):
                print(f"[Kiwoom] 지연된 미체결 주문 응답 무시: {rqname}")
                return

            received_order = {}

            for i in range(tr_data_cnt):
                code = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "종목코드"
                )
                code_name = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "종목명"
                )
                order_number = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "주문번호"
                )
                order_status = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "주문상태"
                )
                order_quantity = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "주문수량"
                )
                order_price = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "주문가격"
                )
                current_price = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "현재가"
                )
                order_type = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "매매구분"
                )
                left_quantity = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "미체결수량"
                )
                executed_quantity = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "체결량"
                )
                ordered_at = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "시간"
                )
                fee = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "당일매매수수료"
                )
                tax = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "당일매매세금"
                )

                code = str(code).strip()

                if code.startswith("A"):
                    code = code[1:]

                code = code.zfill(6)
                code_name = str(code_name).strip()

                order_number = str(order_number).strip()
                order_number = str(int(order_number)) if order_number else ""

                order_status = str(order_status).strip()
                order_quantity = self._to_int(order_quantity)
                order_price = self._to_int(order_price)
                current_price = self._to_int(
                    str(current_price).strip().lstrip("+").lstrip("-")
                )
                order_type = str(order_type).strip().lstrip("+").lstrip("-")
                left_quantity = self._to_int(left_quantity)
                executed_quantity = self._to_int(executed_quantity)
                ordered_at = str(ordered_at).strip()
                fee = self._to_int(fee)
                tax = self._to_int(tax)

                received_order[code] = {
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
                    "시간": ordered_at,
                    "당일매매수수료": fee,
                    "당일매매세금": tax,
                }

            # 정상 응답이 도착한 경우에만 기존 미체결 주문 목록을 교체한다.
            # tr_data_cnt == 0은 정상적으로 미체결 주문이 없는 상태이다.
            self.order = received_order
            self.tr_data = self.order
            self.last_order_query_success = True
            self._waiting_for_order_response = False
            self._active_order_rqname = None

            print(
                f"[Kiwoom] 미체결 주문 조회 정상 완료: "
                f"{len(self.order)}건"
            )
        
        elif rqname.startswith("BAL_"):  # 잔고 데이터 수신
            # 현재 대기 중인 잔고 조회의 응답만 반영한다.
            if (
                rqname != self._active_balance_rqname
                or not self._waiting_for_balance_response
            ):
                print(f"[Kiwoom] 지연된 잔고 응답 무시: {rqname}")
                return

            received_balance = {}

            for i in range(tr_data_cnt):
                code = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "종목번호"
                )
                code_name = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "종목명"
                )
                quantity = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "보유수량"
                )
                purchase_price = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "매입가"
                )
                return_rate = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "수익률(%)"
                )
                current_price = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "현재가"
                )
                total_purchase_price = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "총매입가"
                )
                available_quantity = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, i, "매매가능수량"
                )

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

                received_balance[code] = {
                    "종목명": code_name,
                    "보유수량": quantity,
                    "매입가": purchase_price,
                    "수익률": return_rate,
                    "현재가": current_price,
                    "매입금액": total_purchase_price,
                    "매매가능수량": available_quantity,
                }

            # 정상 응답이 도착한 경우에만 기존 잔고를 새 값으로 교체한다.
            # tr_data_cnt == 0인 경우도 정상적인 전량 미보유 상태이므로 {}로 교체한다.
            self.balance = received_balance
            self.tr_data = self.balance
            self.last_balance_query_success = True
            self._waiting_for_balance_response = False
            self._active_balance_rqname = None

            print(
                f"[Kiwoom] 잔고 조회 정상 완료: "
                f"{len(self.balance)}종목"
            )
        
        self.tr_event_loop.exit()
        time.sleep(0.5)

    def get_price_data(self, code):
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            "opt10081_req",
            "opt10081",
            0,
            "0001",
        )

        self.tr_event_loop.exec_()

        ohlcv = self.tr_data

        while self.has_next_tr_data:
            # 동일 종목의 추가 일봉 페이지 연속조회 제한 방지
            time.sleep(4)

            self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
            self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
            self.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                "opt10081_req",
                "opt10081",
                2,
                "0001",
            )

            self.tr_event_loop.exec_()

            for key, val in self.tr_data.items():
                ohlcv[key][-1:] = val

        df = pd.DataFrame(
            ohlcv,
            columns=["open", "high", "low", "close", "volume"],
            index=ohlcv["date"],
        )

        return df[::-1]
    
    def get_deposit(self):
        """
        신규 매수에 사용할 현금 100% 기준 주문가능금액을 조회한다.

        - 반환값: 100%종목주문가능금액
        - 예수금, 일반 주문가능금액, D+2추정예수금은
        별도 속성에 저장하여 상태 출력 및 성과 계산에 사용한다.
        - 조회 실패 또는 응답 지연 시 마지막 정상 주문가능금액을 유지한다.
        """
        previous_deposit = self.last_deposit

        self.last_deposit_query_success = False
        self._waiting_for_deposit_response = True

        # 모의투자 opw00001은 반복 조회 시 최초 rqname으로 응답하는 경우가 있으므로
        # 자금 조회도 고정 요청명을 사용한다.
        deposit_rqname = "DEP_REQ"
        self._active_deposit_rqname = deposit_rqname
        
        try:
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "계좌번호",
                self.account_number,
            )
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "비밀번호입력매체구분",
                "00",
            )
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "조회구분",
                "2",
            )

            request_result = self.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                deposit_rqname,
                "opw00001",
                0,
                "2003",
            )

            # 키움 환경에 따라 정상 요청 반환값은 None 또는 0일 수 있음
            if request_result not in (None, 0):
                self._waiting_for_deposit_response = False
                self._active_deposit_rqname = None
                self.last_deposit_query_success = False
                self.last_deposit = previous_deposit

                print(
                    f"[Kiwoom] 예수금 조회 요청 실패 "
                    f"result={request_result} - 마지막 정상 예수금 유지"
                )
                return previous_deposit

            QTimer.singleShot(
                self.deposit_request_timeout_ms,
                lambda requested_rqname=deposit_rqname:
                    self._on_deposit_query_timeout(requested_rqname),
            )

            self.tr_event_loop.exec_()

            if not self.last_deposit_query_success:
                self.last_deposit = previous_deposit

                print(
                    "[Kiwoom] 예수금 응답 미확인 - "
                    "마지막 정상 예수금 유지"
                )

                return previous_deposit

            return self.last_deposit

        except Exception as e:
            self._waiting_for_deposit_response = False
            self._active_deposit_rqname = None
            self.last_deposit_query_success = False
            self.last_deposit = previous_deposit

            error_message = (
                f"[Kiwoom] 예수금 조회 예외 - "
                f"마지막 정상 예수금 유지: {e}"
            )
            print(error_message)
            send_message(error_message)

            return previous_deposit
    
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
                executed_quantity = self._safe_int(order_info.get("체결량", 0))
                unit_executed_quantity = self._safe_int(order_info.get("단위체결량", 0))

                left_quantity = self._safe_int(order_info.get("미체결수량", 0))

                executed_price = self._safe_int(
                    order_info.get("단위체결가", 0)
                    or order_info.get("체결가", 0)
                )
                code_name = order_info.get("종목명", code)
                
                fee_raw = order_info.get("당일매매 수수료")
                tax_raw = order_info.get("당일매매세금")

                fee = (
                    self._safe_int(fee_raw)
                    if fee_raw not in (None, "")
                    else None
                )
                tax = (
                    self._safe_int(tax_raw)
                    if tax_raw not in (None, "")
                    else None
                )

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

                # position_strategy 및 체결 이력에는 이번 체결 이벤트의 신규 체결량만 반영한다.
                # 누적 체결량을 더하면 부분 체결 때 DB 수량이 실제 잔고보다 커진다.
                fill_quantity_for_db = unit_executed_quantity
                
                print(
                    f"[Kiwoom] 체결 DB 반영 확인: {code_name}({code}) / "
                    f"주문번호={order_no} / 체결번호={fill_no} / "
                    f"누적체결량={executed_quantity} / "
                    f"단위체결량={unit_executed_quantity} / "
                    f"DB반영수량={fill_quantity_for_db} / "
                    f"미체결수량={left_quantity}"
                )
                
                if fill_no and fill_quantity_for_db > 0 and executed_price > 0:
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
                        quantity=fill_quantity_for_db,
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
                        fee=fee,
                        tax=tax,
                    )

                if order_status == "체결" or (executed_quantity > 0 and left_quantity == 0):
                    send_message(
                        f"체결완료: {code_name}({code}) "
                        f"{order_info.get('주문구분', '')} "
                        f"이번 체결 {unit_executed_quantity}주 / "
                        f"누적 체결 {executed_quantity}주 "
                        f"{executed_price}원"
                    )
                    
                order_type = str(order_info.get("주문구분", "")).strip().lstrip("+").lstrip("-")

        elif int(s_gubun) == 1:
            print("* 잔고 출력(self.balance)")
            print(self.balance)

            if code and code in self.balance:
                balance_info = self.balance[code]

                fee_raw = balance_info.get("당일매매 수수료")
                tax_raw = balance_info.get("당일매매세금")

                fee = (
                    self._safe_int(fee_raw)
                    if fee_raw not in (None, "")
                    else None
                )
                tax = (
                    self._safe_int(tax_raw)
                    if tax_raw not in (None, "")
                    else None
                )

                if fee is not None and tax is not None:
                    update_latest_sell_trade_costs(
                        code=code,
                        fee=fee,
                        tax=tax,
                    )
            
    def get_order(self):
        """
        미체결 주문 조회에 실패하거나 응답이 지연되면
        기존 주문 목록을 유지한다.

        정상적으로 미체결 주문이 0건인 응답은 성공으로 처리되어
        self.order = {}로 갱신된다.

        주의:
        모의투자 opt10075 반복 조회에서 요청명 불일치로 응답을 놓치는
        상황을 방지하기 위해 고정 요청명 ORD_REQ를 사용한다.
        """
        previous_order = {
            code: dict(info)
            for code, info in self.order.items()
        }

        old_order_meta = {
            code: {
                "strategy_name": info.get("strategy_name"),
                "order_time": info.get("order_time"),
            }
            for code, info in previous_order.items()
        }

        self.last_order_query_success = False
        self._waiting_for_order_response = True

        # 모의투자 opt10075도 반복 조회 시 이전/최초 rqname으로
        # 응답하는 경우가 있을 수 있으므로 고정 요청명을 사용한다.
        order_rqname = "ORD_REQ"
        self._active_order_rqname = order_rqname

        try:
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "계좌번호",
                self.account_number,
            )
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "전체종목구분",
                "0",
            )
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "체결구분",
                "0",
            )
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "매매구분",
                "0",
            )

            request_result = self.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                order_rqname,
                "opt10075",
                0,
                "2001",
            )

            # 환경에 따라 정상 요청 반환값이 None 또는 0일 수 있으므로 둘 다 허용
            if request_result not in (None, 0):
                self._waiting_for_order_response = False
                self._active_order_rqname = None
                self.order = previous_order
                self.tr_data = self.order

                print(
                    f"[Kiwoom] 미체결 주문 조회 요청 실패 "
                    f"result={request_result} - 기존 주문 유지"
                )
                return self.order

            QTimer.singleShot(
                self.order_request_timeout_ms,
                lambda requested_rqname=order_rqname:
                    self._on_order_query_timeout(requested_rqname),
            )

            self.tr_event_loop.exec_()

            if not self.last_order_query_success:
                self.order = previous_order
                self.tr_data = self.order

                print(
                    "[Kiwoom] 미체결 주문 응답 미확인 - "
                    "기존 주문 유지"
                )

                return self.order

            # 정상 응답으로 교체된 주문 중 계속 남아 있는 주문에는
            # 전략명과 주문 시작 시각을 다시 연결한다.
            for code, old_info in old_order_meta.items():
                if code not in self.order:
                    continue

                if old_info.get("strategy_name"):
                    self.order[code]["strategy_name"] = old_info["strategy_name"]

                if old_info.get("order_time"):
                    self.order[code]["order_time"] = old_info["order_time"]

            return self.order

        except Exception as e:
            self._waiting_for_order_response = False
            self._active_order_rqname = None
            self.last_order_query_success = False
            self.order = previous_order

            error_message = (
                f"[Kiwoom] 미체결 주문 조회 예외 - "
                f"기존 주문 유지: {e}"
            )
            print(error_message)
            send_message(error_message)

            return self.order
    
    def _on_order_query_timeout(self, requested_rqname):
        """
        현재 진행 중인 미체결 요청이 제한시간 안에 완료되지 않은 경우에만
        타임아웃으로 처리한다.
        """
        if (
            requested_rqname != self._active_order_rqname
            or not self._waiting_for_order_response
        ):
            return

        self._waiting_for_order_response = False
        self._active_order_rqname = None
        self.last_order_query_success = False

        print(
            "[Kiwoom] 미체결 주문 조회 타임아웃 - "
            "기존 주문 정보를 유지합니다."
        )

        self.tr_event_loop.exit()

    def _on_balance_query_timeout(self, requested_rqname):
        """
        현재 진행 중인 잔고 요청이 제한시간 안에 완료되지 않은 경우에만
        타임아웃으로 처리한다.
        """
        if (
            requested_rqname != self._active_balance_rqname
            or not self._waiting_for_balance_response
        ):
            return

        self._waiting_for_balance_response = False
        self._active_balance_rqname = None
        self.last_balance_query_success = False

        print(
            "[Kiwoom] 잔고 조회 타임아웃 - "
            "기존 잔고 정보를 유지합니다."
        )

        self.tr_event_loop.exit()    
    
    def _on_deposit_query_timeout(self, requested_rqname):
        """
        현재 진행 중인 예수금 요청이 제한시간 안에 완료되지 않은 경우에만
        타임아웃으로 처리한다.
        """
        if (
            requested_rqname != self._active_deposit_rqname
            or not self._waiting_for_deposit_response
        ):
            return

        self._waiting_for_deposit_response = False
        self._active_deposit_rqname = None
        self.last_deposit_query_success = False

        print(
            "[Kiwoom] 예수금 조회 타임아웃 - "
            "마지막 정상 예수금을 유지합니다."
        )

        self.tr_event_loop.exit()

    def get_balance(self):
        """
        잔고 조회에 실패하거나 응답이 지연되면 기존 잔고를 유지한다.

        정상적인 빈 잔고 응답은 성공으로 처리되어
        self.balance = {}로 갱신된다.

        주의:
        모의투자 opw00018은 반복 조회 시 최초 rqname으로 응답하는 경우가 있어
        잔고 조회는 고정 요청명 BAL_REQ를 사용한다.
        """
        previous_balance = {
            code: dict(info)
            for code, info in self.balance.items()
        }

        self.last_balance_query_success = False
        self._waiting_for_balance_response = True

        # 기존:
        # balance_rqname = self._next_safe_rqname("BAL")
        #
        # 모의투자 환경에서 반복 잔고 조회 응답이 최초 요청명으로 돌아오는 현상을
        # 피하기 위해 고정 요청명을 사용한다.
        balance_rqname = "BAL_REQ"
        self._active_balance_rqname = balance_rqname

        try:
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "계좌번호",
                self.account_number,
            )
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "비밀번호입력매체구분",
                "00",
            )
            self.dynamicCall(
                "SetInputValue(QString, QString)",
                "조회구분",
                "1",
            )

            request_result = self.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                balance_rqname,
                "opw00018",
                0,
                "2002",
            )

            if request_result not in (None, 0):
                self._waiting_for_balance_response = False
                self._active_balance_rqname = None
                self.balance = previous_balance
                self.tr_data = self.balance

                print(
                    f"[Kiwoom] 잔고 조회 요청 실패 result={request_result} - "
                    "기존 잔고 유지"
                )
                return self.balance

            QTimer.singleShot(
                self.balance_request_timeout_ms,
                lambda requested_rqname=balance_rqname:
                    self._on_balance_query_timeout(requested_rqname),
            )

            self.tr_event_loop.exec_()

            if not self.last_balance_query_success:
                self.balance = previous_balance
                self.tr_data = self.balance

                print(
                    "[Kiwoom] 잔고 응답 미확인 - "
                    "기존 잔고 유지"
                )

            return self.balance

        except Exception as e:
            self._waiting_for_balance_response = False
            self._active_balance_rqname = None
            self.last_balance_query_success = False
            self.balance = previous_balance
            self.tr_data = self.balance

            error_message = (
                f"[Kiwoom] 잔고 조회 예외 - 기존 잔고 유지: {e}"
            )
            print(error_message)
            send_message(error_message)

            return self.balance
    
    def set_stock_realtime_fid_names(self, fid_names):
        normalized = {
            str(name).strip()
            for name in (fid_names or [])
            if str(name).strip()
        }

        if not normalized:
            raise ValueError(
                "주식체결 실시간 FID 목록은 비어 있을 수 없습니다."
            )

        self.stock_realtime_fid_names = normalized

        print(
            "[Kiwoom] 주식체결 실시간 FID 적용: "
            f"{len(normalized)}개 / {sorted(normalized)}"
        )

    def add_realtime_listener(self, listener):
        """주식체결 실시간 정보 수신 시 호출할 콜백을 등록한다."""
        if listener not in self.realtime_listeners:
            self.realtime_listeners.append(listener)

    def remove_realtime_listener(self, listener):
        if listener in self.realtime_listeners:
            self.realtime_listeners.remove(listener)

    def set_real_reg(self, str_screen_no, str_code_list, #실시간 체결 정보 얻어올 종목 전달
                      str_fid_list, #체결 정보 중 제공받을 항목에 해당하는 fid
                      str_opt_type #실시간 등록 타입. 최초/화면 교체는 "0", 같은 화면 추가는 "1"
                      ):
        # SetRealReg는 TR 조회가 아니므로 종목별 조회 지연을 둘 필요가 없다.
        # 반환값을 상위 전략에 전달해 대규모 실시간 등록 실패를 감지할 수 있게 한다.
        return self.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            str_screen_no,
            str_code_list,
            str_fid_list,
            str_opt_type,
        )

    def set_real_remove(self, str_screen_no, str_code="ALL"):
        """SetRealReg로 등록한 실시간 종목을 화면 단위 또는 종목 단위로 해제한다."""
        return self.dynamicCall(
            "SetRealRemove(QString, QString)",
            str_screen_no,
            str_code,
        )

    def _on_receive_real_data(self, s_code, real_type, real_data): #등록 후 응답 받아오는 슬롯
        if real_type=="장시작시간":
            pass        #일단 많이 사용하지 않아서 구현하지 않음. 필요하면 구현할 예정
        
        elif real_type == "주식체결":
            tick_data = {}

            for fid_name in sorted(
                self.stock_realtime_fid_names
            ):
                raw_value = self.dynamicCall(
                    "GetCommRealData(QString, int)",
                    s_code,
                    get_fid(fid_name),
                )

                if fid_name == "체결시간":
                    value = str(raw_value).strip()
                else:
                    value = abs(
                        self._safe_int(raw_value)
                    )

                tick_data[fid_name] = value

            if (
                s_code
                not in self.universe_realtime_transaction_info
            ):
                self.universe_realtime_transaction_info[
                    s_code
                ] = {}

            self.universe_realtime_transaction_info[
                s_code
            ].update(tick_data)

            for listener in list(
                self.realtime_listeners
            ):
                try:
                    listener(
                        s_code,
                        dict(tick_data),
                    )
                except Exception as e:
                    print(
                        "[Kiwoom] 실시간 리스너 오류: "
                        f"{e}"
                    )

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