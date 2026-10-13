from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_fixture import history, market_db
from replay_momentum_breakout import COLUMNS, replay


class MomentumReplayTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        rows = history()
        # These future bars must not change the Sep 30 entry/pivot/ATR.
        for day, close in (("20260930", 17000), ("20261001", 17800),
            ("20261002", 18100), ("20261006", 18000), ("20261007", 16650)):
            rows.append(dict(date=day, open=close, high=close+400,
                low=close-400, close=close, volume=200000))
        self.market = market_db(self.root / "market.db", {"000001": rows})
        self.ticks = self.root / "ticks.csv"
        self.paper = self.root / "paper.db"
        self.events = [
            ("20260930090000", "090000", 16200, 16100, 100, 100),
            ("20260930090459", "090459", 16400, 16100, 200, -100),
            ("20260930090500", "090500", 16600, 16100, 300, 100),
            ("20261007090000", "090000", 17500, 17300, 100, 100),
            ("20261008090000", "090000", 16800, 16700, 100, 100),
        ]

    def write_ticks(self, events=None):
        with self.ticks.open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(COLUMNS)
            for received, trade, price, low, volume, quantity in events or self.events:
                writer.writerow((received, "20260930085900", "000001", trade, price, low, volume, quantity))

    def run_replay(self):
        with redirect_stdout(StringIO()):
            return replay(self.ticks, self.market, self.paper)

    def test_standalone_replay_enters_partials_exits_and_preserves_source_db(self):
        self.write_ticks()
        original = self.market.read_bytes()
        # A subprocess does not inherit tests' Qt stubs; no ActiveX is imported.
        result = subprocess.run([sys.executable, "replay_momentum_breakout.py", "--ticks", str(self.ticks),
            "--market-db", str(self.market), "--paper-db", str(self.paper)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["scope"], "MOMENTUM_ONLY_TICK_REPLAY")
        self.assertTrue(report["paper_only"])
        self.assertEqual([t["reason_code"] for t in report["trades"]],
            ["CONTRACTION_BREAKOUT", "TIME_PARTIAL_EXIT", "MA_CLOSE_EXIT"])
        self.assertEqual(report["trades"][0]["price"], 16609)
        self.assertEqual(report["trades"][1]["event_at"][:8], "20261007")
        self.assertFalse(report["positions"])
        self.assertAlmostEqual(report["cash"], report["initial_cash"] + report["realized_pnl"])
        self.assertEqual(self.market.read_bytes(), original)

    def test_boundary_missing_execution_never_creates_a_trade(self):
        self.write_ticks(self.events[:2] + [("20260930090500", "090500", 16600, 16100, 400, 100)])
        report = self.run_replay()
        self.assertFalse(report["trades"])
        self.assertEqual(report["last_runtime"]["000001"]["reason"], "OPENING_VOLUME_GAP")

    def test_existing_output_is_never_overwritten(self):
        self.write_ticks()
        self.paper.write_bytes(b"keep existing data")
        with self.assertRaisesRegex(ValueError, "new separate"):
            self.run_replay()
        self.assertEqual(self.paper.read_bytes(), b"keep existing data")

    def test_capture_environment_cannot_append_to_replay_input(self):
        self.write_ticks()
        original = self.ticks.read_bytes()
        with patch.dict("os.environ", {"SYSTEMTRADING_PAPER_TICK_CSV": str(self.ticks)}):
            self.run_replay()
        self.assertEqual(self.ticks.read_bytes(), original)

    def test_invalid_late_row_fails_before_creating_paper_database(self):
        self.write_ticks(self.events + [("20261008085900", "085900", 16800, 16700, 100, 100)])
        with self.assertRaisesRegex(ValueError, "receipt order"):
            self.run_replay()
        self.assertFalse(self.paper.exists())

    def test_missing_market_database_is_not_created(self):
        self.write_ticks()
        missing = self.root / "missing.db"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            replay(self.ticks, missing, self.paper)
        self.assertFalse(missing.exists())
        self.assertFalse(self.paper.exists())
