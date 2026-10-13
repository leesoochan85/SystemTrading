"""Order approval, shared cash reservations and stale buy cancellation.

All mutable account/reservation state belongs to the manager. This adapter runs
on the caller's Qt thread and calls the existing broker interface synchronously.
"""

import time
import traceback

from util.db_helper import delete_position_strategy
from util.position_sizing import stop_risk_quantity


class OrderCoordinator:
    def __init__(self, manager, *, notify, notify_error, market_is_open):
        self.manager = manager
        self.notify = notify
        self.notify_error = notify_error
        self.market_is_open = market_is_open

    def get_reserved_buy_amount(self):
        """키움의 최신 예수금 조회에 아직 반영되지 않은 Manager 임시 예약금 합계."""
        return sum(
            max(0, int(info.get("amount", 0) or 0))
            for info in self.manager.reserved_buy_orders.values()
        )

    def get_available_buy_cash(self):
        """
        현재 Manager가 신규 주문에 사용할 수 있다고 판단하는 현금.

        self.manager.deposit은 마지막 정상 키움 조회값이고,
        그 이후 발생한 주문의 임시 예약금을 차감한다.
        """
        return max(
            0,
            int(self.manager.deposit or 0) - self.manager.get_reserved_buy_amount(),
        )

    def get_account_position_codes(self):
        """
        신규 포지션 슬롯을 사용 중인 종목을 반환한다.

        - 실제 보유
        - 매수 미체결
        - SendOrder 직전/직후 Manager 예약
        """
        codes = set()

        for raw_code, info in self.manager.kiwoom.balance.items():
            quantity = int(info.get("보유수량", 0) or 0)
            if quantity > 0:
                codes.add(str(raw_code).strip().zfill(6))

        for raw_code, info in self.manager.kiwoom.order.items():
            order_type = str(info.get("주문구분", "") or "").strip()
            remain = int(info.get("미체결수량", 0) or 0)

            if order_type == "매수" and remain > 0:
                codes.add(str(raw_code).strip().zfill(6))

        codes.update(
            str(code).strip().zfill(6)
            for code in self.manager.reserved_buy_orders.keys()
        )
        return codes

    def get_account_position_count(self):
        return len(self.manager.get_account_position_codes())

    def has_pending_buy(self, code):
        code = str(code).strip().zfill(6)

        if code in self.manager.reserved_buy_orders:
            return True

        info = self.manager.kiwoom.order.get(code, {})
        return (
            str(info.get("주문구분", "") or "").strip() == "매수"
            and int(info.get("미체결수량", 0) or 0) > 0
        )

    def _set_buy_rejection(self, code, reason):
        code = str(code).strip().zfill(6)
        self.manager.last_buy_rejections[code] = str(reason or "신규매수 거절")
        return None

    def get_buy_rejection_reason(self, code, clear=False):
        code = str(code).strip().zfill(6)
        if clear:
            return self.manager.last_buy_rejections.pop(code, None)
        return self.manager.last_buy_rejections.get(code)

    def get_event_driven_order_block_reason(self):
        if not self.manager.is_running:
            return "StrategyManager가 실행 중이 아님"
        if not self.manager.is_initialized:
            return "전략 초기화가 완료되지 않음"
        if not self.manager.account_state_safe:
            return (
                self.manager.account_state_failure_reason
                or "계좌/미체결 상태가 안전하지 않음"
            )
        if not self.manager.deposit_state_safe:
            return "주문가능금액 조회 상태가 안전하지 않음"
        if not self.manager.market_data_ready:
            return "시장 기준 데이터 준비가 완료되지 않음"
        if not self.market_is_open():
            return "정규장 시간이 아님"
        return None

    def request_buy_order(
        self,
        strategy_name,
        code,
        code_name,
        price,
        rqname,
        screen_no,
        buy_fee_rate=None,
        stop_price=None,
        risk_ratio=None,
        sell_cost_rate=0.0,
    ):
        """모든 전략의 신규 매수 요청이 통과해야 하는 단일 관문.

        기존 전략 호환성을 위해 성공 시 dict, 거절/실패 시 None을 그대로 반환한다.
        거절 사유는 get_buy_rejection_reason(code)로 별도 조회할 수 있다.
        """
        code = str(code).strip().zfill(6)
        code_name = str(code_name or code)
        price = int(price or 0)
        self.manager.last_buy_rejections.pop(code, None)

        paper_strategy = next(
            (s for s in self.manager.strategies if s.strategy_name == strategy_name),
            None,
        )
        if paper_strategy is None or getattr(paper_strategy, "paper_only", False):
            return self.manager._set_buy_rejection(
                code, "가상 전용 전략의 실전 주문은 허용되지 않음"
            )

        if strategy_name == "MomentumContractionStrategy" and not getattr(
            paper_strategy, "allow_new_buys", False
        ):
            return self.manager._set_buy_rejection(
                code, "모멘텀 실계좌 신규매수 활성 설정 꺼짐"
            )

        if buy_fee_rate is None:
            buy_fee_rate = self.manager.DEFAULT_BUY_FEE_RATE
        buy_fee_rate = max(0.0, float(buy_fee_rate))

        if price <= 0:
            return self.manager._set_buy_rejection(code, "유효한 주문가격이 없음")

        block_reason = self.manager.get_event_driven_order_block_reason()
        if block_reason:
            return self.manager._set_buy_rejection(code, block_reason)

        if code in self.manager.buy_request_in_progress:
            return self.manager._set_buy_rejection(
                code, "동일 종목의 신규매수 요청 처리 중"
            )

        self.manager.buy_request_in_progress.add(code)

        try:
            balance_info = self.manager.kiwoom.balance.get(code, {})
            if int(balance_info.get("보유수량", 0) or 0) > 0:
                return self.manager._set_buy_rejection(
                    code, "이미 계좌에 보유 중인 종목"
                )

            if self.manager.has_pending_buy(code):
                return self.manager._set_buy_rejection(
                    code, "동일 종목 매수 미체결/예약 주문 존재"
                )

            current_position_count = self.manager.get_account_position_count()
            if current_position_count >= self.manager.MAX_ACCOUNT_POSITIONS:
                reason = (
                    f"계좌 최대 보유/대기 {self.manager.MAX_ACCOUNT_POSITIONS}종목 도달"
                )
                print(
                    "[StrategyManager] 신규매수 거절 - "
                    f"{reason} / {code_name}({code}) / {strategy_name}"
                )
                return self.manager._set_buy_rejection(code, reason)

            total_assets = self.manager.calculate_total_assets()
            if total_assets <= 0:
                reason = "총자산 계산값이 0원 이하"
                print(
                    "[StrategyManager] 신규매수 거절 - "
                    f"{reason} / {code_name}({code})"
                )
                return self.manager._set_buy_rejection(code, reason)

            max_position_amount = int(total_assets * self.manager.MAX_POSITION_RATIO)
            available_cash = self.manager.get_available_buy_cash()
            buy_budget = min(max_position_amount, available_cash)

            if buy_budget <= 0:
                reason = "사용가능 현금 없음"
                print(
                    "[StrategyManager] 신규매수 거절 - "
                    f"{reason} / {code_name}({code}) / {strategy_name}"
                )
                return self.manager._set_buy_rejection(code, reason)

            unit_cost = price * (1.0 + buy_fee_rate)
            quantity = int(buy_budget // unit_cost)
            if strategy_name == "MomentumContractionStrategy":
                if stop_price is None or risk_ratio is None:
                    return self.manager._set_buy_rejection(
                        code, "모멘텀 실매수 손절/위험 설정 누락"
                    )
                quantity = min(
                    quantity,
                    stop_risk_quantity(
                        total_assets,
                        price,
                        stop_price,
                        risk_ratio=risk_ratio,
                        buy_fee_rate=buy_fee_rate,
                        sell_cost_rate=sell_cost_rate,
                    ),
                )

            if quantity <= 0:
                reason = (
                    f"예산 한도로 1주 매수 불가 "
                    f"(가격 {price:,}원 / 가용 {buy_budget:,}원)"
                )
                print(
                    "[StrategyManager] 신규매수 거절 - "
                    f"{reason} / {code_name}({code})"
                )
                return self.manager._set_buy_rejection(code, reason)

            estimated_amount = int(quantity * price * (1.0 + buy_fee_rate))
            if estimated_amount <= 0:
                return self.manager._set_buy_rejection(code, "예상 주문금액 계산 실패")

            self.manager.reserved_buy_orders[code] = {
                "amount": estimated_amount,
                "strategy_name": strategy_name,
                "quantity": quantity,
                "price": price,
                "reserved_at": time.time(),
            }

            result = self.manager.kiwoom.send_order(
                rqname,
                screen_no,
                1,
                code,
                quantity,
                price,
                "00",
                strategy_name=strategy_name,
            )

            if result != 0:
                self.manager.reserved_buy_orders.pop(code, None)
                reason = f"Kiwoom SendOrder 실패 result={result}"
                print(
                    "[StrategyManager] 매수 주문 전송 실패 - "
                    f"{code_name}({code}) / {strategy_name} / result={result}"
                )
                return self.manager._set_buy_rejection(code, reason)

            self.manager.kiwoom.order[code] = {
                "주문구분": "매수",
                "주문가격": price,
                "미체결수량": quantity,
                "strategy_name": strategy_name,
                "order_time": time.time(),
                "예산예약금액": estimated_amount,
            }

            self.manager.last_buy_rejections.pop(code, None)

            print(
                "[StrategyManager] 매수 승인 - "
                f"{code_name}({code}) / {strategy_name} / "
                f"{quantity}주 × {price:,}원 / "
                f"예약 {estimated_amount:,}원 / "
                f"총자산 {total_assets:,}원 / "
                f"종목한도 {max_position_amount:,}원 / "
                f"남은 공용현금 {self.manager.get_available_buy_cash():,}원"
            )

            return {
                "quantity": quantity,
                "price": price,
                "estimated_amount": estimated_amount,
                "max_position_amount": max_position_amount,
                "total_assets": total_assets,
            }

        except Exception:
            self.manager.reserved_buy_orders.pop(code, None)
            error_msg = traceback.format_exc()
            self.notify_error(
                f"[StrategyManager 매수요청 오류] "
                f"{code_name}({code}) / {strategy_name}\n{error_msg}",
                event_key=f"strategy.StrategyManager.request_buy_order:{code}",
            )
            self.manager._set_buy_rejection(
                code, "StrategyManager 매수 요청 처리 중 예외 발생"
            )
            return None

        finally:
            self.manager.buy_request_in_progress.discard(code)

    def can_event_driven_order(self):
        return bool(
            self.manager.is_running
            and self.manager.is_initialized
            and self.manager.account_state_safe
            and self.manager.deposit_state_safe
            and self.manager.market_data_ready
            and self.market_is_open()
        )

    def cancel_stale_buy_orders(self):
        now = time.time()

        for raw_code, order_info in list(self.manager.kiwoom.order.items()):
            code = str(raw_code).strip().zfill(6)

            try:
                order_type = str(order_info.get("주문구분", "") or "").strip()
                remain_qty = int(order_info.get("미체결수량", 0) or 0)
                strategy_name = str(order_info.get("strategy_name", "") or "")
                order_time = float(order_info.get("order_time", 0) or 0)
                order_no = (
                    order_info.get("주문번호") or order_info.get("order_no") or ""
                )

                if order_type != "매수" or remain_qty <= 0:
                    continue

                if code in self.manager.pending_buy_cancellations:
                    continue

                if not order_time:
                    order_info["order_time"] = now
                    continue

                if now - order_time < self.manager.buy_order_cancel_after:
                    continue

                if not order_no:
                    print("[StrategyManager] 매수취소 불가 - " f"주문번호 없음: {code}")
                    continue

                result = self.manager.kiwoom.send_order(
                    "send_buy_cancel_order",
                    "3001",
                    3,
                    code,
                    remain_qty,
                    0,
                    "00",
                    order_no,
                    strategy_name=strategy_name,
                )

                if result == 0:
                    self.manager.pending_buy_cancellations[code] = {
                        "strategy_name": strategy_name,
                        "requested_at": now,
                        "remaining_quantity": remain_qty,
                        "missing_confirmations": 0,
                    }

                    self.notify(
                        f"[매수 미체결 취소요청] "
                        f"{self.manager.get_code_name(code)}({code}) "
                        f"잔량 {remain_qty}주 / 전략: {strategy_name}"
                    )
                else:
                    self.notify_error(
                        f"[매수 미체결 자동취소 실패] "
                        f"{self.manager.get_code_name(code)}({code}) "
                        f"result={result}",
                        event_key=f"strategy.StrategyManager.cancel_stale_buy_orders:{code}",
                        error_identity=f"SendOrder:{result}",
                    )

            except Exception:
                error_msg = traceback.format_exc()
                self.notify_error(
                    error_msg,
                    event_key=f"strategy.StrategyManager.cancel_stale_buy_orders:{code}",
                )

    def cleanup_cancelled_buy_positions(self):
        for code, cancel_info in list(self.manager.pending_buy_cancellations.items()):
            try:
                current_order = self.manager.kiwoom.order.get(code, {})
                remain = int(current_order.get("미체결수량", 0) or 0)

                # 취소 후에도 미체결이 남아 있으면 아직 처리 중.
                if remain > 0:
                    cancel_info["missing_confirmations"] = 0
                    continue

                balance = self.manager.kiwoom.balance.get(code, {})
                quantity = int(balance.get("보유수량", 0) or 0)

                # 일부 체결 후 잔량만 취소된 경우 포지션 유지.
                if quantity > 0:
                    self.manager.pending_buy_cancellations.pop(code, None)
                    self.manager.reserved_buy_orders.pop(code, None)

                    self.notify(
                        f"[일부 체결 후 잔량 취소 완료] "
                        f"{self.manager.get_code_name(code)}({code}) "
                        f"보유수량 {quantity}주 / 포지션 유지"
                    )
                    continue

                cancel_info["missing_confirmations"] = (
                    int(cancel_info.get("missing_confirmations", 0) or 0) + 1
                )

                if cancel_info["missing_confirmations"] < 2:
                    continue

                delete_position_strategy(code)
                self.manager.pending_buy_cancellations.pop(code, None)
                self.manager.reserved_buy_orders.pop(code, None)

                self.notify(
                    f"[전량 미체결 매수취소 완료] "
                    f"{self.manager.get_code_name(code)}({code}) "
                    "포지션 DB 삭제"
                )

            except Exception:
                error_msg = traceback.format_exc()
                self.notify_error(
                    error_msg,
                    event_key=f"strategy.StrategyManager.cleanup_cancelled_buy_positions:{code}",
                )
