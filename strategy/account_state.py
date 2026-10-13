"""Account query recovery, holdings reconciliation and confirmed deletion.

Use the manager's existing state so account failure flags, deletion counters
and order reservations remain shared with the order coordinator.
"""

from contextlib import closing
import sqlite3
import time
import traceback

from util.db_helper import (
    delete_position_strategy,
    get_position_strategy,
    get_filled_position_codes,
    get_all_position_details,
)


class AccountStateSynchronizer:
    def __init__(self, manager, *, position_db, notify, notify_error):
        self.manager = manager
        self.position_db = position_db
        self.notify = notify
        self.notify_error = notify_error

    def calculate_total_assets(self):
        """
        총자산 = D+2 추정예수금 + 현재 보유주식 평가금액.

        현재가가 없으면 잔고 현재가, 그것도 없으면 매입가를 마지막 대체값으로 사용한다.
        """
        evaluation_amount = 0

        for raw_code, info in self.manager.kiwoom.balance.items():
            code = str(raw_code).strip().zfill(6)
            quantity = int(info.get("보유수량", 0) or 0)

            if quantity <= 0:
                continue

            rt = self.manager.kiwoom.universe_realtime_transaction_info.get(code, {})
            current_price = int(
                rt.get("현재가")
                or info.get("현재가", 0)
                or info.get("매입가", 0)
                or info.get("매입단가", 0)
                or 0
            )

            evaluation_amount += quantity * max(0, current_price)

        return max(
            0,
            int(self.manager.d2_estimated_deposit or 0) + evaluation_amount,
        )

    def sync_deposit_from_kiwoom(self):
        """
        주문가능금액과 총자산 계산용 현금값을 갱신한다.

        키움의 새 주문가능금액 조회가 정상 완료되면,
        이전 조회 이후 Manager가 보유하던 임시 예약은 폐기한다.
        그 시점에는 이미 실제 주문/체결 상태가 키움 값에 반영된 것으로 본다.
        """
        queried_orderable_cash = self.manager.kiwoom.get_deposit()

        if (
            not self.manager.kiwoom.last_deposit_query_success
            or queried_orderable_cash is None
        ):
            self.manager.deposit_state_safe = False

            self.notify_error(
                "[안전 중지] 주문가능금액 조회 실패 - "
                "마지막 정상 금액을 유지하고 신규 주문을 중단합니다.",
                event_key="strategy.StrategyManager.sync_deposit_from_kiwoom",
            )

            return False

        self.manager.deposit = max(0, int(queried_orderable_cash or 0))

        self.manager.cash_deposit = int(
            self.manager.kiwoom.last_cash_deposit
            if self.manager.kiwoom.last_cash_deposit is not None
            else 0
        )

        self.manager.general_orderable_cash = int(
            self.manager.kiwoom.last_general_orderable_cash
            if self.manager.kiwoom.last_general_orderable_cash is not None
            else self.manager.deposit
        )

        self.manager.d2_estimated_deposit = int(
            self.manager.kiwoom.last_d2_estimated_deposit
            if self.manager.kiwoom.last_d2_estimated_deposit is not None
            else self.manager.cash_deposit
        )

        self.manager.deposit_state_safe = True
        self.manager.last_deposit_sync_at = time.time()

        # 새 키움 주문가능금액을 기준으로 다시 시작하므로 이전 임시예약은 제거.
        self.manager.reserved_buy_orders.clear()

        self.manager.total_assets = self.manager.calculate_total_assets()

        print(
            "[StrategyManager] 자금 동기화 완료: "
            f"주문가능금액 {self.manager.deposit:,}원 / "
            f"예수금 {self.manager.cash_deposit:,}원 / "
            f"D+2추정예수금 {self.manager.d2_estimated_deposit:,}원 / "
            f"총자산 {self.manager.total_assets:,}원"
        )

        return True

    def refresh_order_and_balance_state(self, allow_buy_cancel=True):
        self.manager.kiwoom.get_order()

        if not self.manager.kiwoom.last_order_query_success:
            now = time.time()
            first_failure = (
                self.manager.account_state_safe
                or self.manager.account_state_failure_reason != "미체결 주문 조회 실패"
            )

            self.manager.account_state_safe = False
            self.manager.account_state_failure_reason = "미체결 주문 조회 실패"
            self.manager.last_order_sync_at = now - (
                self.manager.order_sync_interval
                - self.manager.failed_account_retry_interval
            )

            message = (
                "[안전 중지] 미체결 주문 조회 실패 - "
                "기존 주문 정보를 유지하고 신규 전략 주문을 중단합니다."
            )

            print(f"[StrategyManager] {message}")
            if first_failure:
                self.notify(message)

            return False

        self.manager.kiwoom.get_balance()
        self.manager.last_order_sync_at = time.time()

        if not self.manager.kiwoom.last_balance_query_success:
            now = time.time()
            first_failure = (
                self.manager.account_state_safe
                or self.manager.account_state_failure_reason != "잔고 조회 실패"
            )

            self.manager.account_state_safe = False
            self.manager.account_state_failure_reason = "잔고 조회 실패"
            self.manager.last_order_sync_at = now - (
                self.manager.order_sync_interval
                - self.manager.failed_account_retry_interval
            )

            message = (
                "[안전 중지] 잔고 조회 실패 - "
                "기존 잔고를 유지하고 신규 전략 주문을 중단합니다."
            )

            print(f"[StrategyManager] {message}")
            if first_failure:
                self.notify(message)

            return False

        was_unsafe = not self.manager.account_state_safe
        previous_reason = self.manager.account_state_failure_reason

        self.manager.account_state_safe = True
        self.manager.account_state_failure_reason = None
        self.manager.last_account_failure_log_at = 0

        if was_unsafe:
            message = "[계좌 조회 복구] 미체결 주문 및 잔고 조회 정상 완료 - " + (
                "전략 주문 안전 조건을 다시 확인합니다."
                if self.manager.is_initialized
                else "전략 초기화 대기 · 계좌 동기화만 유지합니다."
            )
            if previous_reason:
                message += f" / 직전 원인: {previous_reason}"

            print(f"[StrategyManager] {message}")
            self.notify(message)

        # 키움의 최신 미체결 목록에 이미 들어간 종목은 Manager 임시예약의
        # 역할이 끝났다. 다만 예수금을 새로 조회하기 전에는 중복 차감을 피하기 위해
        # 여기서 모두 지우지 않고 sync_deposit_from_kiwoom()에서 일괄 초기화한다.
        if allow_buy_cancel:
            self.manager.cancel_stale_buy_orders()

        # HTS/MTS 수동 부분매도 체결 이벤트를 놓친 경우에도
        # 실제 키움 잔고를 기준으로 strategy_position.db 수량을 보정한다.
        self.manager.sync_position_quantities_from_balance()

        self.manager.cleanup_cancelled_buy_positions()
        self.manager.cleanup_sold_positions()
        self.manager.check_balance_position_db_consistency()

        if self.manager.deposit_state_safe:
            self.manager.save_performance_snapshot()

        return True

    def sync_position_quantities_from_balance(self):
        """
        실제 키움 잔고를 strategy_position.db의 최종 기준으로 사용한다.

        자동 복구 규칙
        --------------
        1) DB에 해당 종목과 strategy_name이 이미 존재하고
        2) DB quantity <= 0, buy_price <= 0인 '매수 예약 row'이며
        3) 실제 키움 잔고 quantity > 0, buy_price > 0이면
           체결 이벤트가 누락된 것으로 보고 실제 잔고의 수량/매입가를 복구한다.

        안전 원칙
        ---------
        - DB에 종목 자체가 없는 실제 보유종목은 자동으로 전략에 귀속하지 않는다.
        - strategy_name이 현재 실행 전략 목록에 없는 row도 자동복구하지 않는다.
        - 이미 정상 체결된 DB 포지션(quantity > 0)은 기존처럼 수량만 실제 잔고에 맞춘다.
        - 정상 포지션의 buy_price, created_at, strategy_name은 변경하지 않는다.
        - 실제 잔고가 0인 전량매도는 cleanup_sold_positions()의 2회 확인 후 삭제한다.

        목적
        ----
        - 매수 체결 Chejan/DB 반영 일시 누락 후 프로그램 재시작
        - HTS/MTS 수동 부분매도
        - 재접속 후 실제 잔고와 전략 DB 수량 차이 복구
        """
        if not self.manager.account_state_safe:
            return

        if not self.manager.kiwoom.last_balance_query_success:
            return

        db_positions = get_all_position_details()
        valid_strategy_names = {
            str(getattr(strategy, "strategy_name", "") or "")
            for strategy in self.manager.strategies
        }

        quantity_corrections = []
        reservation_recoveries = []

        with closing(sqlite3.connect(self.position_db)) as con, con:
            for raw_code, balance_info in self.manager.kiwoom.balance.items():
                code = str(raw_code).strip().zfill(6)

                actual_quantity = int(balance_info.get("보유수량", 0) or 0)
                actual_buy_price = float(
                    balance_info.get("매입가") or balance_info.get("매입단가") or 0
                )

                if actual_quantity <= 0:
                    continue

                db_position = db_positions.get(code)

                # DB 기록 자체가 없으면 HTS/MTS 수동매수일 가능성이 있으므로
                # 어떤 전략에도 자동 귀속하지 않는다.
                if db_position is None:
                    continue

                strategy_name = str(db_position.get("strategy_name", "") or "").strip()
                db_quantity = int(db_position.get("quantity", 0) or 0)
                db_buy_price = float(db_position.get("buy_price", 0) or 0)
                code_name = (
                    balance_info.get("종목명")
                    or db_position.get("code_name")
                    or self.manager.get_code_name(code)
                )

                # 매수 주문 직후 생성된 0주/0원 예약 row인데 실제 잔고가
                # 이미 존재한다면 체결 이벤트 DB 반영이 빠진 상황으로 간주한다.
                # 단, 전략명이 현재 실행 중인 전략 중 하나로 확정된 경우만 복구한다.
                if db_quantity <= 0 and db_buy_price <= 0:
                    if (
                        strategy_name
                        and strategy_name in valid_strategy_names
                        and actual_buy_price > 0
                    ):
                        con.execute(
                            """
                            UPDATE position_strategy
                            SET code_name = ?,
                                quantity = ?,
                                buy_price = ?
                            WHERE code = ?
                              AND quantity <= 0
                              AND buy_price <= 0
                        """,
                            (
                                code_name,
                                actual_quantity,
                                int(round(actual_buy_price)),
                                code,
                            ),
                        )

                        reservation_recoveries.append(
                            (
                                code,
                                code_name,
                                strategy_name,
                                actual_quantity,
                                actual_buy_price,
                            )
                        )
                    continue

                # 이미 정상 체결 기록이 있는 포지션은 수동 부분매도나
                # 체결 이벤트 누락을 대비해 수량만 실제 잔고 기준으로 맞춘다.
                if db_quantity <= 0:
                    continue

                if db_quantity == actual_quantity:
                    continue

                con.execute(
                    """
                    UPDATE position_strategy
                    SET quantity = ?
                    WHERE code = ?
                """,
                    (
                        actual_quantity,
                        code,
                    ),
                )

                quantity_corrections.append(
                    (
                        code,
                        code_name,
                        db_quantity,
                        actual_quantity,
                    )
                )

        for (
            code,
            code_name,
            strategy_name,
            quantity,
            buy_price,
        ) in reservation_recoveries:
            message = (
                "[체결누락 포지션 자동복구] "
                f"{code_name}({code}) / {strategy_name} / "
                f"DB 0주·0원 -> 실제잔고 "
                f"{quantity}주·평단 {buy_price:,.0f}원"
            )
            print(f"[StrategyManager] {message}")
            self.notify(message)

        for code, code_name, before, after in quantity_corrections:
            message = (
                "[수동매도/잔고 DB 자동보정] "
                f"{code_name}({code}) "
                f"전략DB {before}주 -> 실제잔고 {after}주"
            )
            print(f"[StrategyManager] {message}")
            self.notify(message)

        return {
            "reservation_recovered": len(reservation_recoveries),
            "quantity_corrected": len(quantity_corrections),
        }

    def notify_strategy_position_closed(
        self,
        code,
        strategy_name,
        reason="",
    ):
        if not strategy_name:
            return

        for strategy in self.manager.strategies:
            if getattr(strategy, "strategy_name", None) != strategy_name:
                continue

            callback = getattr(
                strategy,
                "on_position_closed",
                None,
            )

            if not callable(callback):
                return

            try:
                callback(
                    code,
                    reason=reason,
                )
            except TypeError:
                callback(code)
            except Exception:
                error_msg = traceback.format_exc()
                self.notify_error(
                    f"[전략 포지션 종료 후처리 실패] "
                    f"{strategy_name} / {code}\n"
                    f"{error_msg}",
                    event_key=f"strategy.StrategyManager.notify_strategy_position_closed:{code}",
                )
            return

    def cleanup_sold_positions(self):
        tracked_codes = set(get_filled_position_codes())

        for code in list(self.manager.pending_sold_position_deletions.keys()):
            if code not in tracked_codes:
                self.manager.pending_sold_position_deletions.pop(code, None)

        for raw_code in tracked_codes:
            code = str(raw_code).strip().zfill(6)

            try:
                balance = self.manager.kiwoom.balance.get(code, {})
                quantity = int(balance.get("보유수량", 0) or 0)

                if quantity > 0:
                    self.manager.pending_sold_position_deletions.pop(code, None)
                    continue

                order_info = self.manager.kiwoom.order.get(code, {})
                order_type = str(order_info.get("주문구분", "") or "").strip()
                remain = int(order_info.get("미체결수량", 0) or 0)

                if order_type == "매도" and remain > 0:
                    self.manager.pending_sold_position_deletions.pop(code, None)
                    continue

                count = self.manager.pending_sold_position_deletions.get(code, 0) + 1
                self.manager.pending_sold_position_deletions[code] = count

                if count < self.manager.sold_position_delete_confirmations:
                    continue

                strategy_name = get_position_strategy(code)

                delete_position_strategy(code)
                self.manager.kiwoom.order.pop(code, None)
                self.manager.reserved_buy_orders.pop(code, None)
                self.manager.pending_sold_position_deletions.pop(code, None)

                self.manager.notify_strategy_position_closed(
                    code,
                    strategy_name,
                    reason="실제 잔고 0 확인",
                )

                self.notify(
                    f"[전량 매도 완료] "
                    f"{self.manager.get_code_name(code)}({code}) "
                    f"포지션 DB 삭제 / 전략: "
                    f"{strategy_name or '확인불가'}"
                )

            except Exception:
                error_msg = traceback.format_exc()
                self.notify_error(
                    error_msg,
                    event_key=f"strategy.StrategyManager.cleanup_sold_positions:{code}",
                )

    def check_balance_position_db_consistency(self):
        if not self.manager.account_state_safe:
            return False

        if not self.manager.kiwoom.last_balance_query_success:
            return False

        actual_balance = {}

        for raw_code, info in self.manager.kiwoom.balance.items():
            code = str(raw_code).strip().zfill(6)
            quantity = int(info.get("보유수량", 0) or 0)

            if quantity <= 0:
                continue

            actual_balance[code] = {
                "code_name": info.get("종목명") or self.manager.get_code_name(code),
                "quantity": quantity,
                "buy_price": int(info.get("매입가", 0) or info.get("매입단가", 0) or 0),
            }

        db_positions = get_all_position_details()
        issues = []

        for code, actual in sorted(actual_balance.items()):
            db_position = db_positions.get(code)

            if db_position is None:
                issues.append(
                    f"- [미관리 보유] "
                    f"{actual['code_name']}({code}) "
                    f"실제 {actual['quantity']}주 / 전략 DB 기록 없음"
                )
                continue

            strategy_name = db_position.get(
                "strategy_name",
                "-",
            )
            db_quantity = int(db_position.get("quantity", 0) or 0)
            db_buy_price = float(db_position.get("buy_price", 0) or 0)

            if db_quantity != actual["quantity"]:
                issues.append(
                    f"- [수량 불일치] "
                    f"{actual['code_name']}({code}) "
                    f"실제 {actual['quantity']}주 / "
                    f"DB {db_quantity}주 / "
                    f"{strategy_name}"
                )

            if db_buy_price <= 0:
                issues.append(
                    f"- [매입가 누락] "
                    f"{actual['code_name']}({code}) "
                    f"실제 보유 중 / DB 매입가 없음 / "
                    f"{strategy_name}"
                )

        for code, db_position in sorted(db_positions.items()):
            db_quantity = int(db_position.get("quantity", 0) or 0)
            db_buy_price = float(db_position.get("buy_price", 0) or 0)

            # 매수 주문 직후 아직 체결되지 않은 예약 row는 제외.
            if db_quantity <= 0 and db_buy_price <= 0:
                continue

            if code in actual_balance:
                continue

            if code in self.manager.pending_sold_position_deletions:
                continue

            order_info = self.manager.kiwoom.order.get(code, {})
            order_type = str(order_info.get("주문구분", "") or "").strip()
            remain = int(order_info.get("미체결수량", 0) or 0)

            if order_type == "매도" and remain > 0:
                continue

            issues.append(
                f"- [잔고 없음/DB 잔존] "
                f"{db_position.get('code_name') or self.manager.get_code_name(code)}"
                f"({code}) DB {db_quantity}주 / "
                f"{db_position.get('strategy_name', '-')}"
            )

        signature = tuple(sorted(issues))

        if signature == self.manager.last_position_db_mismatch_signature:
            return bool(issues)

        previous_signature = self.manager.last_position_db_mismatch_signature
        self.manager.last_position_db_mismatch_signature = signature

        if issues:
            message = (
                "[경고] 실제 잔고와 전략 DB 불일치 발견\n\n"
                + "\n".join(issues)
                + "\n\n자동 수정하지 않았습니다. 실제 잔고와 전략 기록을 확인하세요."
            )
            print(f"[StrategyManager] {message}")
            self.notify(message)
            return True

        if previous_signature:
            self.notify("[정상화] 실제 잔고와 전략 DB 불일치가 해소되었습니다.")

        return False
