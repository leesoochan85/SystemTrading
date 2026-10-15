"""Period ranking, isolated fills/restarts, shared subscriptions and read-only web views."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from paper_fixture import history, market_db, qt_stubs
qt_stubs()
from strategy.MomentumContractionStrategy import MomentumContractionStrategy
from strategy.StrategyManager import StrategyManager
from util.momentum_breakout import analyze_bars, load_config, rank_and_filter
from util.momentum_periods import PERIODS, period_db_path
from util.paper_trading import MOMENTUM
from util.period_paper_engine import ParallelMomentumPaperEngine
from util.virtual_strategy_engine import VirtualStrategyEngine
from web_api.period_queries import get_momentum_periods
from web_api.query_context import ReadOnlyDatabase


class MomentumPeriodTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 30, 8, 59)
        self.path = self.root / "paper.db"
        market = market_db(self.root / "market.db", {"000001": history()})
        self.kiwoom = SimpleNamespace(send_order=Mock(), set_real_reg=Mock(return_value=0),
                                      set_stock_realtime_fid_names=Mock())
        self.source = MomentumContractionStrategy(self.kiwoom, market_db_path=market, clock=lambda: self.now)
        metric = analyze_bars(history(), self.source.rules)
        self.raw = {f"{i:06d}": dict(metric, low126_score=i, return21_pct=0,
                                   return63_pct=0, return126_pct=0) for i in range(1, 101)}
        for period, winners in zip(PERIODS, [(1, 2), (3, 4), (5, 6)]):
            for i in winners:
                self.raw[f"{i:06d}"][period["field"]] = 1000-i
        self.source.universe = {code: {"code_name": "Fixture " + code} for code in self.raw}
        self.publish_metrics(self.raw)
        self.source.is_init_success = True
        self.engine = ParallelMomentumPaperEngine([self.source], self.path, record_ticks=False)
        self.source.watchlist_writer(self.source.metrics_date, self.source.metrics,
                                     self.source.universe, self.source.stats, self.now)
        self.source.on_realtime_registered()
        self.engine.on_realtime_registered()
        self.volumes = {}

    def publish_metrics(self, metrics):
        self.source.metrics, counts = rank_and_filter(metrics, self.source.rules)
        self.source.stats = dict(counts, universe=100, excluded_history=0, reference_date="20260929")
        self.source.metrics_date = self.now.strftime("%Y%m%d")

    def tick(self, code, at, price):
        self.now = datetime.strptime("20260930" + at, "%Y%m%d%H%M%S")
        qty = 100
        self.volumes[code] = self.volumes.get(code, 0) + qty
        self.engine.on_realtime_tick(code, {"체결시간": at, "현재가": price, "저가": 16100,
            "거래량": qty, "누적거래량": self.volumes[code]})

    def enter(self, codes):
        for code in codes:
            self.tick(code, "090000", 16200)
        for code in codes:
            self.tick(code, "090500", 16600)

    def view(self):
        self.engine.flush_snapshot_if_due(force=True)
        return get_momentum_periods(ReadOnlyDatabase(self.path))

    def test_periods_rank_separately_and_reuse_rejected_baseline_stocks(self):
        self.assertEqual(self.source.get_realtime_candidate_codes(), ["000099", "000100"])
        for key, expected in [("1m", ["000001", "000002"]), ("3m", ["000003", "000004"]), ("6m", ["000005", "000006"])]:
            engine = self.engine.period_engines[key]
            self.assertEqual(engine.get_candidate_codes(), expected)
            self.assertEqual(engine.momentum.stats["valid_history"], 100)
            self.assertEqual(engine.momentum.stats["top_ranked"], 2)
            self.assertEqual(engine.momentum.config["account"], self.source.config["account"])
            self.assertEqual({k: v for k, v in engine.momentum.rules.items() if k != "momentum_mode"},
                             {k: v for k, v in self.source.rules.items() if k != "momentum_mode"})

    def test_return_formula_uses_close_N_sessions_ago_not_low(self):
        rows = history()
        for index, price in [(-1, 200), (-22, 100), (-64, 80), (-127, 50)]:
            rows[index].update(open=price, high=price+5, low=price-5, close=price)
        metric = analyze_bars(rows, self.source.rules)
        self.assertEqual([metric[p["field"]] for p in PERIODS], [100, 150, 300])

    def test_rank_then_filters_without_backfilling_and_stable_ties(self):
        raw = deepcopy(self.raw)
        raw["000001"]["adr20_pct"] = 0
        raw["000003"]["return21_pct"] = raw["000002"]["return21_pct"]
        self.publish_metrics(raw)
        self.engine.get_candidate_codes()
        metric = self.engine.period_engines["1m"].momentum
        self.assertEqual(metric.get_realtime_candidate_codes(), ["000002"])
        self.assertEqual(metric.metrics["000003"]["rejection_reason"], "MOMENTUM_RANK")
        self.assertEqual(metric.stats["top_ranked"], 2)

    def test_one_tick_stream_enters_different_portfolios_without_live_orders(self):
        self.enter(["000001", "000003", "000005"])
        for key, code in [("1m", "000001"), ("3m", "000003"), ("6m", "000005")]:
            broker = self.engine.period_engines[key].broker
            self.assertEqual(set(broker.positions), {(MOMENTUM, code)})
            self.assertLess(broker.accounts[MOMENTUM]["cash"], 10000000)
        self.assertFalse(self.engine.baseline.broker.positions)
        self.assertEqual(self.engine.baseline.broker.accounts[MOMENTUM]["cash"], 10000000)
        self.kiwoom.send_order.assert_not_called()

    def test_same_stock_can_fill_three_times_and_restart_without_duplicate_entries(self):
        for p in PERIODS:
            self.raw["000001"][p["field"]] = 2000
        self.publish_metrics(self.raw)
        self.engine.get_candidate_codes()
        self.enter(["000001"])
        balances = {}
        for key, engine in self.engine.period_engines.items():
            self.assertIn((MOMENTUM, "000001"), engine.broker.positions)
            balances[key] = engine.broker.accounts[MOMENTUM]["cash"]
        self.engine = ParallelMomentumPaperEngine([self.source], self.path, False)
        self.engine.get_candidate_codes()
        for key, engine in self.engine.period_engines.items():
            self.assertEqual(len(engine.broker.positions), 1)
            self.assertEqual(engine.broker.accounts[MOMENTUM]["cash"], balances[key])
        self.tick("000001", "090600", 16000)
        self.tick("000001", "090700", 16600)
        for engine in self.engine.period_engines.values():
            self.assertFalse(engine.broker.positions)
            with closing(sqlite3.connect(engine.broker.db_path)) as con:
                self.assertEqual([r[0] for r in con.execute("SELECT event_type FROM paper_trade ORDER BY trade_id")], ["BUY", "SELL"])

    def test_shared_registration_includes_period_only_candidates_and_registers_opening(self):
        manager = StrategyManager.__new__(StrategyManager)
        manager.kiwoom, manager.strategies = self.kiwoom, [self.source]
        manager.virtual_strategy_engine = SimpleNamespace(paper_engine=self.engine, get_open_codes=self.engine.get_open_codes)
        codes = manager.register_shared_event_realtime(manager.strategies)
        self.assertEqual(codes, ["000001", "000002", "000003", "000004", "000005", "000006", "000099", "000100"])
        for engine in self.engine.period_engines.values():
            self.assertEqual(engine.momentum.opening.registered_at, self.now)

    def test_no_extra_history_load_and_holdings_still_exit_when_candidates_disappear(self):
        self.enter(["000003"])
        self.publish_metrics({})
        with patch("strategy.MomentumContractionStrategy.load_momentum_metrics", side_effect=AssertionError("extra history read")):
            self.assertFalse(self.engine.get_candidate_codes())
            self.assertIn("000003", self.engine.get_open_codes())
            self.tick("000003", "090600", 16000)
        self.assertFalse(self.engine.period_engines["3m"].broker.positions)

    def test_no_candidates_still_publishes_each_account_heartbeat(self):
        self.publish_metrics({})
        self.now = datetime(2026, 9, 30, 10)
        view = self.view()
        self.assertTrue(view["ready"])
        for profile in view["profiles"]:
            account = profile["account"]
            self.assertEqual(account["stats"]["paper_execution"]["phase"], "NO_CANDIDATES")
            self.assertFalse(account["stats"]["paper_execution"]["live_new_buys_enabled"])
            self.assertEqual(account["accounts"][0]["equity"], 10000000)
            self.assertEqual(len(account["equity_history"]), 1)

    def test_web_read_only_missing_files_and_mismatched_start_dates(self):
        missing = self.root / "absent.db"
        self.assertFalse(get_momentum_periods(ReadOnlyDatabase(missing))["ready"])
        self.assertFalse(list(self.root.glob("absent*")))
        view = self.view()
        self.assertTrue(all(view["comparison"].values()))
        with closing(sqlite3.connect(period_db_path(self.path, "3m"))) as con, con:
            con.execute("UPDATE paper_account SET created_at='20260929085900'")
        view = get_momentum_periods(ReadOnlyDatabase(self.path))
        self.assertFalse(view["comparison"]["same_start_date"])
        with closing(sqlite3.connect(period_db_path(self.path, "3m"))) as con, con:
            con.execute("UPDATE paper_account SET created_at='20260930090000'")
        self.assertFalse(get_momentum_periods(ReadOnlyDatabase(self.path))["comparison"]["same_start_date"])

    def test_api_new_route_returns_profiles_without_changing_old_contract(self):
        from fastapi.testclient import TestClient
        from web_api.main import app
        self.view()
        with patch("web_api.database.PAPER_DB", self.path):
            client = TestClient(app)
            response = client.get("/api/strategies/MomentumContractionStrategy/momentum-periods")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([p["id"] for p in response.json()["profiles"]], ["1m", "3m", "6m"])
            self.assertEqual(client.get("/api/strategies/ValueQualityStrategy/momentum-periods").status_code, 404)
            self.assertEqual(client.get("/api/strategies/MomentumContractionStrategy/momentum-periods?trade_limit=0").status_code, 422)
            old = client.get("/api/strategies/MomentumContractionStrategy/paper-account").json()
            self.assertEqual(old["settings"]["strategy"]["momentum_mode"], "video_low126")

    def test_fault_in_one_period_does_not_skip_other_periods(self):
        with patch.object(self.engine.period_engines["1m"], "on_realtime_tick", side_effect=RuntimeError("fixture")), \
             patch.object(self.engine.period_engines["3m"], "on_realtime_tick") as third, \
             patch.object(self.engine.period_engines["6m"], "on_realtime_tick") as sixth:
            self.tick("000003", "090000", 16200)
        third.assert_called_once()
        sixth.assert_called_once()

    def test_next_day_refreshes_common_history_once_for_all_periods(self):
        self.now = datetime(2026, 10, 1, 9)
        stats = dict(self.source.stats, reference_date="20260930")
        with patch("strategy.MomentumContractionStrategy.load_momentum_metrics", return_value=(self.source.metrics, stats)) as load, \
             patch("util.paper_strategy_engine.check_transaction_open", return_value=True):
            self.engine.on_realtime_tick("000001", {"체결시간": "090000", "현재가": 16200,
                "저가": 16100, "거래량": 100, "누적거래량": 100})
        load.assert_called_once()
        for engine in self.engine.period_engines.values():
            self.assertEqual(engine.momentum.metrics_date, "20261001")
            self.assertEqual(engine.momentum.stats["reference_date"], "20260930")

    def test_union_tick_recorder_includes_period_only_stock_exactly_once(self):
        recorder = self.engine.baseline.tick_recorder = Mock()
        self.tick("000003", "090000", 16200)
        recorder.record.assert_called_once()
        self.assertEqual(recorder.record.call_args.args[0], "000003")

    def test_production_virtual_engine_wires_all_three_periods_to_configured_base_path(self):
        with patch.dict(os.environ, {"SYSTEMTRADING_PAPER_DB": str(self.path)}, clear=False), \
             patch("util.virtual_strategy_engine.get_all_virtual_positions", return_value=[]), \
             patch("util.virtual_strategy_engine.get_virtual_entry_keys_for_date", return_value=set()):
            virtual = VirtualStrategyEngine([self.source])
        self.assertIsInstance(virtual.paper_engine, ParallelMomentumPaperEngine)
        for period in PERIODS:
            self.assertTrue(
                virtual.paper_engine.period_engines[period["id"]].broker.db_path.samefile(
                    period_db_path(self.path, period["id"])
                )
            )
        self.assertIn("000005", virtual.paper_engine.get_candidate_codes())

    def test_late_registration_never_fabricates_first_five_minutes(self):
        self.now = datetime(2026, 9, 30, 10)
        self.engine.on_realtime_registered()
        self.tick("000001", "100001", 16600)
        self.assertFalse(self.engine.period_engines["1m"].broker.positions)
        self.assertEqual(self.engine.period_engines["1m"].momentum.runtime["000001"]["reason"], "OPENING_RANGE_MISSING")

    def test_period_modes_load_and_empty_candidates_do_not_fail_registration(self):
        for period in PERIODS:
            config = deepcopy(self.source.config)
            config["strategy"]["momentum_mode"] = period["mode"]
            path = self.root / "config.json"
            path.write_text(json.dumps(config))
            self.assertEqual(load_config(path)["strategy"]["momentum_mode"], period["mode"])
        self.publish_metrics({})
        manager = StrategyManager.__new__(StrategyManager)
        manager.kiwoom, manager.strategies = self.kiwoom, [self.source]
        manager.virtual_strategy_engine = SimpleNamespace(paper_engine=self.engine, get_open_codes=self.engine.get_open_codes)
        self.assertEqual(manager.register_shared_event_realtime(manager.strategies), [])
        self.kiwoom.set_real_reg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
