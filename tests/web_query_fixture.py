"""Deterministic mixed open, partial and fully closed virtual history."""
from pathlib import Path
from unittest.mock import patch
from util import virtual_trading as ledger
from web_api import database

STRATEGY = 'ValueQualityStrategy'

def seed_history(path):
    with patch.object(ledger, 'MONITORING_DB', Path(path)), patch.object(ledger, '_now', return_value='20261012090000'):
        ledger.open_virtual_position(STRATEGY, '000001', '부분보유', 100, 'ENTRY', '진입', {'stop': 90})
        ledger.open_virtual_position(STRATEGY, '000002', '완료거래', 200, 'ENTRY', '진입')
        ledger.open_virtual_position('OtherStrategy', '000003', '다른전략', 300, 'ENTRY', '진입')
        ledger.save_virtual_strategy_daily_snapshots([STRATEGY], '20261012')
    with patch.object(ledger, 'MONITORING_DB', Path(path)), patch.object(ledger, '_now', return_value='20261013100000'):
        ledger.close_virtual_position(STRATEGY, '000001', 110, 'PARTIAL', '부분청산', exit_ratio=0.3)
        ledger.close_virtual_position(STRATEGY, '000002', 180, 'STOP', '전량청산')
        ledger.update_virtual_position(STRATEGY, '000001', 120)
        ledger.save_virtual_strategy_daily_snapshots([STRATEGY], '20261013')

def responses():
    return {
        'positions': database.get_virtual_positions(STRATEGY),
        'all_positions': database.get_virtual_positions(),
        'trades': database.get_virtual_trades(STRATEGY),
        'paged_trades': database.get_virtual_trades(STRATEGY, limit=1, offset=1),
        'events': database.get_virtual_events(STRATEGY),
        'event_summary': database.get_virtual_event_summary(STRATEGY, '20261013'),
        'daily': database.get_virtual_daily_performance(STRATEGY),
        'daily_filtered': database.get_virtual_daily_performance(STRATEGY, '20261013', '20261013'),
        'index': database.get_virtual_strategy_index_history(STRATEGY),
        'index_filtered': database.get_virtual_strategy_index_history(STRATEGY, '20261013', '20261013'),
        'performance': database.get_virtual_performance(STRATEGY),
        'missing_strategy': database.get_virtual_performance('MissingStrategy'),
    }
