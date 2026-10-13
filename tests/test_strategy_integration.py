from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from paper_fixture import history, market_db, qt_stubs
qt_stubs()

from util.paper_breakout_baseline import PaperBreakoutBaseline
from strategy.MomentumContractionStrategy import MomentumContractionStrategy
from strategy.StrategyManager import StrategyManager
from util.breakout_atr import ma_profit_exit_signal, load_atr_metrics, new_atr_state_from_value
from util.market_history import load_breakout_metrics
from util.paper_strategy_engine import MomentumPaperEngine
from util.paper_trading import PROFILES, MOMENTUM
from util.strategy_settings import get_ma_settings_status, save_ma_settings
from util.virtual_strategy_engine import VirtualStrategyEngine


class StrategyIntegrationTest(unittest.TestCase):
    def test_step_repeated_failure_uses_error_throttle_and_keeps_console_traceback(self):
        import io
        from contextlib import redirect_stdout
        from test_notifier_privacy import load_notifier

        notifier = load_notifier()
        response = Mock(ok=True)
        response.json.return_value = {"ok": True}
        notifier.requests.post.return_value = response
        manager = StrategyManager.__new__(StrategyManager)
        manager.is_running = True
        manager.is_initialized = True
        manager.virtual_strategy_engine = SimpleNamespace(
            flush_snapshot_if_due=Mock(side_effect=RuntimeError("snapshot failure"))
        )
        console = io.StringIO()

        def send_error(message, **kwargs):
            return notifier.send_error(message, token="FAKE", chat_id="1", **kwargs)

        with redirect_stdout(console), patch("strategy.StrategyManager.send_error", send_error), patch.object(notifier.time, "monotonic", return_value=1000):
            for _ in range(3):
                manager.step()
        self.assertEqual(notifier.requests.post.call_count, 1)
        self.assertEqual(console.getvalue().count("RuntimeError: snapshot failure"), 3)

    def test_deposit_failure_throttles_alert_but_keeps_buy_guard_unsafe(self):
        import io
        from contextlib import redirect_stdout
        from test_notifier_privacy import load_notifier

        notifier = load_notifier()
        response = Mock(ok=True)
        response.json.return_value = {"ok": True}
        notifier.requests.post.return_value = response
        manager = StrategyManager.__new__(StrategyManager)
        manager.deposit_state_safe = True
        manager.kiwoom = SimpleNamespace(get_deposit=Mock(return_value=None), last_deposit_query_success=False)
        console = io.StringIO()

        def send_error(message, **kwargs):
            return notifier.send_error(message, token="FAKE", chat_id="1", **kwargs)

        with redirect_stdout(console), patch("strategy.StrategyManager.send_error", send_error), patch.object(notifier.time, "monotonic", return_value=1000):
            for _ in range(3):
                self.assertFalse(manager.sync_deposit_from_kiwoom())
                self.assertFalse(manager.deposit_state_safe)
        self.assertEqual(notifier.requests.post.call_count, 1)
        self.assertEqual(console.getvalue().count("[안전 중지]"), 3)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = market_db(Path(self.directory.name) / "market.db", {"000001": history()})
        self.now = datetime(2026, 9, 30, 8, 59)
        self.cumulative_volume = 0
        self.kiwoom = SimpleNamespace(balance={}, order={}, universe_realtime_transaction_info={}, send_order=Mock())
        self.momentum = MomentumContractionStrategy(self.kiwoom, market_db_path=self.path, clock=lambda: self.now)
        self.momentum.check_and_get_universe()
        self.momentum.is_init_success = True
        self.high = PaperBreakoutBaseline(self.kiwoom, auto_init=False)
        self.high.market_history_db_path = self.path
        self.high.ma_settings_db_path = Path(self.directory.name) / "settings.db"
        self.high.universe = self.momentum.universe
        self.high.breakout_metrics = load_breakout_metrics(["000001"], breakout_window=180, db_path=self.path, before_date="20260930")
        self.high.breakout_metrics["000001"].update(load_atr_metrics(["000001"], self.path, "20260930")["000001"])
        self.high._ensure_daily_metrics = Mock()
        self.high.is_init_success = True
        self.engine = MomentumPaperEngine([self.high, self.momentum], Path(self.directory.name) / "paper.db")
        self.momentum.check_and_get_price_data()
        self.momentum.on_realtime_registered()

    def tick(self, time, price, low=16100):
        self.now = datetime.strptime("20260930" + time, "%Y%m%d%H%M%S")
        quantity = 250000 if self.cumulative_volume == 0 else 100
        self.cumulative_volume += quantity
        self.engine.on_realtime_tick("000001", {"체결시간": time, "현재가": price, "저가": low,
            "누적거래량": self.cumulative_volume, "거래량": quantity})

    def test_dropped_opening_trade_blocks_momentum_entry(self):
        self.tick("090000", 16200)
        self.cumulative_volume += 100  # A missing execution, potentially the OR high.
        self.tick("090500", 16600)
        self.assertNotIn((MOMENTUM, "000001"), self.engine.broker.positions)
        self.assertFalse(self.engine.broker.positions)
        self.assertEqual(self.momentum.runtime["000001"]["reason"], "OPENING_VOLUME_GAP")

    def test_rejected_stale_opening_tick_cannot_be_hidden_by_engine_guard(self):
        self.tick("090000", 16200)
        self.now = datetime(2026, 9, 30, 9, 2)
        self.engine.on_realtime_tick("000001", {"체결시간": "090100", "현재가": 19000,
            "저가": 16100, "누적거래량": 250100, "거래량": 100})
        self.tick("090500", 16600)
        self.assertNotIn((MOMENTUM, "000001"), self.engine.broker.positions)
        self.assertEqual(self.momentum.runtime["000001"]["reason"], "STALE_OR_OUTSIDE_SESSION")

    def test_captured_production_ticks_replay_the_same_c_entry(self):
        from replay_momentum_breakout import replay
        from contextlib import redirect_stdout
        from io import StringIO
        ticks = Path(self.directory.name) / "recorded.csv"
        with patch.dict(os.environ, {"SYSTEMTRADING_PAPER_TICK_CSV": str(ticks)}):
            self.engine = MomentumPaperEngine([self.high, self.momentum], Path(self.directory.name) / "capture.db")
        self.tick("090000", 16200)
        self.tick("090459", 16400)
        self.tick("090500", 16600)
        self.engine.flush_snapshot_if_due(force=True)
        self.engine.tick_recorder.close()
        with redirect_stdout(StringIO()):
            result = replay(ticks, self.path, Path(self.directory.name) / "replayed.db")
        original = self.engine.broker.positions[(MOMENTUM, "000001")]
        self.assertEqual(result["trades"][0]["price"], original["entry_price"])
        self.assertEqual(result["trades"][0]["quantity"], original["initial_quantity"])
        self.kiwoom.send_order.assert_not_called()

    def test_recording_header_error_preserves_file_and_keeps_paper_running(self):
        ticks = Path(self.directory.name) / "wrong.csv"
        ticks.write_text("preserve this file", encoding="utf-8")
        with patch.dict(os.environ, {"SYSTEMTRADING_PAPER_TICK_CSV": str(ticks)}):
            self.engine = MomentumPaperEngine([self.high, self.momentum], Path(self.directory.name) / "capture.db")
        self.assertIsNone(self.engine.tick_recorder)
        self.tick("090000", 16200)
        self.tick("090500", 16600)
        self.assertIn((MOMENTUM, "000001"), self.engine.broker.positions)
        self.assertEqual(ticks.read_text(encoding="utf-8"), "preserve this file")

    def test_first_tick_is_recorded_after_new_candidate_refresh(self):
        import csv
        ticks = Path(self.directory.name) / "new_candidate.csv"
        with patch.dict(os.environ, {"SYSTEMTRADING_PAPER_TICK_CSV": str(ticks)}):
            self.engine = MomentumPaperEngine([self.momentum], Path(self.directory.name) / "capture.db")
        metric = self.momentum.metrics["000001"]
        self.momentum.metrics = {}
        self.momentum.ensure_daily_metrics = Mock(side_effect=lambda now: self.momentum.metrics.update({"000001": metric}))
        self.tick("090000", 16200)
        self.engine.tick_recorder.close()
        with ticks.open(encoding="utf-8", newline="") as source:
            records = list(csv.DictReader(source))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["trade_time"], "090000")

    def test_only_momentum_account_trades_on_shared_stream(self):
        self.assertTrue(self.momentum.metrics["000001"]["eligible"])
        self.tick("090000", 16200)
        self.tick("090100", 16550)
        self.assertFalse(self.engine.broker.positions)
        self.tick("090500", 16600)
        self.assertEqual(set(p for p, _ in self.engine.broker.positions), set(PROFILES))
        c = self.engine.broker.positions[(MOMENTUM, "000001")]
        self.assertEqual(c["state"]["initial_stop"], 16100)
        self.assertEqual(c["entry_price"], 16609)
        self.assertLessEqual(16609 - c["state"]["initial_stop"], self.momentum.metrics["000001"]["atr14"])
        self.assertFalse(self.kiwoom.send_order.called)
        self.engine.flush_snapshot_if_due(force=True)
        with closing(sqlite3.connect(self.engine.broker.db_path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_equity").fetchone()[0], 1)

    def test_holdings_remain_managed_without_a_current_candidate_or_db(self):
        self.tick("090000", 16200)
        self.tick("090500", 16600)
        self.momentum.metrics = {}
        self.momentum.ensure_daily_metrics = Mock(side_effect=sqlite3.OperationalError("fixture database unavailable"))
        self.momentum.exit_context = Mock(side_effect=AssertionError("fixed stop must work without MA data"))
        self.tick("090600", 16000, low=15900)
        self.assertNotIn((MOMENTUM, "000001"), self.engine.broker.positions)
        self.assertFalse(self.engine.get_open_codes())
        with closing(sqlite3.connect(self.engine.broker.db_path)) as con:
            reason, price = con.execute("SELECT reason_code,price FROM paper_trade WHERE strategy_name=? AND event_type='SELL'", (MOMENTUM,)).fetchone()
        self.assertEqual((reason, price), ("DAY_LOW_STOP", 15992))

    def test_late_start_blocks_momentum_new_entries(self):
        self.now = datetime(2026, 9, 30, 9, 1)
        self.momentum.on_realtime_registered()
        self.tick("090500", 16600)
        self.assertNotIn((MOMENTUM, "000001"), self.engine.broker.positions)
        self.assertFalse(self.engine.broker.positions)
        self.assertEqual(self.momentum.runtime["000001"]["reason"], "OPENING_RANGE_MISSING")

    def test_missing_stale_or_out_of_order_trade_time_cannot_fill_paper_accounts(self):
        self.now = datetime(2026, 9, 30, 9, 6)
        self.engine.on_realtime_tick("000001", {"현재가": 17000, "저가": 16500, "누적거래량": 250000})
        self.engine.on_realtime_tick("000001", {"체결시간": "090000", "현재가": 17000, "저가": 16500, "누적거래량": 250000})
        self.assertFalse(self.engine.broker.positions)
        self.tick("090600", 16200)
        self.engine.on_realtime_tick("000001", {"체결시간": "090550", "현재가": 17000, "저가": 16500, "누적거래량": 250000})
        self.assertFalse(self.engine.broker.positions)

    def test_ma_context_counts_completed_sessions_and_excludes_today(self):
        position = {"code": "000001", "entry_at": "20260923100000"}
        # Sep 24/25/28 and the weekend are excluded by the synthetic calendar.
        context = self.momentum.exit_context(position, datetime(2026, 9, 30, 9, 6))
        self.assertEqual(context["elapsed_sessions"], 2)  # Sep 23 and Sep 29
        self.assertEqual(context["ma_date"], "20260929")
        self.assertEqual(context["close"], 16200)

    def test_manager_rejects_every_paper_profile_before_order_checks(self):
        manager = StrategyManager.__new__(StrategyManager)
        manager.strategies = [self.momentum]
        manager.last_buy_rejections = {}
        manager.kiwoom = self.kiwoom
        manager.get_event_driven_order_block_reason = Mock(side_effect=AssertionError("guard should precede live flow"))
        for profile in PROFILES:
            self.assertIsNone(manager.request_buy_order(profile, "000001", "Fixture", 17000, "test", "2004"))
            self.assertIn("가상 전용", manager.last_buy_rejections["000001"])
        self.kiwoom.send_order.assert_not_called()

    def test_legacy_virtual_processing_survives_paper_failure(self):
        virtual = VirtualStrategyEngine.__new__(VirtualStrategyEngine)
        virtual.paper_engine = SimpleNamespace(on_realtime_tick=Mock(side_effect=RuntimeError("fixture failure")))
        value = SimpleNamespace(strategy_name="ValueQualityStrategy", is_init_success=True, universe={"000001": {}})
        virtual.strategies = {value.strategy_name: value}
        virtual._get_cached_position = Mock(return_value=None)
        virtual._process_strategy = Mock()
        with patch("util.virtual_strategy_engine.check_transaction_open", return_value=True):
            virtual.on_realtime_tick("000001", {"현재가": 16600})
        virtual._process_strategy.assert_called_once()

    def test_ma_settings_apply_next_trading_day_and_not_holiday(self):
        save_ma_settings(20, 0.95, 0, self.high.ma_settings_db_path, now=datetime(2026, 9, 30, 12))
        self.high._ensure_ma_settings(datetime(2026, 9, 30, 13))
        self.assertEqual(self.high.active_ma_settings["revision"], 0)
        self.high._ensure_ma_settings(datetime(2026, 10, 5, 9))
        self.assertEqual(get_ma_settings_status(self.high.ma_settings_db_path)["applied"]["applied_date"], "20260930")
        self.high._ensure_ma_settings(datetime(2026, 10, 6, 9))
        self.assertEqual(self.high.active_ma_settings["ma_period"], 20)
        self.assertEqual(self.high.active_ma_settings["revision"], 1)

    def test_legacy_ma_freezes_first_tick_is_profit_only_and_atr_has_priority(self):
        state = new_atr_state_from_value(100, "20260923100000", 5, "20260922")
        signal, frozen = ma_profit_exit_signal(110, 100, state, 440, now=datetime(2026, 9, 30, 15, 19))
        self.assertIsNone(signal)
        self.assertAlmostEqual(frozen["ma_profit_stop"], 106.7)
        signal, again = ma_profit_exit_signal(105, 100, frozen, 400, now=datetime(2026, 9, 30, 15, 19, 30))
        self.assertEqual(signal["reason_code"], "MA_PROFIT_EXIT")
        self.assertEqual(again["ma_profit_stop"], 106.7)
        self.assertIsNone(ma_profit_exit_signal(99, 100, frozen, 440, now=datetime(2026, 9, 30, 15, 19, 45))[0])
        self.assertIsNone(ma_profit_exit_signal(105, 100, frozen, 440, now=datetime(2026, 9, 30, 15, 20))[0])
        self.high._atr_history = Mock(return_value=[])
        self.high._ensure_ma_settings = Mock(side_effect=AssertionError("ATR priority"))
        signal, _ = self.high.evaluate_sell_signal("000001", 89, 100, state["entry_at"], state, now=datetime(2026, 9, 30, 15, 19))
        self.assertEqual(signal["reason_code"], "ATR_INITIAL_STOP")


class PaperApiTest(unittest.TestCase):
    def test_endpoint_limits_and_paper_only_route(self):
        from fastapi.testclient import TestClient
        from web_api.main import app
        client = TestClient(app)
        with tempfile.TemporaryDirectory() as directory:
            from web_api import database
            missing = Path(directory) / "missing.db"
            with patch.object(database, "PAPER_DB", missing):
                url = "/api/strategies/MomentumContractionStrategy/paper-account"
                self.assertEqual(client.get(url).status_code, 200)
                self.assertFalse(client.get(url).json()["ready"])
                self.assertEqual(client.get(url + "?candidate_limit=201").status_code, 422)
                self.assertEqual(client.get("/api/strategies/HighBreakoutStrategy/paper-comparison").status_code, 404)
                self.assertEqual(client.put(url, json={}).status_code, 405)
            self.assertFalse(missing.exists())
