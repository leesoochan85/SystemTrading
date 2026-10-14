"""Synthetic data and broker mocks only; no external requests or orders."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from paper_fixture import market_db, qt_stubs
qt_stubs()
from util import market_history as mh
from util import db_helper as dh
from strategy.StrategyManager import StrategyManager


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 13, 8)


def bars(count):
    dates = pd.bdate_range(end="2026-10-12", periods=count)
    return pd.DataFrame({"open": 100, "high": 110, "low": 90,
                         "close": 100, "volume": 1000}, index=dates)


def records(frame):
    return [dict(date=date.strftime("%Y%m%d"), **row.to_dict())
            for date, row in frame.iterrows()]


class HistoryBackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.db"
        clock = patch.object(mh, "datetime", FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)
        self.output = StringIO()
        context = redirect_stdout(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def run_collection(self, frame, count=157, latest=True):
        saved = bars(count)
        if not latest:
            saved.index -= timedelta(days=7)
        market_db(self.path, {"000001": records(saved)})
        with patch.object(mh, "_fetch_code_ohlcv", return_value=frame) as fetch:
            result = mh.bootstrap_market_history(db_path=self.path,
                codes=["000001"], request_sleep=0)
        return result, fetch

    def test_latest_157_bars_backfill_and_repeat_does_not_duplicate(self):
        result, fetch = self.run_collection(bars(200))
        self.assertEqual(fetch.call_args.args, ("20250907", "20261012", "000001"))
        self.assertEqual(result["backfill_codes"], 1)
        self.assertEqual(result["eligible_code_count"], 1)
        self.assertEqual(mh.get_code_history_counts(["000001"], self.path)["000001"], 200)
        with patch.object(mh, "_fetch_code_ohlcv") as again:
            result = mh.bootstrap_market_history(db_path=self.path, codes=["000001"], request_sleep=0)
        again.assert_not_called()
        self.assertEqual(result["up_to_date_codes"], 1)
        self.assertEqual(result["row_count"], 200)

    def test_sufficient_history_only_fetches_recent_gap(self):
        result, fetch = self.run_collection(bars(200), count=190, latest=False)
        self.assertEqual(result["incremental_codes"], 1)
        self.assertEqual(fetch.call_args.args[0], "20260925")

    def test_new_listing_shortage_is_reported_without_padding(self):
        result, _ = self.run_collection(bars(4), count=4)
        self.assertEqual(result["insufficient_history_count"], 1)
        self.assertEqual(result["row_count"], 4)

    def test_recent_full_scan_skips_repeat_but_still_reports_shortage(self):
        self.run_collection(bars(4), count=4)
        with patch.object(mh, "_fetch_code_ohlcv") as fetch:
            result = mh.bootstrap_market_history(db_path=self.path, codes=["000001"], request_sleep=0)
        fetch.assert_not_called()
        self.assertEqual(result["insufficient_history_count"], 1)
        self.assertEqual(result["up_to_date_codes"], 1)

    def test_recent_full_scan_fetches_incrementally_on_next_day(self):
        self.run_collection(bars(4), count=4)
        class NextDay(FixedDatetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 14, 8)
        with patch.object(mh, "datetime", NextDay), patch.object(mh, "_fetch_code_ohlcv", return_value=bars(4)) as fetch:
            result = mh.bootstrap_market_history(db_path=self.path, codes=["000001"], request_sleep=0)
        self.assertEqual(fetch.call_args.args[0], "20261002")
        self.assertEqual(result["incremental_codes"], 1)
        self.assertEqual(result["insufficient_history_count"], 1)

    def test_weekly_audit_or_larger_range_repeats_full_scan(self):
        self.run_collection(bars(4), count=4)
        for widen in (True, False):
            if not widen:
                with sqlite3.connect(self.path) as con:
                    con.execute("UPDATE history_backfill_state SET end_date='20261001'")
            with patch.object(mh, "_fetch_code_ohlcv", return_value=bars(4)) as fetch:
                result = mh.bootstrap_market_history(db_path=self.path, codes=["000001"],
                    request_sleep=0, lookback_calendar_days=500 if widen else 400)
            self.assertEqual(result["backfill_codes"], 1)
            self.assertLess(fetch.call_args.args[0], "20260101")

    def test_empty_full_scan_does_not_suppress_retry(self):
        self.run_collection(pd.DataFrame(), count=4)
        with patch.object(mh, "_fetch_code_ohlcv", return_value=bars(4)) as fetch:
            mh.bootstrap_market_history(db_path=self.path, codes=["000001"], request_sleep=0)
        fetch.assert_called_once()

    def test_empty_response_preserves_existing_bars(self):
        result, _ = self.run_collection(pd.DataFrame())
        self.assertEqual(result["row_count"], 157)
        self.assertEqual(result["empty_codes"], ["000001"])

    def test_incomplete_current_day_does_not_count_as_180th_bar(self):
        saved = bars(179)
        saved.loc[pd.Timestamp("2026-10-13")] = [100, 110, 90, 100, 1000]
        market_db(self.path, {"000001": records(saved)})
        with patch.object(mh, "_fetch_code_ohlcv", return_value=pd.DataFrame()) as fetch:
            result = mh.bootstrap_market_history(db_path=self.path,
                codes=["000001"], request_sleep=0)
        fetch.assert_called_once()
        self.assertEqual(result["backfill_codes"], 1)
        self.assertEqual(result["insufficient_history_count"], 1)

    def test_target_can_be_configured_and_invalid_target_fails_before_fetch(self):
        market_db(self.path, {"000001": records(bars(100))})
        with patch.object(mh, "_fetch_code_ohlcv") as fetch:
            result = mh.bootstrap_market_history(db_path=self.path, codes=["000001"],
                request_sleep=0, required_history_rows=65)
            self.assertEqual(result["up_to_date_codes"], 1)
            with self.assertRaises(ValueError):
                mh.bootstrap_market_history(db_path=self.path, codes=["000001"], required_history_rows=0)
        fetch.assert_not_called()


class AccountOnlyRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "positions.db")
        for target in ("util.db_helper.POSITION_DB", "strategy.StrategyManager.POSITION_DB"):
            p = patch(target, self.db)
            p.start()
            self.addCleanup(p.stop)
        dh.save_position_strategy("000001", "Sold", "RemovedStrategy", 0, 100)
        m = self.manager = StrategyManager.__new__(StrategyManager)
        m.is_running = True
        m.is_initialized = False
        m.market_data_ready = False
        m.strategies = []
        m.kiwoom = SimpleNamespace(balance={}, order={}, get_order=Mock(), get_balance=Mock(),
            last_order_query_success=True, last_balance_query_success=True, send_order=Mock())
        m.account_state_safe = True
        m.account_state_failure_reason = None
        m.last_account_failure_log_at = 0
        m.last_order_sync_at = 0
        m.order_sync_interval = 60
        m.failed_account_retry_interval = 10
        m.deposit_state_safe = True
        m.last_deposit_sync_at = 1000
        m.deposit_sync_interval = 300
        m.pending_buy_cancellations = {}
        m.pending_sold_position_deletions = {}
        m.sold_position_delete_confirmations = 2
        m.reserved_buy_orders = {}
        m.market_open_interval = 1000
        m.market_closed_interval = 300000
        for name in ("set_timer_interval", "save_position_snapshot_if_due",
                     "save_strategy_equity_snapshot_if_due", "save_performance_snapshot",
                     "check_balance_position_db_consistency", "notify_strategy_position_closed",
                     "cancel_stale_buy_orders", "sync_deposit_from_kiwoom"):
            setattr(m, name, Mock())
        context = redirect_stdout(StringIO())
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def step(self, now, market_open=True):
        with patch("strategy.StrategyManager.time.time", return_value=now), \
             patch("strategy.StrategyManager.check_transaction_open", return_value=market_open):
            self.manager.step()

    def test_uninitialized_reconciles_twice_deletes_only_position_and_never_orders(self):
        self.step(1000)
        self.assertIsNotNone(dh.get_position_detail("000001"))
        self.step(1059)
        self.assertEqual(self.manager.kiwoom.get_balance.call_count, 1)
        self.step(1060)
        self.assertIsNone(dh.get_position_detail("000001"))
        self.manager.kiwoom.send_order.assert_not_called()
        self.manager.cancel_stale_buy_orders.assert_not_called()
        self.assertFalse(self.manager.is_initialized)
        self.assertFalse(self.manager.can_event_driven_order())

    def test_failed_balance_does_not_confirm_deletion(self):
        self.manager.kiwoom.last_balance_query_success = False
        self.step(1000)
        self.step(1010)
        self.assertEqual(self.manager.pending_sold_position_deletions, {})
        self.assertIsNotNone(dh.get_position_detail("000001"))

    def test_reappearing_holding_cancels_pending_deletion(self):
        self.step(1000)
        self.manager.kiwoom.balance = {"000001": {"보유수량": 2, "매입가": 100}}
        self.step(1060)
        self.assertEqual(self.manager.pending_sold_position_deletions, {})
        self.assertIsNotNone(dh.get_position_detail("000001"))

    def test_partial_manual_sell_corrects_existing_filled_quantity(self):
        dh.save_position_strategy("000001", "Held", "RemovedStrategy", 5, 100)
        self.manager.kiwoom.balance = {"000001": {"보유수량": 2, "매입가": 100}}
        self.step(1000)
        self.assertEqual(dh.get_position_detail("000001")["quantity"], 2)

    def test_market_closed_also_reconciles_and_stopped_manager_does_not(self):
        self.step(1000, market_open=False)
        self.manager.set_timer_interval.assert_called_with(300000)
        self.manager.kiwoom.get_balance.assert_called_once()
        self.manager.is_running = False
        self.step(2000)
        self.manager.kiwoom.get_balance.assert_called_once()


if __name__ == "__main__":
    unittest.main()
