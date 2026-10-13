"""Synthetic completed history and Qt stubs; never opens a trading session."""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
import types

from util.momentum_breakout import load_config
from util.time_helper import is_market_business_day


def history(before="20260930", count=180):
    day = datetime.strptime(before, "%Y%m%d") - timedelta(days=1)
    dates = []
    while len(dates) < count:
        if is_market_business_day(day):
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    rows = []
    for i, date in enumerate(reversed(dates)):
        if i < count - 20:
            close = 10000 if i < count - 80 else round(10000 + 6000 * (i - count + 80) / 59)
            high, low = close + 350, close - 350
        elif i < count - 5:
            close, high, low = 16000, 16500, 15500
        else:
            close, high, low = 16200, 16300, 15800
        rows.append(dict(date=date, open=close, high=high, low=low, close=close, volume=200000))
    return rows


def market_db(path, series):
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("""CREATE TABLE daily_price (code TEXT, date TEXT, open INTEGER,
            high INTEGER, low INTEGER, close INTEGER, volume INTEGER,
            source TEXT, updated_at TEXT, PRIMARY KEY(code,date))""")
        con.execute("""CREATE TABLE stock_master (code TEXT PRIMARY KEY,
            code_name TEXT, market TEXT, active INTEGER, updated_at TEXT)""")
        for code, rows in series.items():
            con.execute("INSERT INTO stock_master VALUES (?,?,?,1,?)", (code, "Fixture " + code, "KOSPI", "20260929160000"))
            con.executemany("INSERT INTO daily_price VALUES (?,?,?,?,?,?,?,'fixture','20260929160000')",
                [(code, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows])
    return Path(path)


def qt_stubs():
    pyqt = sys.modules.setdefault("PyQt5", types.ModuleType("PyQt5"))
    core = sys.modules.setdefault("PyQt5.QtCore", types.ModuleType("PyQt5.QtCore"))
    pyqt.QtCore = core
    for name in ("QThread", "QObject", "QTimer"):
        if not hasattr(core, name):
            setattr(core, name, type(name, (), {}))
    if not hasattr(core, "QCoreApplication"):
        core.QCoreApplication = type("QCoreApplication", (), {
            "instance": staticmethod(lambda: None),
        })
    notifier = sys.modules.setdefault("util.notifier", types.ModuleType("util.notifier"))
    notifier.send_message = lambda *args, **kwargs: None
    notifier.send_error = lambda *args, **kwargs: None
    notifier.get_updates = lambda *args, **kwargs: []


def config():
    return load_config()
