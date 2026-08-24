from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONITORING_DB = PROJECT_ROOT / "monitoring.db"
SQLITE_TIMEOUT = 15


def _now() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _connect():
    con = sqlite3.connect(MONITORING_DB, timeout=SQLITE_TIMEOUT)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


def init_virtual_trading_tables():
    with _connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS virtual_position (
                strategy_name TEXT NOT NULL,
                code TEXT NOT NULL,
                code_name TEXT,
                entry_price REAL NOT NULL,
                entry_at TEXT NOT NULL,
                remaining_ratio REAL NOT NULL DEFAULT 1.0,
                current_price REAL NOT NULL DEFAULT 0,
                unrealized_return_pct REAL NOT NULL DEFAULT 0,
                buy_reason_code TEXT,
                buy_reason TEXT,
                state_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (strategy_name, code)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS virtual_trade (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                code TEXT NOT NULL,
                code_name TEXT,
                entry_price REAL NOT NULL,
                entry_at TEXT NOT NULL,
                exit_price REAL NOT NULL,
                exit_at TEXT NOT NULL,
                exit_ratio REAL NOT NULL,
                return_pct REAL NOT NULL,
                weighted_return_pct REAL NOT NULL,
                buy_reason_code TEXT,
                buy_reason TEXT,
                sell_reason_code TEXT,
                sell_reason TEXT,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_virtual_position_strategy
            ON virtual_position(strategy_name)
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_virtual_trade_strategy_exit
            ON virtual_trade(strategy_name, exit_at)
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS virtual_strategy_daily_snapshot (
                snapshot_date TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                open_position_count INTEGER NOT NULL DEFAULT 0,
                deployed_units REAL NOT NULL DEFAULT 0,
                realized_cost_units REAL NOT NULL DEFAULT 0,
                realized_value_units REAL NOT NULL DEFAULT 0,
                open_cost_units REAL NOT NULL DEFAULT 0,
                open_value_units REAL NOT NULL DEFAULT 0,
                total_value_units REAL NOT NULL DEFAULT 0,
                strategy_index REAL NOT NULL DEFAULT 100,
                cumulative_return_pct REAL NOT NULL DEFAULT 0,
                daily_return_pct REAL NOT NULL DEFAULT 0,
                peak_index REAL NOT NULL DEFAULT 100,
                drawdown_pct REAL NOT NULL DEFAULT 0,
                metric_type TEXT NOT NULL DEFAULT 'EQUAL_NOTIONAL_MARK_TO_MARKET_INDEX',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (strategy_name, snapshot_date)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_virtual_strategy_daily_snapshot_date
            ON virtual_strategy_daily_snapshot(strategy_name, snapshot_date)
        """)


def get_virtual_entry_keys_for_date(entry_date: str):
    """특정 날짜에 이미 가상 BUY가 발생한 (strategy_name, code) 집합."""
    init_virtual_trading_tables()

    date_text = str(entry_date or "").strip()
    if len(date_text) != 8 or not date_text.isdigit():
        return set()

    result = set()

    with _connect() as con:
        position_rows = con.execute(
            """
            SELECT DISTINCT strategy_name, code
            FROM virtual_position
            WHERE substr(entry_at, 1, 8) = ?
            """,
            (date_text,),
        ).fetchall()

        trade_rows = con.execute(
            """
            SELECT DISTINCT strategy_name, code
            FROM virtual_trade
            WHERE substr(entry_at, 1, 8) = ?
            """,
            (date_text,),
        ).fetchall()

    for row in list(position_rows) + list(trade_rows):
        result.add((
            str(row["strategy_name"]),
            str(row["code"]).strip().upper().zfill(6),
        ))

    return result


def get_all_virtual_positions() -> list[dict[str, Any]]:
    """현재 열린 가상 포지션 전체를 한 번에 읽는다.

    VirtualStrategyEngine 시작 시 메모리 캐시를 복원할 때 사용한다.
    실시간 틱마다 호출하지 않는다.
    """
    init_virtual_trading_tables()

    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM virtual_position ORDER BY strategy_name, code"
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        try:
            item["state"] = json.loads(item.pop("state_json") or "{}")
        except (TypeError, ValueError):
            item["state"] = {}
        result.append(item)

    return result


def bulk_update_virtual_positions(positions: list[dict[str, Any]]) -> int:
    """메모리에 누적된 최신 가상 포지션 상태를 한 트랜잭션으로 저장한다.

    현재가/미실현수익률/runtime state를 웹 조회용 DB에 반영한다.
    BUY/SELL 같은 이벤트 저장은 이 함수와 무관하게 즉시 처리한다.
    """
    if not positions:
        return 0

    init_virtual_trading_tables()
    now = _now()
    rows = []

    for position in positions:
        entry_price = float(position.get("entry_price") or 0)
        current_price = float(position.get("current_price") or 0)

        if entry_price <= 0 or current_price <= 0:
            continue

        return_pct = (current_price - entry_price) / entry_price * 100.0
        rows.append((
            current_price,
            return_pct,
            json.dumps(position.get("state") or {}, ensure_ascii=False),
            now,
            str(position.get("strategy_name") or ""),
            str(position.get("code") or "").strip().zfill(6),
        ))

    if not rows:
        return 0

    with _connect() as con:
        con.executemany(
            """
            UPDATE virtual_position
            SET current_price = ?,
                unrealized_return_pct = ?,
                state_json = ?,
                updated_at = ?
            WHERE strategy_name = ? AND code = ?
            """,
            rows,
        )

    return len(rows)


def get_virtual_position(strategy_name: str, code: str) -> Optional[dict[str, Any]]:
    init_virtual_trading_tables()
    code = str(code).strip().zfill(6)
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM virtual_position WHERE strategy_name = ? AND code = ?",
            (strategy_name, code),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["state"] = json.loads(result.pop("state_json") or "{}")
    except (TypeError, ValueError):
        result["state"] = {}
    return result


def open_virtual_position(
    strategy_name: str,
    code: str,
    code_name: str,
    entry_price: float,
    buy_reason_code: str,
    buy_reason: str,
    state: Optional[dict[str, Any]] = None,
) -> bool:
    init_virtual_trading_tables()
    entry_price = float(entry_price or 0)
    if entry_price <= 0:
        return False
    code = str(code).strip().zfill(6)
    now = _now()
    with _connect() as con:
        cursor = con.execute("""
            INSERT OR IGNORE INTO virtual_position (
                strategy_name, code, code_name, entry_price, entry_at,
                remaining_ratio, current_price, unrealized_return_pct,
                buy_reason_code, buy_reason, state_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1.0, ?, 0, ?, ?, ?, ?)
        """, (
            strategy_name, code, code_name or code, entry_price, now,
            entry_price, buy_reason_code, buy_reason,
            json.dumps(state or {}, ensure_ascii=False), now,
        ))
        return cursor.rowcount > 0


def update_virtual_position(
    strategy_name: str,
    code: str,
    current_price: float,
    state: Optional[dict[str, Any]] = None,
):
    position = get_virtual_position(strategy_name, code)
    if not position:
        return False
    current_price = float(current_price or 0)
    if current_price <= 0:
        return False
    return_pct = (current_price - float(position["entry_price"])) / float(position["entry_price"]) * 100.0
    state_json = json.dumps(
        position.get("state", {}) if state is None else state,
        ensure_ascii=False,
    )
    with _connect() as con:
        con.execute("""
            UPDATE virtual_position
            SET current_price = ?, unrealized_return_pct = ?, state_json = ?, updated_at = ?
            WHERE strategy_name = ? AND code = ?
        """, (current_price, return_pct, state_json, _now(), strategy_name, str(code).zfill(6)))
    return True


def close_virtual_position(
    strategy_name: str,
    code: str,
    exit_price: float,
    sell_reason_code: str,
    sell_reason: str,
    exit_ratio: float = 1.0,
) -> bool:
    position = get_virtual_position(strategy_name, code)
    if not position:
        return False
    exit_price = float(exit_price or 0)
    if exit_price <= 0:
        return False

    remaining = float(position.get("remaining_ratio") or 0)
    ratio = max(0.0, min(float(exit_ratio), remaining))
    if ratio <= 0:
        return False

    entry_price = float(position["entry_price"])
    return_pct = (exit_price - entry_price) / entry_price * 100.0
    weighted_return_pct = return_pct * ratio
    now = _now()

    with _connect() as con:
        con.execute("""
            INSERT INTO virtual_trade (
                strategy_name, code, code_name, entry_price, entry_at,
                exit_price, exit_at, exit_ratio, return_pct, weighted_return_pct,
                buy_reason_code, buy_reason, sell_reason_code, sell_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            strategy_name, position["code"], position.get("code_name"),
            entry_price, position["entry_at"], exit_price, now, ratio,
            return_pct, weighted_return_pct,
            position.get("buy_reason_code"), position.get("buy_reason"),
            sell_reason_code, sell_reason, now,
        ))

        new_remaining = max(0.0, remaining - ratio)
        if new_remaining <= 1e-9:
            con.execute(
                "DELETE FROM virtual_position WHERE strategy_name = ? AND code = ?",
                (strategy_name, str(code).zfill(6)),
            )
        else:
            con.execute("""
                UPDATE virtual_position
                SET remaining_ratio = ?, current_price = ?,
                    unrealized_return_pct = ?, updated_at = ?
                WHERE strategy_name = ? AND code = ?
            """, (
                new_remaining, exit_price, return_pct, now,
                strategy_name, str(code).zfill(6),
            ))
    return True


def save_virtual_strategy_daily_snapshots(
    strategy_names,
    snapshot_date: str | None = None,
) -> list[dict[str, Any]]:
    """가상 BUY를 동일 투자금 1단위로 본 일별 mark-to-market 성과지수."""
    init_virtual_trading_tables()

    date_text = str(snapshot_date) if snapshot_date else datetime.now().strftime("%Y%m%d")
    now_text = _now()
    names = list(dict.fromkeys(
        str(name).strip()
        for name in (strategy_names or [])
        if str(name).strip()
    ))

    if not names:
        return []

    saved = []

    with _connect() as con:
        for strategy_name in names:
            realized = con.execute(
                """
                SELECT
                    COALESCE(SUM(exit_ratio), 0) AS cost_units,
                    COALESCE(SUM(exit_ratio * exit_price / entry_price), 0) AS value_units
                FROM virtual_trade
                WHERE strategy_name = ? AND entry_price > 0
                """,
                (strategy_name,),
            ).fetchone()

            opened = con.execute(
                """
                SELECT
                    COUNT(*) AS open_position_count,
                    COALESCE(SUM(remaining_ratio), 0) AS cost_units,
                    COALESCE(SUM(remaining_ratio * current_price / entry_price), 0) AS value_units
                FROM virtual_position
                WHERE strategy_name = ?
                  AND entry_price > 0
                  AND current_price > 0
                  AND remaining_ratio > 0
                """,
                (strategy_name,),
            ).fetchone()

            realized_cost = float(realized["cost_units"] or 0)
            realized_value = float(realized["value_units"] or 0)
            open_count = int(opened["open_position_count"] or 0)
            open_cost = float(opened["cost_units"] or 0)
            open_value = float(opened["value_units"] or 0)

            deployed_units = realized_cost + open_cost
            total_value_units = realized_value + open_value
            strategy_index = (
                total_value_units / deployed_units * 100.0
                if deployed_units > 0 else 100.0
            )
            cumulative_return_pct = strategy_index - 100.0

            previous = con.execute(
                """
                SELECT strategy_index, peak_index
                FROM virtual_strategy_daily_snapshot
                WHERE strategy_name = ? AND snapshot_date < ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (strategy_name, date_text),
            ).fetchone()

            if previous is None:
                previous_index = 100.0
                previous_peak = 100.0
            else:
                previous_index = float(previous["strategy_index"] or 100.0)
                previous_peak = float(previous["peak_index"] or 100.0)

            daily_return_pct = (
                (strategy_index / previous_index - 1.0) * 100.0
                if previous_index > 0 else 0.0
            )
            peak_index = max(100.0, previous_peak, strategy_index)
            drawdown_pct = (
                (strategy_index / peak_index - 1.0) * 100.0
                if peak_index > 0 else 0.0
            )

            con.execute(
                """
                INSERT INTO virtual_strategy_daily_snapshot (
                    snapshot_date, strategy_name, open_position_count, deployed_units,
                    realized_cost_units, realized_value_units,
                    open_cost_units, open_value_units, total_value_units,
                    strategy_index, cumulative_return_pct, daily_return_pct,
                    peak_index, drawdown_pct, metric_type, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'EQUAL_NOTIONAL_MARK_TO_MARKET_INDEX', ?)
                ON CONFLICT(strategy_name, snapshot_date) DO UPDATE SET
                    open_position_count=excluded.open_position_count,
                    deployed_units=excluded.deployed_units,
                    realized_cost_units=excluded.realized_cost_units,
                    realized_value_units=excluded.realized_value_units,
                    open_cost_units=excluded.open_cost_units,
                    open_value_units=excluded.open_value_units,
                    total_value_units=excluded.total_value_units,
                    strategy_index=excluded.strategy_index,
                    cumulative_return_pct=excluded.cumulative_return_pct,
                    daily_return_pct=excluded.daily_return_pct,
                    peak_index=excluded.peak_index,
                    drawdown_pct=excluded.drawdown_pct,
                    metric_type=excluded.metric_type,
                    updated_at=excluded.updated_at
                """,
                (
                    date_text, strategy_name, open_count, deployed_units,
                    realized_cost, realized_value, open_cost, open_value,
                    total_value_units, strategy_index, cumulative_return_pct,
                    daily_return_pct, peak_index, drawdown_pct, now_text,
                ),
            )

            saved.append({
                "snapshot_date": date_text,
                "strategy_name": strategy_name,
                "open_position_count": open_count,
                "deployed_units": deployed_units,
                "strategy_index": strategy_index,
                "cumulative_return_pct": cumulative_return_pct,
                "daily_return_pct": daily_return_pct,
                "peak_index": peak_index,
                "drawdown_pct": drawdown_pct,
                "metric_type": "EQUAL_NOTIONAL_MARK_TO_MARKET_INDEX",
                "updated_at": now_text,
            })

    return saved
