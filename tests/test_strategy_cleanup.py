"""Runtime wiring checks for two operational strategies and three paper profiles."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from paper_fixture import history, market_db, qt_stubs
qt_stubs()
from strategy.MomentumContractionStrategy import MomentumContractionStrategy
from strategy.StrategyManager import StrategyManager
from util.paper_strategy_engine import PaperComparisonEngine
from util.paper_breakout_baseline import PaperBreakoutBaseline
from util.paper_trading import BASELINE, FILTERED, MOMENTUM
from util.virtual_strategy_engine import VirtualStrategyEngine
from web_api.strategies import STRATEGIES
from util.const import get_fid

class StrategyCleanupTest(unittest.TestCase):
    def test_automatic_baseline_operates_without_live_high_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = market_db(Path(directory) / "market.db", {"000001": history()})
            now = datetime(2026, 9, 30, 8, 59)
            kiwoom = SimpleNamespace(send_order=Mock())
            momentum = MomentumContractionStrategy(kiwoom, market_db_path=path, clock=lambda: now)
            momentum.check_and_get_universe()
            momentum.check_and_get_price_data()
            momentum.is_init_success = True
            momentum.on_realtime_registered()
            engine = PaperComparisonEngine([momentum], Path(directory) / "paper.db", record_ticks=False)
            self.assertIsInstance(engine.baseline, PaperBreakoutBaseline)
            self.assertFalse(hasattr(engine.baseline, "order_sell"))
            self.assertFalse(hasattr(engine.baseline, "check_buy_signal_and_order"))
            self.assertIn("000001", engine.get_candidate_codes())
            now = datetime(2026, 9, 30, 9, 0)
            engine.on_realtime_tick("000001", {"체결시간": "090000", "현재가": 16200, "저가": 16100, "누적거래량": 250000, "거래량": 250000})
            now = datetime(2026, 9, 30, 9, 5)
            engine.on_realtime_tick("000001", {"체결시간": "090500", "현재가": 16600, "저가": 16100, "누적거래량": 250100, "거래량": 100})
            self.assertEqual({profile for profile, _ in engine.broker.positions}, {BASELINE, FILTERED, MOMENTUM})
            kiwoom.send_order.assert_not_called()

    def test_shared_registration_includes_baseline_and_all_paper_holdings(self):
        manager = StrategyManager.__new__(StrategyManager)
        kiwoom = SimpleNamespace(set_stock_realtime_fid_names=Mock(), set_real_reg=Mock(return_value=0))
        manager.kiwoom = kiwoom
        value = SimpleNamespace(strategy_name="ValueQualityStrategy", is_init_success=True, event_driven=True, get_realtime_candidate_codes=lambda: ["000001"])
        momentum = SimpleNamespace(strategy_name=MOMENTUM, is_init_success=True, event_driven=True, get_realtime_candidate_codes=lambda: ["000002"], on_realtime_registered=Mock())
        manager.strategies = [value, momentum]
        manager.virtual_strategy_engine = SimpleNamespace(get_open_codes=lambda: ["000004"], paper_engine=SimpleNamespace(get_candidate_codes=lambda: ["000003"]))
        codes = manager.register_shared_event_realtime(manager.strategies)
        self.assertEqual(codes, ["000001", "000002", "000003", "000004"])
        fids = kiwoom.set_real_reg.call_args.args[2].split(";")
        for name in ("체결시간", "거래량", "누적거래량", "시가", "고가", "저가", "현재가"):
            self.assertIn(get_fid(name), fids)
        momentum.on_realtime_registered.assert_called_once()

    def test_inactive_virtual_holdings_are_preserved_but_not_loaded(self):
        engine = VirtualStrategyEngine.__new__(VirtualStrategyEngine)
        engine.strategies = {"ValueQualityStrategy": object()}
        engine.dirty_position_keys = set()
        rows = [{"strategy_name": "PullbackTrendStrategy", "code": "000001"}, {"strategy_name": "ValueQualityStrategy", "code": "000002"}]
        with patch("util.virtual_strategy_engine.get_all_virtual_positions", return_value=rows):
            engine.reload_positions_from_db()
        self.assertEqual(set(engine.position_cache), {("ValueQualityStrategy", "000002")})
        self.assertEqual(len(rows), 2)

    def test_only_two_operational_web_strategies_remain(self):
        self.assertEqual(set(STRATEGIES), {"ValueQualityStrategy", MOMENTUM})

    def test_comparison_ma_endpoint_keeps_saved_revisions(self):
        from fastapi.testclient import TestClient
        from web_api.main import app
        from util.strategy_settings import get_ma_settings_status, save_ma_settings
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "settings.db"
            with patch("web_api.main.get_ma_settings_status", side_effect=lambda: get_ma_settings_status(db)), patch("web_api.main.save_ma_settings", side_effect=lambda p, r, v: save_ma_settings(p, r, v, db, now=datetime(2026, 9, 30, 12))):
                client = TestClient(app)
                url = "/api/strategies/MomentumContractionStrategy/ma-settings"
                self.assertEqual(client.get(url).status_code, 200)
                result = client.put(url, json={"ma_period": 10, "exit_ratio": 0.95, "expected_revision": 0})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["saved"]["revision"], 1)
                self.assertEqual(client.get("/api/strategies/HighBreakoutStrategy/ma-settings").status_code, 404)
