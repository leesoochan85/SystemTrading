"""Account and order regression scenarios with no broker connection or requests."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from paper_fixture import qt_stubs

qt_stubs()
from strategy.StrategyManager import StrategyManager
from util import db_helper as db


class ManagerRefactorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = str(Path(self.directory.name) / "positions.db")
        for name in [
            "util.db_helper.POSITION_DB",
            "strategy.StrategyManager.POSITION_DB",
        ]:
            patcher = patch(name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in [
            "strategy.StrategyManager.send_message",
            "strategy.StrategyManager.send_error",
        ]:
            patcher = patch(name, Mock())
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "strategy.StrategyManager.check_transaction_open", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        console = redirect_stdout(StringIO())
        console.__enter__()
        self.addCleanup(console.__exit__, None, None, None)
        m = self.manager = StrategyManager.__new__(StrategyManager)
        m.strategies = [SimpleNamespace(strategy_name="ValueQualityStrategy")]
        m.is_running = m.is_initialized = m.account_state_safe = (
            m.deposit_state_safe
        ) = m.market_data_ready = True
        m.account_state_failure_reason = None
        m.deposit = 120000
        m.cash_deposit = m.d2_estimated_deposit = 1000000
        m.general_orderable_cash = 120000
        m.reserved_buy_orders = {}
        m.buy_request_in_progress = set()
        m.last_buy_rejections = {}
        m.pending_buy_cancellations = {}
        m.buy_order_cancel_after = 600
        m.pending_sold_position_deletions = {}
        m.sold_position_delete_confirmations = 2
        m.last_account_failure_log_at = 0
        m.last_order_sync_at = 0
        m.order_sync_interval = 60
        m.failed_account_retry_interval = 10
        m.get_code_name = lambda code: "Fixture"
        m.kiwoom = SimpleNamespace(
            balance={},
            order={},
            universe_realtime_transaction_info={},
            send_order=Mock(return_value=0),
            get_order=Mock(),
            get_balance=Mock(),
            last_order_query_success=True,
            last_balance_query_success=True,
        )

    def buy(self, code):
        return self.manager.request_buy_order(
            "ValueQualityStrategy", code, "Fixture", 10000, "buy", "1001"
        )

    def test_nested_different_code_requests_share_reserved_cash(self):
        nested = []

        def send(*args, **kwargs):
            if args[3] == "000001":
                nested.append(self.buy("000002"))
            return 0

        self.manager.kiwoom.send_order.side_effect = send
        first = self.buy("000001")
        self.assertEqual(first["quantity"], 9)
        self.assertEqual(nested[0]["quantity"], 2)
        self.assertLessEqual(self.manager.get_reserved_buy_amount(), 120000)
        self.assertEqual(self.manager.get_account_position_count(), 2)
        self.assertEqual(self.manager.buy_request_in_progress, set())

    def test_nested_same_code_is_blocked_before_second_broker_call(self):
        nested, reasons = [], []

        def send(*args, **kwargs):
            nested.append(self.buy("000001"))
            reasons.append(self.manager.get_buy_rejection_reason("000001"))
            return 0

        self.manager.kiwoom.send_order.side_effect = send
        self.assertIsNotNone(self.buy("000001"))
        self.assertEqual(nested, [None])
        self.assertIn("처리 중", reasons[0])
        self.manager.kiwoom.send_order.assert_called_once()

    def test_rejected_and_exception_orders_release_temporary_reservation(self):
        for result in [-1, RuntimeError("fixture")]:
            with self.subTest(result=str(result)):
                self.manager.kiwoom.send_order.side_effect = (
                    result if isinstance(result, Exception) else None
                )
                self.manager.kiwoom.send_order.return_value = result
                self.assertIsNone(self.buy("000001"))
                self.assertEqual(self.manager.reserved_buy_orders, {})
                self.assertEqual(self.manager.buy_request_in_progress, set())
                self.assertNotIn("000001", self.manager.kiwoom.order)

    def test_slot_limit_counts_held_pending_and_reserved_union(self):
        self.manager.kiwoom.balance = {
            str(i).zfill(6): {"보유수량": 1} for i in range(1, 9)
        }
        self.manager.kiwoom.order = {"000009": {"주문구분": "매수", "미체결수량": 1}}
        self.manager.reserved_buy_orders = {"000010": {"amount": 10000}}
        self.assertEqual(self.manager.get_account_position_count(), 10)
        self.assertIsNone(self.buy("000011"))
        self.manager.kiwoom.send_order.assert_not_called()

    def test_cancel_failure_keeps_reservation_and_success_is_not_sent_twice(self):
        self.manager.kiwoom.order = {
            "000001": {
                "주문구분": "매수",
                "미체결수량": 2,
                "order_time": 100,
                "주문번호": "o1",
                "strategy_name": "ValueQualityStrategy",
            }
        }
        self.manager.reserved_buy_orders = {"000001": {"amount": 20000}}
        with patch("strategy.StrategyManager.time.time", return_value=1000):
            self.manager.kiwoom.send_order.return_value = -1
            self.manager.cancel_stale_buy_orders()
            self.assertEqual(self.manager.pending_buy_cancellations, {})
            self.assertIn("000001", self.manager.reserved_buy_orders)
            self.manager.kiwoom.send_order.return_value = 0
            self.manager.cancel_stale_buy_orders()
            calls = self.manager.kiwoom.send_order.call_count
            self.manager.cancel_stale_buy_orders()
            self.assertEqual(self.manager.kiwoom.send_order.call_count, calls)
            self.assertIn("000001", self.manager.pending_buy_cancellations)

    def test_partial_fill_after_cancel_keeps_position_and_clears_reservation(self):
        db.save_position_strategy("1", "Fixture", "ValueQualityStrategy", 1, 10000)
        self.manager.pending_buy_cancellations = {
            "000001": {"missing_confirmations": 0}
        }
        self.manager.reserved_buy_orders = {"000001": {"amount": 20000}}
        self.manager.kiwoom.balance = {"000001": {"보유수량": 1}}
        self.manager.cleanup_cancelled_buy_positions()
        self.assertEqual(db.get_position_detail("1")["quantity"], 1)
        self.assertEqual(self.manager.reserved_buy_orders, {})
        self.assertEqual(self.manager.pending_buy_cancellations, {})

    def test_cancelled_unfilled_position_requires_two_confirmations(self):
        db.save_position_strategy("1", "Fixture", "ValueQualityStrategy", 0, 0)
        self.manager.pending_buy_cancellations = {
            "000001": {"missing_confirmations": 0}
        }
        self.manager.cleanup_cancelled_buy_positions()
        self.assertIsNotNone(db.get_position_detail("1"))
        self.manager.cleanup_cancelled_buy_positions()
        self.assertIsNone(db.get_position_detail("1"))

    def test_failed_order_query_does_not_read_balance_or_confirm_deletion(self):
        self.manager.kiwoom.last_order_query_success = False
        self.manager.pending_sold_position_deletions = {"000001": 1}
        self.assertFalse(self.manager.refresh_order_and_balance_state())
        self.manager.kiwoom.get_balance.assert_not_called()
        self.assertEqual(self.manager.pending_sold_position_deletions, {"000001": 1})
        self.assertFalse(self.manager.account_state_safe)
        self.assertIsNone(self.buy("000002"))
        self.manager.kiwoom.send_order.assert_not_called()

    def test_pending_reservation_recovers_only_known_strategy_and_unowned_holding_stays_unowned(
        self,
    ):
        db.save_position_strategy("1", "Fixture", "ValueQualityStrategy", 0, 0)
        db.save_position_strategy("2", "Fixture", "RemovedStrategy", 0, 0)
        self.manager.kiwoom.balance = {
            str(i).zfill(6): {"보유수량": 2, "매입가": 11000} for i in [1, 2, 3]
        }
        result = self.manager.sync_position_quantities_from_balance()
        self.assertEqual(result, {"reservation_recovered": 1, "quantity_corrected": 0})
        self.assertEqual(db.get_position_detail("1")["quantity"], 2)
        self.assertEqual(db.get_position_detail("2")["quantity"], 0)
        self.assertIsNone(db.get_position_detail("3"))


if __name__ == "__main__":
    unittest.main()
