"""Real SQLite regression tests for repositories and the legacy public facade."""

from contextlib import closing
from datetime import datetime
import inspect
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from util import db_helper as db
from util import position_repository
from util.db_context import RepositoryContext


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 13, 10, 5)


class RepositoryRegressionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.monitoring = self.root / "monitoring.db"
        self.positions = self.root / "positions.db"
        for name, value in [
            ("MONITORING_DB", self.monitoring),
            ("POSITION_DB", self.positions),
            ("datetime", FixedDatetime),
        ]:
            patcher = patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def rows(self, table, columns="*"):
        with closing(sqlite3.connect(self.monitoring)) as con:
            con.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in con.execute(f"SELECT {columns} FROM {table} ORDER BY rowid")
            ]

    def fill(self, fill, order, kind, quantity, price, **costs):
        db.save_trade_fill(
            fill,
            order,
            "000001",
            "Fixture",
            kind,
            "ValueQualityStrategy",
            quantity,
            price,
            **costs,
        )

    def test_partial_buys_average_actual_prices_and_duplicate_fill_is_ignored(self):
        self.fill("buy1", "o1", "매수", 4, 100)
        self.fill("buy2", "o1", "매수", 6, 120)
        self.fill("buy2", "o1", "매수", 6, 120)
        position = db.get_position_detail("1")
        self.assertEqual(position["quantity"], 10)
        self.assertEqual(position["buy_price"], 112)
        self.assertEqual(position["created_at"], "20261013100500")
        self.assertEqual(len(self.rows("trade_fill_log")), 2)

    def test_partial_sells_keep_latest_cumulative_costs_and_do_not_reduce_twice(self):
        db.save_position_strategy("1", "Fixture", "ValueQualityStrategy", 10, 100)
        self.fill("sell1", "o2", "매도", 4, 120, buy_price=100, fee=3, tax=4)
        self.fill("sell2", "o2", "매도", 2, 130, buy_price=100, fee=5, tax=6)
        self.fill("sell2", "o2", "매도", 2, 130, buy_price=100, fee=5, tax=6)
        (trade,) = self.rows("real_trades")
        self.assertEqual(trade["quantity"], 6)
        self.assertAlmostEqual(trade["sell_price"], 740 / 6)
        self.assertEqual(
            (
                trade["gross_realized_pnl"],
                trade["fee"],
                trade["tax"],
                trade["net_realized_pnl"],
            ),
            (140, 5, 6, 129),
        )
        self.assertEqual(db.get_position_detail("1")["quantity"], 4)
        self.assertEqual(db.get_today_realized_pnl(), (140, 129, 0, 0))

    def test_late_costs_change_net_summary_without_creating_a_new_trade(self):
        self.fill("sell1", "o2", "매도", 2, 120, buy_price=100)
        self.assertEqual(db.get_today_realized_pnl(), (40, 0, 0, 1))
        db.update_latest_sell_trade_costs("1", fee=3, tax=4)
        self.assertEqual(db.get_today_realized_pnl(), (40, 33, 0, 0))
        self.assertEqual(len(self.rows("real_trades")), 1)

    def test_signal_hits_order_outcome_and_event_idempotency_survive_split(self):
        args = dict(
            strategy_name="ValueQualityStrategy",
            code="1",
            code_name="Fixture",
            signal_type="BUY",
            reason_code="ENTRY",
            condition_data={"가격": 100},
        )
        key = db.save_strategy_signal(**args)
        self.assertEqual(db.save_strategy_signal(**args), key)
        db.update_strategy_signal_order_result(key, True, "ORDER_SENT", None)
        (signal,) = self.rows("strategy_signal_log")
        self.assertEqual(signal["hit_count"], 2)
        self.assertEqual(signal["order_result"], "ORDER_SENT")
        self.assertEqual(json.loads(signal["condition_data"]), {"가격": 100})
        for _ in range(2):
            db.save_order_event(
                "request-1", "1", "Fixture", "매수", event_type="REQUESTED"
            )
        self.assertEqual(db.get_today_order_count(), 1)

    def test_snapshot_insert_failure_rolls_back_delete_and_partial_insert(self):
        balance = {
            "000001": {"종목명": "Before", "보유수량": 2, "매입가": 100, "현재가": 110}
        }
        self.assertEqual(db.save_position_snapshot(balance), 1)
        previous = self.rows("position_snapshot")
        with closing(sqlite3.connect(self.monitoring)) as con, con:
            con.execute(
                """CREATE TRIGGER fail_snapshot BEFORE INSERT ON position_snapshot
                           WHEN NEW.code='000002' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"""
            )
        updated = {
            "000001": {"보유수량": 5, "매입가": 100},
            "000002": {"보유수량": 1, "매입가": 200},
        }
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_position_snapshot(updated)
        self.assertEqual(self.rows("position_snapshot"), previous)

    def test_position_snapshot_daily_equity_and_strategy_roi_keep_values(self):
        db.save_position_strategy("1", "Fixture", "ValueQualityStrategy", 2, 100)
        balance = {"000001": {"보유수량": 2, "매입가": 100, "현재가": 110}}
        db.save_position_snapshot(balance)
        self.assertEqual(db.save_daily_equity(1000, balance), (220, 1220))
        result = db.save_strategy_equity_snapshots(["ValueQualityStrategy"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["deployed_capital"], 200)
        self.assertEqual(result[0]["unrealized_pnl"], 20)
        self.assertEqual(result[0]["return_on_deployed_capital_pct"], 10)

    def test_legacy_order_table_is_preserved_during_schema_migration(self):
        with closing(sqlite3.connect(self.monitoring)) as con, con:
            con.execute("CREATE TABLE order_log (order_no TEXT, code TEXT)")
            con.execute("INSERT INTO order_log VALUES ('old', '000001')")
        db.init_monitoring_tables()
        legacy = self.rows("order_log_snapshot_legacy_20261013100500")
        self.assertEqual(legacy, [{"order_no": "old", "code": "000001"}])
        self.assertEqual(self.rows("order_log"), [])

    def test_imported_facade_function_uses_current_paths_and_direct_context_is_isolated(
        self,
    ):
        save = db.save_position_strategy
        save("1", "First", "ValueQualityStrategy", 2, 100)
        other = self.root / "other.db"
        with patch.object(db, "POSITION_DB", other):
            save("2", "Second", "ValueQualityStrategy", 3, 200)
            self.assertIsNone(db.get_position_detail("1"))
            self.assertEqual(db.get_position_detail("2")["quantity"], 3)
        context = RepositoryContext(
            position_db=str(other),
            monitoring_db=str(self.monitoring),
            clock=FixedDatetime.now,
        )
        self.assertEqual(
            position_repository.get_position_detail("2", context=context)["quantity"], 3
        )
        self.assertEqual(db.get_position_detail("1")["quantity"], 2)
        self.assertIsNone(db.get_position_detail("2"))

    def test_existing_function_signatures_are_unchanged(self):
        signatures = json.loads(
            (Path(__file__).parent / "fixtures/db_api_signatures.json").read_text()
        )
        for name, signature in signatures.items():
            with self.subTest(function=name):
                self.assertEqual(str(inspect.signature(getattr(db, name))), signature)


class ConnectionLifetimeTest(unittest.TestCase):
    def test_commit_rollback_and_handle_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transaction.db"
            context = RepositoryContext()
            with context.connect(path) as committed:
                committed.execute("CREATE TABLE items (value INTEGER)")
                committed.execute("INSERT INTO items VALUES (1)")
            with self.assertRaises(sqlite3.ProgrammingError):
                committed.execute("SELECT 1")
            with self.assertRaises(RuntimeError):
                with context.connect(path) as rolled_back:
                    rolled_back.execute("INSERT INTO items VALUES (2)")
                    raise RuntimeError("abort transaction")
            with self.assertRaises(sqlite3.ProgrammingError):
                rolled_back.execute("SELECT 1")
            with context.connect(path) as con:
                self.assertEqual(
                    con.execute("SELECT value FROM items").fetchall(), [(1,)]
                )


if __name__ == "__main__":
    unittest.main()
