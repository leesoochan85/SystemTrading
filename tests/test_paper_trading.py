from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from paper_fixture import config
from util.paper_trading import PaperBroker, PROFILES, BASELINE, MOMENTUM
from web_api import database


class PaperLedgerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "paper.db"
        self.now = datetime(2026, 9, 30, 9, 6)
        self.config = config()
        self.broker = PaperBroker(self.path, self.config, self.now)

    def buy(self, code="000001", profile=MOMENTUM):
        return self.broker.buy(profile, code, "Fixture", 10000, 9500,
            {"stop_price": 9500, "partial_done": False}, "ENTRY", {}, self.now)

    def test_identical_accounts_integer_lots_costs_and_cash_limit(self):
        for profile in PROFILES:
            position, error = self.buy(profile=profile)
            self.assertIsNone(error)
            self.assertEqual(position["quantity"], 93)  # 50,000 risk / 535.815 modeled loss
            self.assertEqual(position["entry_price"], 10005)
            self.assertAlmostEqual(self.broker.accounts[profile]["cash"], 10000000 - 93 * 10005 * 1.00035)
        self.assertEqual(len(self.broker.positions), 3)
        self.assertEqual(self.broker.positions[(MOMENTUM, "000001")]["quantity"], self.broker.positions[(BASELINE, "000001")]["quantity"])

    def test_partial_fill_restart_cash_reconciliation_and_reentry_guard(self):
        position, _ = self.buy()
        quantity = position["quantity"]
        original_cost = quantity * position["entry_price"] * 1.00035
        self.assertTrue(self.broker.sell(position, 11000, quantity // 3, "TIME_PARTIAL_EXIT", self.now, partial=True))
        restarted = PaperBroker(self.path, self.config, self.now)
        remaining = restarted.positions[(MOMENTUM, "000001")]
        self.assertEqual(remaining["quantity"], quantity - quantity // 3)
        self.assertTrue(remaining["state"]["partial_done"])
        self.assertEqual(remaining["state"]["stop_price"], 10038)
        restarted.sell(remaining, 9000, remaining["quantity"], "DAY_LOW_STOP", self.now)
        with closing(sqlite3.connect(self.path)) as con:
            net_pnl = con.execute("SELECT SUM(net_pnl) FROM paper_trade WHERE event_type='SELL'").fetchone()[0]
            proceeds = con.execute("SELECT SUM(price*quantity-fee) FROM paper_trade WHERE event_type='SELL'").fetchone()[0]
        self.assertAlmostEqual(net_pnl, proceeds - original_cost)
        self.assertAlmostEqual(restarted.accounts[MOMENTUM]["cash"], 10000000 + net_pnl)
        self.broker = PaperBroker(self.path, self.config, self.now)
        self.assertEqual(self.buy()[1], "SAME_DAY_REENTRY")
        self.now += timedelta(days=1)
        self.assertIsNone(self.buy()[1])

    def test_max_positions_and_fully_invested_cash_never_go_negative(self):
        for i in range(1, 11):
            position, error = self.broker.buy(MOMENTUM, f"{i:06d}", "Fixture", 10000, 9990,
                {"partial_done": False}, "ENTRY", {}, self.now)
            self.assertIsNone(error)
            self.assertIsInstance(position["quantity"], int)
        self.assertEqual(self.buy("000011")[1], "POSITION_LIMIT")
        self.assertGreaterEqual(self.broker.accounts[MOMENTUM]["cash"], 0)
        self.assertEqual(len(self.broker.positions), 10)

    def test_gap_stop_fills_observed_tick_and_slippage_not_the_stop(self):
        position, _ = self.buy()
        self.broker.sell(position, 9000, position["quantity"], "DAY_LOW_STOP", self.now)
        with closing(sqlite3.connect(self.path)) as con:
            price, pnl = con.execute("SELECT price,net_pnl FROM paper_trade WHERE event_type='SELL'").fetchone()
        self.assertEqual(price, 8995)
        self.assertLess(pnl, -50000)  # The risk budget cannot guarantee gap losses.

    def test_slippage_can_reject_an_atr_boundary_entry(self):
        result = self.broker.buy(MOMENTUM, "000001", "Fixture", 10000, 9500,
            {}, "ENTRY", {}, self.now, max_stop_distance=500)
        self.assertEqual(result, (None, "STOP_TOO_WIDE_AFTER_SLIPPAGE"))
        self.assertEqual(self.broker.accounts[MOMENTUM]["cash"], 10000000)

    def test_competing_writer_cannot_spend_stale_cash(self):
        other = PaperBroker(self.path, self.config, self.now)
        self.buy()
        result = other.buy(MOMENTUM, "000002", "Fixture", 10000, 9500, {}, "ENTRY", {}, self.now)
        self.assertEqual(result[1], "ACCOUNT_CHANGED_RESTART_REQUIRED")
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_position").fetchone()[0], 1)

    def test_account_settings_cannot_change_mid_experiment(self):
        changed = deepcopy(self.config)
        changed["strategy"]["trailing_ma_period"] = 20
        with self.assertRaises(ValueError):
            PaperBroker(self.path, changed, self.now)
        separate = PaperBroker(Path(self.directory.name) / "experiment2.db", changed, self.now)
        self.assertEqual(separate.accounts[MOMENTUM]["cash"], 10000000)

    def test_max_drawdown_survives_intraday_recovery_and_restart(self):
        self.buy()
        self.broker.mark("000001", 12000, self.now)
        self.broker.snapshot(self.now)
        peak = self.broker.equity(MOMENTUM)
        self.broker.mark("000001", 8000, self.now)
        self.broker.snapshot(self.now)
        drawdown = (self.broker.equity(MOMENTUM) / peak - 1) * 100
        self.broker.mark("000001", 12000, self.now)
        self.broker.snapshot(self.now)
        self.broker = PaperBroker(self.path, self.config, self.now)
        with patch.object(database, "PAPER_DB", self.path):
            view = database.get_paper_comparison()
        account = next(a for a in view["accounts"] if a["strategy_name"] == MOMENTUM)
        self.assertAlmostEqual(account["max_drawdown_pct"], drawdown)
        self.assertAlmostEqual(account["equity"], peak)

    def test_read_api_reports_partial_events_but_only_completed_trades(self):
        position, _ = self.buy()
        self.broker.sell(position, 11000, 30, "TIME_PARTIAL_EXIT", self.now, partial=True)
        with patch.object(database, "PAPER_DB", self.path):
            view = database.get_paper_comparison()
            events = database.get_virtual_event_summary(MOMENTUM, "20260930")
        account = next(a for a in view["accounts"] if a["strategy_name"] == MOMENTUM)
        self.assertEqual(account["performance"]["completed_trade_count"], 0)
        self.assertEqual(events, {"buy": 1, "sell": 1, "total": 2})
        self.broker.sell(position, 12000, position["quantity"], "MA_CLOSE_EXIT", self.now)
        with patch.object(database, "PAPER_DB", self.path):
            account = next(a for a in database.get_paper_comparison()["accounts"] if a["strategy_name"] == MOMENTUM)
        self.assertEqual(account["performance"]["completed_trade_count"], 1)
        self.assertEqual(account["performance"]["win_rate_pct"], 100)

    def test_missing_api_database_is_not_created(self):
        missing = Path(self.directory.name) / "missing.db"
        with patch.object(database, "PAPER_DB", missing):
            self.assertFalse(database.get_paper_comparison()["ready"])
        self.assertFalse(missing.exists())

    def test_stop_cannot_be_lowered(self):
        position, _ = self.buy()
        with self.assertRaises(ValueError):
            self.broker.update_state(position, dict(position["state"], stop_price=9000))
        self.assertEqual(position["state"]["stop_price"], 9500)
