"""신고가 MA 수익청산 설정: 웹 저장, 매매 프로세스의 다음 거래일 적용."""
from __future__ import annotations

import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


SETTINGS_DB = Path(os.getenv(
    "SYSTEMTRADING_ROOT", str(Path(__file__).resolve().parents[1])
)) / "strategy_settings.db"
DEFAULT_MA_PERIOD = 5
DEFAULT_MA_EXIT_RATIO = 0.97


class SettingsConflictError(ValueError):
    pass


def validate_ma_settings(ma_period, exit_ratio):
    if isinstance(ma_period, bool) or not isinstance(ma_period, int) or not 2 <= ma_period <= 60:
        raise ValueError("MA 기간은 2~60 사이의 정수여야 합니다")
    if isinstance(exit_ratio, bool) or not isinstance(exit_ratio, (int, float)):
        raise ValueError("MA 기준 비율은 숫자여야 합니다")
    if not math.isfinite(float(exit_ratio)) or not 0.80 <= float(exit_ratio) < 1.0:
        raise ValueError("MA 기준 비율은 0.80 이상 1.00 미만이어야 합니다")
    return int(ma_period), float(exit_ratio)


def _connect(db_path):
    con = sqlite3.connect(str(db_path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def _init(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS ma_settings (
            revision INTEGER PRIMARY KEY,
            ma_period INTEGER NOT NULL,
            exit_ratio REAL NOT NULL,
            saved_date TEXT NOT NULL,
            saved_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ma_settings_applied (
            strategy_name TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            ma_period INTEGER NOT NULL,
            exit_ratio REAL NOT NULL,
            applied_date TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)


def _default():
    return {"revision": 0, "ma_period": DEFAULT_MA_PERIOD,
            "exit_ratio": DEFAULT_MA_EXIT_RATIO, "saved_date": None, "saved_at": None}


def get_ma_settings_status(db_path=SETTINGS_DB):
    with closing(_connect(db_path)) as con:
        _init(con)
        latest = con.execute("SELECT * FROM ma_settings ORDER BY revision DESC LIMIT 1").fetchone()
        applied = con.execute(
            "SELECT * FROM ma_settings_applied WHERE strategy_name = 'HighBreakoutStrategy'"
        ).fetchone()
    saved = dict(latest) if latest else _default()
    active = dict(applied) if applied else None
    return {
        "saved": saved,
        "applied": active,
        "pending": saved["revision"] > (active["revision"] if active else 0),
        "effective_policy": "저장한 날짜 이후 첫 거래일에 매매 프로세스가 적용",
    }


def save_ma_settings(ma_period, exit_ratio, expected_revision, db_path=SETTINGS_DB,
                     now=None):
    ma_period, exit_ratio = validate_ma_settings(ma_period, exit_ratio)
    now = now or datetime.now()
    with closing(_connect(db_path)) as con:
        with con:
            con.execute("BEGIN IMMEDIATE")
            _init(con)
            row = con.execute("SELECT COALESCE(MAX(revision), 0) FROM ma_settings").fetchone()
            current = int(row[0])
            if current != expected_revision:
                raise SettingsConflictError("설정이 다른 화면에서 변경되었습니다. 새로고침 후 다시 저장하세요")
            con.execute(
                "INSERT INTO ma_settings VALUES (?, ?, ?, ?, ?)",
                (current + 1, ma_period, exit_ratio, now.strftime("%Y%m%d"),
                 now.strftime("%Y%m%d%H%M%S")),
            )
    return get_ma_settings_status(db_path)


def load_ma_settings_for_date(trade_date, db_path=SETTINGS_DB):
    """저장일보다 늦은 거래일에만 새 버전을 선택한다. 당일 재시작에도 동일."""
    with closing(_connect(db_path)) as con:
        _init(con)
        row = con.execute(
            "SELECT * FROM ma_settings WHERE saved_date < ? "
            "ORDER BY revision DESC LIMIT 1", (trade_date,),
        ).fetchone()
    values = dict(row) if row else _default()
    period, ratio = validate_ma_settings(values["ma_period"], values["exit_ratio"])
    return {**values, "ma_period": period, "exit_ratio": ratio}


def acknowledge_ma_settings(settings, trade_date, db_path=SETTINGS_DB, now=None):
    """매매 프로그램이 실제 활성화한 버전만 적용 확인으로 기록한다."""
    now = now or datetime.now()
    with closing(_connect(db_path)) as con:
        with con:
            _init(con)
            con.execute(
                "INSERT OR REPLACE INTO ma_settings_applied VALUES (?, ?, ?, ?, ?, ?)",
                ("HighBreakoutStrategy", settings["revision"], settings["ma_period"],
                 settings["exit_ratio"], trade_date, now.strftime("%Y%m%d%H%M%S")),
            )
