from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "value_quality.db"
SQLITE_BUSY_TIMEOUT_MS = 15_000
MAX_QUOTE_AGE_DAYS = 7
MAX_FINANCIAL_AGE_DAYS = 240
MAX_ANNUAL_AGE_DAYS = 550
QUALITY_BASIS = "TTM; revenue/mean(TTM_start,end assets)"


@contextmanager
def _connect(db_path: Path = DB_PATH):
    """Commit/rollback the transaction, then close the OS file handle."""
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(
        db_path,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    try:
        con.row_factory = sqlite3.Row
        con.execute(
            f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}"
        )
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        with con:
            yield con
    finally:
        con.close()


def get_db_path() -> Path:
    return DB_PATH


def init_value_quality_tables(
    db_path: Path = DB_PATH,
):
    """
    기존 value_quality.db가 이미 있더라도 안전하게 필요한 테이블만 만든다.

    과거 버전의 value_earnings_event 테이블이 남아 있어도 사용하지 않는다.
    자동 삭제하지 않는 이유는 기존 사용자 데이터를 임의로 파괴하지 않기 위함이다.
    """
    with _connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS value_fundamental_snapshot (
                code TEXT PRIMARY KEY,
                code_name TEXT NOT NULL,
                as_of_date TEXT NOT NULL,

                per_ttm REAL,
                pbr REAL,
                gross_margin_pct REAL,
                asset_turnover REAL,

                latest_annual_year TEXT,
                source_note TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        columns = {row[1] for row in con.execute("PRAGMA table_info(value_fundamental_snapshot)")}
        for name, definition in (
            ("validated", "INTEGER NOT NULL DEFAULT 0"),
            ("financial_period_end", "TEXT"),
            ("annual_period_end", "TEXT"),
            ("validation_note", "TEXT"),
        ):
            if name not in columns:
                con.execute(f"ALTER TABLE value_fundamental_snapshot ADD COLUMN {name} {definition}")
        con.execute("""CREATE TABLE IF NOT EXISTS value_collection_status (
            code TEXT PRIMARY KEY, checked_at TEXT NOT NULL, reason TEXT NOT NULL
        )""")

        con.execute("""
            CREATE TABLE IF NOT EXISTS value_position_state (
                code TEXT PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                entry_price REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # 과거 스키마에 baseline_report_date /
        # last_processed_report_key가 있어도 그대로 둔다.
        # 새 코드에서는 사용하지 않는다.

        con.execute("""
            CREATE TABLE IF NOT EXISTS value_sent_report (
                report_key TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                title TEXT,
                broker TEXT,
                report_date TEXT,
                url TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_value_sent_report_code_date
            ON value_sent_report(code, report_date)
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS value_report_scan_state (
                state_key TEXT PRIMARY KEY,
                last_report_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def fundamental_is_fresh(info, today=None):
    """Date freshness and complete finite metrics; not strategy thresholds."""
    today = today or datetime.now().date()
    if info.get("validated") != 1 or not info.get("source_note"):
        return False
    try:
        source = json.loads(info["source_note"])
        if source.get("quality_basis") != QUALITY_BASIS:
            return False
        for name in ("per_ttm", "pbr", "gross_margin_pct", "asset_turnover"):
            if not math.isfinite(float(info.get(name))):
                return False
        if float(info["per_ttm"]) <= 0 or float(info["pbr"]) <= 0:
            return False
        dates = {}
        for field, max_age in (("as_of_date", MAX_QUOTE_AGE_DAYS),
                               ("financial_period_end", MAX_FINANCIAL_AGE_DAYS),
                               ("annual_period_end", MAX_ANNUAL_AGE_DAYS)):
            value = str(info.get(field) or "")
            if len(value) != 8 or not value.isdigit():
                return False
            day = datetime.strptime(value, "%Y%m%d").date()
            if not 0 <= (today - day).days <= max_age:
                return False
            dates[field] = day
        if not dates["annual_period_end"] <= dates["financial_period_end"] <= dates["as_of_date"]:
            return False
    except (TypeError, ValueError, OverflowError, AttributeError):
        return False
    return True


def mark_fundamental_invalid(code, reason, db_path=DB_PATH):
    """Keep previous values for audit but stop using failed refresh rows."""
    init_value_quality_tables(db_path)
    code = str(code).strip().zfill(6)
    with _connect(db_path) as con:
        con.execute("UPDATE value_fundamental_snapshot SET validated=0, validation_note=? WHERE code=?",
                    (str(reason), code))
        con.execute("""INSERT INTO value_collection_status VALUES (?,?,?)
            ON CONFLICT(code) DO UPDATE SET checked_at=excluded.checked_at, reason=excluded.reason""",
            (code, datetime.now().strftime("%Y%m%d%H%M%S"), str(reason)))


def get_fundamental_status(db_path=DB_PATH):
    init_value_quality_tables(db_path)
    with _connect(db_path) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM value_fundamental_snapshot")]
        reasons = [dict(r) for r in con.execute("SELECT reason, COUNT(*) count FROM value_collection_status GROUP BY reason")]
    return {"total": len(rows), "validated": sum(r["validated"] == 1 for r in rows),
            "fresh": sum(fundamental_is_fresh(r) for r in rows),
            "qualified": len(load_value_candidates(db_path=db_path)), "reasons": reasons}


def upsert_fundamental_snapshot(
    code: str,
    code_name: str,
    as_of_date: str,
    per_ttm: Optional[float],
    pbr: Optional[float],
    gross_margin_pct: Optional[float],
    asset_turnover: Optional[float],
    latest_annual_year: Optional[str] = None,
    source_note: Optional[str] = None,
    db_path: Path = DB_PATH,
    *, validated: bool = False,
    financial_period_end: Optional[str] = None,
    annual_period_end: Optional[str] = None,
    validation_note: Optional[str] = None,
):
    init_value_quality_tables(db_path)

    now_text = datetime.now().strftime("%Y%m%d%H%M%S")
    code = str(code).strip().zfill(6)

    info = dict(as_of_date=as_of_date, per_ttm=per_ttm, pbr=pbr,
                gross_margin_pct=gross_margin_pct, asset_turnover=asset_turnover,
                validated=int(validated), financial_period_end=financial_period_end,
                annual_period_end=annual_period_end, source_note=source_note)
    if validated and not fundamental_is_fresh(info):
        raise ValueError("검증된 재무 데이터의 지표/기준일이 유효하지 않습니다.")
    with _connect(db_path) as con:
        con.execute("""
            INSERT INTO value_fundamental_snapshot (
                code, code_name, as_of_date,
                per_ttm, pbr,
                gross_margin_pct, asset_turnover,
                latest_annual_year,
                source_note, updated_at, validated, financial_period_end,
                annual_period_end, validation_note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                code_name = excluded.code_name,
                as_of_date = excluded.as_of_date,
                per_ttm = excluded.per_ttm,
                pbr = excluded.pbr,
                gross_margin_pct = excluded.gross_margin_pct,
                asset_turnover = excluded.asset_turnover,
                latest_annual_year = excluded.latest_annual_year,
                source_note = excluded.source_note,
                updated_at = excluded.updated_at,
                validated = excluded.validated,
                financial_period_end = excluded.financial_period_end,
                annual_period_end = excluded.annual_period_end,
                validation_note = excluded.validation_note
        """, (
            code,
            str(code_name or code),
            str(as_of_date),
            per_ttm,
            pbr,
            gross_margin_pct,
            asset_turnover,
            latest_annual_year,
            source_note,
            now_text, int(validated), financial_period_end,
            annual_period_end, validation_note,
        ))
        con.execute("""INSERT INTO value_collection_status VALUES (?,?,?)
            ON CONFLICT(code) DO UPDATE SET checked_at=excluded.checked_at, reason=excluded.reason""",
            (code, now_text, "OK" if validated else "UNVERIFIED_IMPORT"))


def load_value_candidates(
    per_min: float = 0.0,
    per_max: float = 15.0,
    pbr_min: float = 0.0,
    pbr_max: float = 1.0,
    gross_margin_min: float = 30.0,
    gross_margin_max: float = 95.0,
    asset_turnover_min: float = 1.0,
    asset_turnover_max: float = 10.0,
    db_path: Path = DB_PATH,
) -> Dict[str, dict]:
    """
    사용자 조건을 모두 만족하는 저평가 우량주 후보.

    PER/PBR의 0은 데이터 없음/적자 표시일 수 있어
    0 자체는 통과시키지 않는다.
    """
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        rows = con.execute("""
            SELECT *
            FROM value_fundamental_snapshot
            WHERE
                validated = 1
                AND per_ttm > ?
                AND per_ttm <= ?
                AND pbr > ?
                AND pbr <= ?
                AND gross_margin_pct >= ?
                AND gross_margin_pct <= ?
                AND asset_turnover >= ?
                AND asset_turnover <= ?
            ORDER BY
                per_ttm ASC,
                pbr ASC,
                gross_margin_pct DESC,
                asset_turnover DESC,
                code ASC
        """, (
            per_min,
            per_max,
            pbr_min,
            pbr_max,
            gross_margin_min,
            gross_margin_max,
            asset_turnover_min,
            asset_turnover_max,
        )).fetchall()

    return {
        str(row["code"]).zfill(6): dict(row)
        for row in rows if fundamental_is_fresh(dict(row))
    }


def save_value_position_state(
    code: str,
    strategy_name: str,
    entry_price: float,
    entry_date: Optional[str] = None,
    entry_time: Optional[str] = None,
    db_path: Path = DB_PATH,
):
    init_value_quality_tables(db_path)

    now = datetime.now()
    entry_date = entry_date or now.strftime("%Y%m%d")
    entry_time = entry_time or now.strftime("%H%M%S")
    now_text = now.strftime("%Y%m%d%H%M%S")

    code = str(code).zfill(6)

    with _connect(db_path) as con:
        # 과거 DB에는 추가 컬럼이 있을 수 있으므로
        # 새 코드가 사용하는 컬럼만 지정해서 저장한다.
        con.execute("""
            INSERT INTO value_position_state (
                code, strategy_name,
                entry_date, entry_time, entry_price,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                strategy_name = excluded.strategy_name,
                entry_date = excluded.entry_date,
                entry_time = excluded.entry_time,
                entry_price = excluded.entry_price,
                updated_at = excluded.updated_at
        """, (
            code,
            strategy_name,
            entry_date,
            entry_time,
            float(entry_price or 0),
            now_text,
            now_text,
        ))


def ensure_value_position_state(
    code: str,
    strategy_name: str,
    entry_price: float,
    db_path: Path = DB_PATH,
) -> dict:
    state = get_value_position_state(
        code,
        db_path=db_path,
    )
    if state:
        return state

    save_value_position_state(
        code=code,
        strategy_name=strategy_name,
        entry_price=entry_price,
        db_path=db_path,
    )

    return get_value_position_state(
        code,
        db_path=db_path,
    ) or {}


def get_value_position_state(
    code: str,
    db_path: Path = DB_PATH,
) -> Optional[dict]:
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        row = con.execute("""
            SELECT *
            FROM value_position_state
            WHERE code = ?
        """, (
            str(code).zfill(6),
        )).fetchone()

    return dict(row) if row else None


def delete_value_position_state(
    code: str,
    db_path: Path = DB_PATH,
):
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        con.execute(
            "DELETE FROM value_position_state WHERE code = ?",
            (str(code).zfill(6),),
        )


def is_value_report_sent(
    report_key: str,
    db_path: Path = DB_PATH,
) -> bool:
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        row = con.execute("""
            SELECT 1
            FROM value_sent_report
            WHERE report_key = ?
        """, (
            str(report_key),
        )).fetchone()

    return row is not None


def save_sent_value_report(
    report_key: str,
    code: str,
    title: str,
    broker: str,
    report_date: str,
    url: str,
    db_path: Path = DB_PATH,
):
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        con.execute("""
            INSERT OR IGNORE INTO value_sent_report (
                report_key,
                code,
                title,
                broker,
                report_date,
                url,
                sent_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            str(report_key),
            str(code).zfill(6),
            str(title or ""),
            str(broker or ""),
            str(report_date or ""),
            str(url),
            datetime.now().strftime("%Y%m%d%H%M%S"),
        ))


def get_last_sent_value_report_at(
    db_path: Path = DB_PATH,
) -> Optional[str]:
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        row = con.execute("""
            SELECT sent_at
            FROM value_sent_report
            ORDER BY sent_at DESC
            LIMIT 1
        """).fetchone()

    return None if row is None else row["sent_at"]


def get_last_scanned_hankyung_report_id(
    db_path: Path = DB_PATH,
) -> Optional[int]:
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        row = con.execute("""
            SELECT last_report_id
            FROM value_report_scan_state
            WHERE state_key = 'hankyung_consensus'
        """).fetchone()

    if row is None:
        return None

    return int(row["last_report_id"])


def save_last_scanned_hankyung_report_id(
    report_id: int,
    db_path: Path = DB_PATH,
):
    init_value_quality_tables(db_path)

    with _connect(db_path) as con:
        con.execute("""
            INSERT INTO value_report_scan_state (
                state_key,
                last_report_id,
                updated_at
            )
            VALUES ('hankyung_consensus', ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                last_report_id = excluded.last_report_id,
                updated_at = excluded.updated_at
        """, (
            int(report_id),
            datetime.now().strftime("%Y%m%d%H%M%S"),
        ))
