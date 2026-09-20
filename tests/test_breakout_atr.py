import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

# 테스트 환경에는 32bit Kiwoom용 PyQt5가 없으므로 QThread 인터페이스만 대체한다.
if "PyQt5.QtCore" not in sys.modules:
    pyqt5 = types.ModuleType("PyQt5")
    qtcore = types.ModuleType("PyQt5.QtCore")

    class QThread:
        pass

    qtcore.QThread = QThread
    pyqt5.QtCore = qtcore
    sys.modules["PyQt5"] = pyqt5
    sys.modules["PyQt5.QtCore"] = qtcore

if "util.notifier" not in sys.modules:
    notifier = types.ModuleType("util.notifier")
    notifier.send_message = lambda *args, **kwargs: None
    sys.modules["util.notifier"] = notifier

from util.breakout_atr import (
    AtrStateStore,
    advance_atr_state,
    atr_exit_signal,
    iter_atr_bars,
    new_atr_state,
    new_atr_state_from_value,
)
from util.market_history import load_breakout_metrics
from strategy.HighBreakoutStrategy import HighBreakoutStrategy


class BreakoutAtrTest(unittest.TestCase):
    def test_high_zone_rebound_buy_boundaries(self):
        strategy = HighBreakoutStrategy(types.SimpleNamespace(), auto_init=False)
        strategy.metrics_date = datetime.now().strftime("%Y%m%d")
        strategy.breakout_metrics = {
            "000001": {
                "breakout_price": 100,
                "prev_high": 94,
                "atr14": 2,
                "atr_date": "20260101",
                "volume_ma20": 10,
                "trading_value_ma20": 2_000_000_000,
            }
        }

        def signal(price, previous_high=94):
            strategy.breakout_metrics["000001"]["prev_high"] = previous_high
            return strategy._get_buy_signal(
                "000001", {"현재가": price, "누적거래량": 10}
            )

        self.assertIsNotNone(signal(95))
        self.assertIsNotNone(signal(100, previous_high=99))
        self.assertIsNotNone(signal(101, previous_high=99))
        self.assertIsNotNone(signal(120, previous_high=99))
        self.assertIsNone(signal(94))
        self.assertIsNone(signal(96, previous_high=97))

    def test_wilder_atr_includes_overnight_gap(self):
        rows = [
            ("20260101", 11, 9, 10),
            ("20260102", 13, 11, 12),  # TR 3: 전일 종가 10 -> 고가 13
            ("20260103", 13, 10, 11),  # TR 3
            ("20260104", 12, 10, 11),  # TR 2, 초기 ATR = 8/3
            ("20260105", 15, 12, 14),  # TR 4, Wilder ATR = (8/3*2+4)/3
        ]
        bars = list(iter_atr_bars(rows, period=3))
        self.assertAlmostEqual(bars[0]["atr"], 8 / 3)
        self.assertAlmostEqual(bars[1]["atr"], 28 / 9)

    def test_atr_stop_never_moves_down(self):
        bars = [
            {"date": "20260109", "close": 98.0, "atr": 2.0},
            {"date": "20260110", "close": 101.0, "atr": 2.0},
            {"date": "20260111", "close": 110.0, "atr": 3.0},
        ]
        state = new_atr_state(100, "20260110100000", bars, initial_multiple=2)
        self.assertEqual(state["stop_price"], 96)

        raised = advance_atr_state(state, 100, bars, trailing_multiple=3)
        self.assertEqual(raised["stop_price"], 101)  # max(96, 110 - 3*3)

        wider_atr = bars + [{"date": "20260112", "close": 108.0, "atr": 10.0}]
        unchanged = advance_atr_state(raised, 100, wider_atr, trailing_multiple=3)
        self.assertEqual(unchanged["stop_price"], 101)
        self.assertEqual(atr_exit_signal(101, unchanged)["reason_code"], "ATR_TRAILING_STOP")

    def test_state_store_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AtrStateStore(Path(directory) / "atr.db")
            state = {
                "entry_at": "20260110100000",
                "entry_price": 100.0,
                "entry_atr": 2.0,
                "initial_stop": 96.0,
                "stop_price": 101.0,
                "highest_close": 110.0,
                "last_bar_date": "20260111",
            }
            store.save("1234", state)
            self.assertEqual(store.load("001234"), state)
            store.delete("001234")
            self.assertIsNone(store.load("001234"))

    def test_existing_position_can_migrate_from_latest_atr(self):
        state = new_atr_state_from_value(
            100, "20250102100000", 4, "20260110", initial_multiple=2
        )
        self.assertEqual(state["initial_stop"], 92)
        self.assertEqual(state["last_bar_date"], "20260110")

    def test_breakout_metrics_exclude_cutoff_date(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "market.db"
            with closing(sqlite3.connect(db_path)) as con, con:
                con.execute(
                    "CREATE TABLE daily_price ("
                    "code TEXT, date TEXT, open INTEGER, high INTEGER, low INTEGER, "
                    "close INTEGER, volume INTEGER)"
                )
                con.executemany(
                    "INSERT INTO daily_price VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("000001", "20260101", 90, 100, 85, 95, 10),
                        ("000001", "20260102", 95, 105, 90, 100, 20),
                        ("000001", "20260103", 100, 103, 95, 101, 30),
                        # 장중/당일 행이 존재하더라도 cutoff에서 제외되어야 한다.
                        ("000001", "20260104", 101, 999, 100, 998, 999),
                    ],
                )
            metrics = load_breakout_metrics(
                ["000001"], breakout_window=3, volume_window=2, ma_window=2,
                db_path=db_path, before_date="20260104",
            )
            self.assertEqual(metrics["000001"]["breakout_price"], 105)
            self.assertEqual(metrics["000001"]["prev_high"], 103)
            self.assertEqual(metrics["000001"]["history_last_date"], "20260103")


if __name__ == "__main__":
    unittest.main()
