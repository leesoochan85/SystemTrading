"""Pinned API contracts and independent read-only database configuration."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import inspect
import json
import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from web_api import database, virtual_queries
from web_api.query_context import ReadOnlyDatabase, VirtualQueryContext
from web_query_fixture import seed_history, responses, STRATEGY


class QueryRefactorTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'history.db'
        seed_history(self.path)
        self.contract = json.loads((Path(__file__).parent / 'fixtures/web_query_contract.json').read_text(encoding='utf-8'))

    def test_public_signatures_and_pinned_response_contract_are_unchanged(self):
        for name, signature in self.contract['signatures'].items():
            with self.subTest(function=name):
                self.assertEqual(str(inspect.signature(getattr(database, name))), signature)
        with closing(sqlite3.connect(self.path)) as con:
            before = list(con.iterdump())
        with patch.object(database, 'MONITORING_DB', self.path):
            self.assertEqual(responses(), self.contract['responses'])
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(list(con.iterdump()), before)

    def test_imported_facade_functions_use_current_path_and_clock(self):
        query = database.get_virtual_event_summary
        class FixedDatetime(datetime):
            @classmethod
            def now(cls): return cls(2026, 10, 13, 12)
        with patch.object(database, 'MONITORING_DB', self.path), patch.object(database, 'datetime', FixedDatetime):
            self.assertEqual(query(STRATEGY), self.contract['responses']['event_summary'])
        missing = self.path.parent / 'missing.db'
        with patch.object(database, 'MONITORING_DB', missing):
            self.assertEqual(query(STRATEGY), {'buy': 0, 'sell': 0, 'total': 0})
            self.assertEqual(database.get_virtual_positions(), [])
        self.assertFalse(missing.exists())

    def test_explicit_contexts_are_isolated_from_facade_paths(self):
        context = VirtualQueryContext(ReadOnlyDatabase(self.path), ReadOnlyDatabase(self.path.parent / 'paper.db'))
        with patch.object(database, 'MONITORING_DB', self.path.parent / 'missing.db'):
            result = virtual_queries.get_virtual_performance(context, STRATEGY)
            self.assertEqual(result, self.contract['responses']['performance'])
            self.assertEqual(database.get_virtual_positions(), [])

    def test_read_only_connection_rejects_writes_and_closes_on_both_exit_paths(self):
        reader = ReadOnlyDatabase(self.path, busy_timeout_ms=1234)
        with reader.connect() as con:
            self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone()[0], 1234)
            self.assertEqual(con.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM virtual_position')
        with self.assertRaises(sqlite3.ProgrammingError):
            con.execute('SELECT 1')
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with reader.connect() as failed:
                raise RuntimeError('fixture')
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute('SELECT 1')
        self.assertTrue(reader.table_exists('virtual_position'))
        self.assertFalse(reader.table_exists('missing_table'))
        with self.assertRaises(database.DatabaseNotReadyError):
            with ReadOnlyDatabase(self.path.parent / 'absent.db').connect():
                pass

    def test_importing_query_and_report_modules_has_no_startup_side_effects(self):
        root = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c',
                "import sys; import web_api.database; import strategy.value_report_service; "
                "assert 'PyQt5' not in sys.modules; assert 'api.Kiwoom' not in sys.modules"],
                cwd=directory, env={**os.environ, 'PYTHONPATH': root}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(list(Path(directory).iterdir()), [])
