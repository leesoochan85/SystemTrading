"""Regression checks for continuing C while freezing legacy A/B records."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from paper_fixture import config
from util.paper_trading import PaperBroker, MOMENTUM, BASELINE, FILTERED
from web_api import database


class MomentumSingleAccountTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "paper.db"
        self.now = datetime(2026, 9, 30, 9, 6)
        self.config = config()
        self.broker = PaperBroker(self.path, self.config, self.now)

    def rows(self, table, name):
        with closing(sqlite3.connect(self.path)) as con:
            return con.execute(f"SELECT * FROM {table} WHERE strategy_name=? ORDER BY rowid", (name,)).fetchall()

    def seed_legacy(self):
        position, error = self.broker.buy(MOMENTUM, "000001", "Fixture", 10000, 9500,
            {"initial_stop": 9500, "partial_done": False}, "ENTRY", {}, self.now)
        self.assertIsNone(error)
        self.broker.sell(position, 11000, 30, "TIME_PARTIAL_EXIT", self.now, partial=True)
        self.broker.snapshot(self.now)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.row_factory = sqlite3.Row
            for name in (BASELINE, FILTERED):
                for table, id_column in (("paper_account", None), ("paper_position", "position_id"),
                                         ("paper_trade", "trade_id"), ("paper_equity", None)):
                    for row in con.execute(f"SELECT * FROM {table} WHERE strategy_name=?", (MOMENTUM,)).fetchall():
                        values = dict(row)
                        values["strategy_name"] = name
                        if id_column:
                            values.pop(id_column)
                        # Inactive rows must not be decoded or checked against current rules.
                        if table == "paper_account":
                            values["config_json"] = "archived settings"
                        if table == "paper_position":
                            values["state_json"] = "archived state"
                        columns = ",".join(values)
                        marks = ",".join("?" for _ in values)
                        con.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))
        return position

    def test_fresh_database_contains_only_momentum(self):
        self.assertEqual(set(self.broker.accounts), {MOMENTUM})
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT strategy_name FROM paper_account").fetchall(), [(MOMENTUM,)])
        for name in (BASELINE, FILTERED):
            result = self.broker.buy(name, "000001", "Fixture", 10000, 9500, {}, "ENTRY", {}, self.now)
            self.assertEqual(result, (None, "INACTIVE_PAPER_ACCOUNT"))

    def test_restart_continues_partial_state_and_never_updates_legacy_rows(self):
        position = self.seed_legacy()
        tables = ("paper_account", "paper_position", "paper_trade", "paper_equity")
        archived = {(table, name): self.rows(table, name) for table in tables for name in (BASELINE, FILTERED)}
        old_account = dict(self.broker.accounts[MOMENTUM])
        restarted = PaperBroker(self.path, self.config, self.now)
        self.assertEqual(set(restarted.accounts), {MOMENTUM})
        self.assertEqual(set(restarted.positions), {(MOMENTUM, "000001")})
        remaining = restarted.positions[(MOMENTUM, "000001")]
        self.assertEqual(remaining["position_id"], position["position_id"])
        self.assertEqual(remaining["quantity"], position["quantity"])
        self.assertEqual(remaining["state"], position["state"])
        self.assertEqual(restarted.accounts[MOMENTUM], old_account)
        restarted.mark("000001", 12000, self.now)
        restarted.snapshot(self.now)
        with patch.object(database, "PAPER_DB", self.path):
            view = database.get_paper_account()
            for field in ("accounts", "positions", "trades", "equity_history"):
                self.assertEqual({r["strategy_name"] for r in view[field]}, {MOMENTUM})
        self.assertTrue(view["ready"])
        restarted.sell(remaining, 12000, remaining["quantity"], "MA_CLOSE_EXIT", self.now)
        restarted.snapshot(self.now)
        self.assertEqual(restarted.buy(MOMENTUM, "000001", "Fixture", 10000, 9500,
            {}, "ENTRY", {}, self.now)[1], "SAME_DAY_REENTRY")
        for key, rows in archived.items():
            self.assertEqual(self.rows(*key), rows)

    def test_archived_accounts_alone_do_not_report_ready(self):
        self.seed_legacy()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("DELETE FROM paper_account WHERE strategy_name=?", (MOMENTUM,))
        with patch.object(database, "PAPER_DB", self.path):
            self.assertFalse(database.get_paper_account()["ready"])

    def test_new_endpoint_and_legacy_url_return_same_single_account(self):
        self.seed_legacy()
        from fastapi.testclient import TestClient
        from web_api.main import app
        with patch.object(database, "PAPER_DB", self.path):
            client = TestClient(app)
            prefix = "/api/strategies/MomentumContractionStrategy/"
            result = client.get(prefix + "paper-account")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), client.get(prefix + "paper-comparison").json())
            self.assertEqual(len(result.json()["accounts"]), 1)
            self.assertEqual(client.get(prefix + "paper-account?trade_limit=201").status_code, 422)
