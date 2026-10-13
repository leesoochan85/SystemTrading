"""Shared ATR invariants used by current momentum entry and stop checks."""
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import unittest

from util.atr import iter_atr_bars, load_atr_metrics


class AtrTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'market.db'
        self.rows = [('20260101', 11, 9, 10), ('20260102', 13, 11, 12),
                     ('20260103', 13, 10, 11), ('20260104', 12, 10, 11),
                     ('20260105', 15, 12, 14)]

    def create_db(self, stocks):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('CREATE TABLE daily_price (code TEXT, date TEXT, high REAL, low REAL, close REAL)')
            for code, rows in stocks.items():
                con.executemany('INSERT INTO daily_price VALUES (?,?,?,?,?)', [(code, *row) for row in rows])

    def test_wilder_atr_includes_overnight_gap(self):
        bars = list(iter_atr_bars(self.rows, period=3))
        self.assertAlmostEqual(bars[0]['atr'], 8 / 3)
        self.assertAlmostEqual(bars[1]['atr'], 28 / 9)

    def test_cutoff_excludes_current_and_future_bars_without_changing_db(self):
        self.create_db({'000001': self.rows + [('20260106', 1000, 900, 950)]})
        original = self.path.read_bytes()
        metric = load_atr_metrics(['1'], self.path, '20260105', period=3)['000001']
        self.assertEqual(metric['atr_date'], '20260104')
        self.assertAlmostEqual(metric['atr14'], 8 / 3)
        self.assertEqual(self.path.read_bytes(), original)

    def test_14_period_seed_requires_15_completed_bars(self):
        rows = [(f'202601{i:02d}', 11, 9, 10) for i in range(1, 16)]
        self.assertEqual(list(iter_atr_bars(rows[:14])), [])
        self.assertEqual(list(iter_atr_bars(rows)), [{'date': '20260115', 'close': 10.0, 'atr': 2.0}])

    def test_invalid_ohlc_and_non_increasing_dates_are_rejected(self):
        for rows in [[('20260101', float('nan'), 9, 10)],
                     [('20260101', 11, 0, 10)], [('20260101', 11, 9, 12)],
                     [self.rows[1], self.rows[0]], [self.rows[0], self.rows[0]]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                list(iter_atr_bars(rows, period=3))

    def test_invalid_symbol_does_not_remove_valid_symbols(self):
        self.create_db({'000001': [('20260101', 11, 9, 12)], '000002': self.rows})
        with redirect_stdout(StringIO()):
            metrics = load_atr_metrics(['000001', '000002'], self.path, '20260106', period=3)
        self.assertEqual(set(metrics), {'000002'})
        self.assertAlmostEqual(metrics['000002']['atr14'], 28 / 9)

    def test_selected_symbols_only_are_returned(self):
        self.create_db({'000001': self.rows, '000002': self.rows})
        self.assertEqual(set(load_atr_metrics(['2'], self.path, '20260106', period=3)), {'000002'})
        self.assertEqual(load_atr_metrics([], self.path, '20260106', period=3), {})

    def test_missing_database_is_never_created(self):
        with self.assertRaises(sqlite3.OperationalError):
            load_atr_metrics(['000001'], self.path, '20260106')
        self.assertFalse(self.path.exists())
