"""Shared tick processing with separate paper cash; no broker login/orders."""
from contextlib import closing, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from paper_fixture import history, market_db, qt_stubs
qt_stubs()
from strategy.LiveMomentumContractionStrategy import LiveMomentumContractionStrategy
from strategy.ValueQualityStrategy import ValueQualityStrategy
from util.paper_strategy_engine import MomentumPaperEngine
from util.paper_trading import MOMENTUM
from util.virtual_strategy_engine import VirtualStrategyEngine
from web_api.paper_queries import get_paper_account
from web_api.query_context import ReadOnlyDatabase


class MomentumParallelTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.now = datetime(2026, 9, 30, 8, 59)
        market = market_db(root / "market.db", {"000001": history()})
        self.broker = SimpleNamespace(account_number="TEST", balance={}, order={},
            universe_realtime_transaction_info={}, send_order=Mock(return_value=0))
        self.live = LiveMomentumContractionStrategy(self.broker, market_db_path=market,
            live_db_path=root / "live.db", clock=lambda: self.now, allow_new_buys=False)
        self.live.check_and_get_universe()
        self.live.is_init_success = True
        self.live.set_order_guard(lambda: False)
        self.live.set_buy_order_handler(Mock())
        self.paper = MomentumPaperEngine([self.live], root / "paper.db", record_ticks=False)
        self.console = StringIO()
        with redirect_stdout(self.console):
            self.live.check_and_get_price_data()
        self.live.on_realtime_registered()
        self.virtual = VirtualStrategyEngine.__new__(VirtualStrategyEngine)
        self.virtual.paper_engine = self.paper
        self.virtual.strategies = {}
        self.volume = 0

    def tick(self, at, price):
        self.now = datetime.strptime("20260930" + at, "%Y%m%d%H%M%S")
        quantity = 250000 if not self.volume else 100
        self.volume += quantity
        tick = dict(체결시간=at, 현재가=price, 저가=16100, 누적거래량=self.volume, 거래량=quantity)
        with patch("util.virtual_strategy_engine.check_transaction_open", return_value=True), \
             patch("strategy.LiveMomentumContractionStrategy.get_position_strategy", return_value=None):
            self.virtual.on_realtime_tick("000001", tick)
            self.live.on_realtime_tick("000001", tick)

    def test_full_real_account_zero_cash_still_allows_paper_entry_and_exit(self):
        self.live.allow_new_buys = True  # Even an enabled broker adapter cannot gate the paper account.
        self.broker.balance = {str(900000+i): {"보유수량": 1} for i in range(10)}
        self.broker.deposit = 0
        self.tick("090000", 16200)
        self.tick("090459", 16400)
        self.tick("090500", 16600)
        self.assertIn((MOMENTUM, "000001"), self.paper.broker.positions)
        self.live.buy_order_handler.assert_not_called()
        self.broker.send_order.assert_not_called()
        self.tick("090600", 16000)
        self.assertNotIn((MOMENTUM, "000001"), self.paper.broker.positions)
        with closing(sqlite3.connect(self.paper.broker.db_path)) as con:
            self.assertEqual([r[0] for r in con.execute("SELECT event_type FROM paper_trade ORDER BY trade_id")], ["BUY", "SELL"])

    def test_value_order_handler_and_momentum_paper_entry_both_process_ticks(self):
        value = ValueQualityStrategy.__new__(ValueQualityStrategy)
        value.kiwoom = self.broker
        value.is_init_success = True
        value.value_db_path = Path(self.temp.name) / "value.db"
        value.universe = {"000002": dict(code_name="Value fixture", per_ttm=10,
            pbr=.6, gross_margin_pct=40, asset_turnover=1.2)}
        value._is_qualified_candidate = lambda code: True
        value.order_guard = lambda: True
        value.buy_order_handler = Mock(return_value={"quantity": 1})
        self.tick("090000", 16200)
        self.tick("090500", 16600)
        with patch("strategy.ValueQualityStrategy.check_transaction_open", return_value=True), \
             patch("strategy.ValueQualityStrategy.check_adjacent_transaction_closed_for_buying", return_value=False), \
             patch("strategy.ValueQualityStrategy.get_position_strategy", return_value=None), \
             patch("strategy.ValueQualityStrategy.save_strategy_signal", return_value="TEST"), \
             patch("strategy.ValueQualityStrategy.update_strategy_signal_order_result"), \
             patch("strategy.ValueQualityStrategy.save_position_strategy"), \
             patch("strategy.ValueQualityStrategy.save_value_position_state"), \
             patch("strategy.ValueQualityStrategy.send_message"):
            value.on_realtime_tick("000002", {"현재가": 1000, "(최우선)매수호가": 1000})
        value.buy_order_handler.assert_called_once()
        self.assertIn((MOMENTUM, "000001"), self.paper.broker.positions)
        self.live.buy_order_handler.assert_not_called()

    def test_no_candidates_still_publishes_equity_and_heartbeat_without_trade(self):
        self.live.metrics = {}
        self.live.stats["qualified"] = 0
        self.now = datetime(2026, 9, 30, 10)
        self.live.watchlist_writer(self.live.metrics_date, {}, self.live.universe, self.live.stats, self.now)
        self.assertFalse(self.paper.dirty)
        self.paper.flush_snapshot_if_due(force=True)
        account = get_paper_account(ReadOnlyDatabase(self.paper.broker.db_path))
        status = account["stats"]["paper_execution"]
        self.assertEqual(status["phase"], "NO_CANDIDATES")
        self.assertEqual(status["heartbeat_at"], "20260930100000")
        self.assertTrue(status["paper_enabled"])
        self.assertFalse(status["live_new_buys_enabled"])
        self.assertEqual(account["accounts"][0]["equity"], 10000000)
        self.assertEqual(len(account["equity_history"]), 1)
        self.assertEqual(account["trades"], [])
        with patch.object(self.paper.broker, "snapshot") as save, patch("util.paper_strategy_engine.time.monotonic", return_value=self.paper.last_snapshot_at+1):
            self.paper.flush_snapshot_if_due()
        save.assert_not_called()

    def test_late_start_is_reported_and_not_bypassed_for_paper(self):
        self.now = datetime(2026, 9, 30, 10)
        self.live.on_realtime_registered()
        self.paper.flush_snapshot_if_due(force=True)
        self.assertEqual(self.paper.execution_status(self.now)["phase"], "OPENING_RANGE_MISSING")
        self.tick("100001", 16600)
        self.assertFalse(self.paper.broker.positions)

    def test_candidate_log_explains_all_filter_stages(self):
        text = self.console.getvalue()
        for term in ("유효 일봉", "상위", "ADR", "평균 거래대금", "선행 상승", "변동폭 수축", "저점 상승/최종"):
            self.assertIn(term, text)


if __name__ == "__main__":
    unittest.main()
