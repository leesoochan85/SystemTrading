from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(
    os.getenv(
        "SYSTEMTRADING_ROOT",
        str(Path(__file__).resolve().parents[1]),
    )
).resolve()

MONITORING_DB = PROJECT_ROOT / "monitoring.db"
POSITION_DB = PROJECT_ROOT / "strategy_position.db"

SQLITE_BUSY_TIMEOUT_MS = 5_000


class DatabaseNotReadyError(RuntimeError):
    pass


@contextmanager
def _connect_readonly(path: Path):
    """기존 자동매매 DB를 쓰지 않는 조회 전용 연결."""
    path = Path(path).resolve()

    if not path.exists():
        raise DatabaseNotReadyError(
            f"DB 파일을 찾을 수 없습니다: {path}"
        )

    con = sqlite3.connect(
        str(path),
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    con.row_factory = sqlite3.Row
    con.execute(
        f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}"
    )
    con.execute("PRAGMA query_only = ON")

    try:
        yield con
    finally:
        con.close()


def table_exists(path: Path, table_name: str) -> bool:
    if not Path(path).exists():
        return False

    with _connect_readonly(path) as con:
        row = con.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()

    return row is not None


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _safe_json_loads(value):
    if value in (None, ""):
        return value

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def get_db_status() -> dict[str, Any]:
    return {
        "project_root": str(PROJECT_ROOT),
        "monitoring_db": {
            "path": str(MONITORING_DB),
            "exists": MONITORING_DB.exists(),
        },
        "position_db": {
            "path": str(POSITION_DB),
            "exists": POSITION_DB.exists(),
        },
    }


def get_latest_account_equity() -> dict[str, Any] | None:
    if not table_exists(MONITORING_DB, "daily_equity"):
        return None

    with _connect_readonly(MONITORING_DB) as con:
        row = con.execute(
            """
            SELECT *
            FROM daily_equity
            ORDER BY date DESC, updated_at DESC
            LIMIT 1
            """
        ).fetchone()

    return dict(row) if row else None


def get_strategy_positions(strategy_name: str) -> list[dict[str, Any]]:
    """최신 position_snapshot을 우선 사용한다.

    스냅샷 테이블이 아직 없거나 해당 전략 데이터가 없으면
    기존 strategy_position.db를 fallback으로 사용한다.
    """
    if table_exists(MONITORING_DB, "position_snapshot"):
        with _connect_readonly(MONITORING_DB) as con:
            rows = con.execute(
                """
                SELECT
                    code,
                    code_name,
                    strategy_name,
                    quantity,
                    available_quantity,
                    buy_price,
                    current_price,
                    purchase_amount,
                    evaluation_amount,
                    unrealized_pnl,
                    return_pct,
                    updated_at
                FROM position_snapshot
                WHERE strategy_name = ?
                  AND quantity > 0
                ORDER BY updated_at DESC, code ASC
                """,
                (strategy_name,),
            ).fetchall()

        if rows:
            result = _rows_to_dicts(rows)

            for item in result:
                item["quantity"] = int(
                    item.get("quantity") or 0
                )
                item["available_quantity"] = int(
                    item.get("available_quantity") or 0
                )

                for key in (
                    "buy_price",
                    "current_price",
                    "purchase_amount",
                    "evaluation_amount",
                    "unrealized_pnl",
                    "return_pct",
                ):
                    item[key] = float(
                        item.get(key) or 0
                    )

                item["live_price_available"] = True
                item["source"] = "position_snapshot"

            return result

    # 아직 Kiwoom 프로세스가 position_snapshot을 한 번도 저장하지 않은 경우.
    if not table_exists(POSITION_DB, "position_strategy"):
        return []

    with _connect_readonly(POSITION_DB) as con:
        rows = con.execute(
            """
            SELECT
                code,
                code_name,
                strategy_name,
                quantity,
                buy_price,
                created_at
            FROM position_strategy
            WHERE strategy_name = ?
              AND quantity > 0
            ORDER BY created_at DESC, code ASC
            """,
            (strategy_name,),
        ).fetchall()

    result = _rows_to_dicts(rows)

    for item in result:
        item["quantity"] = int(
            item.get("quantity") or 0
        )
        item["available_quantity"] = None
        item["buy_price"] = float(
            item.get("buy_price") or 0
        )
        item["current_price"] = None
        item["purchase_amount"] = None
        item["evaluation_amount"] = None
        item["unrealized_pnl"] = None
        item["return_pct"] = None
        item["live_price_available"] = False
        item["updated_at"] = item.get("created_at")
        item["source"] = "strategy_position_fallback"

    return result


def get_position_snapshot_status() -> dict[str, Any]:
    if not table_exists(MONITORING_DB, "position_snapshot"):
        return {
            "available": False,
            "holding_count": 0,
            "latest_updated_at": None,
        }

    with _connect_readonly(MONITORING_DB) as con:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS holding_count,
                MAX(updated_at) AS latest_updated_at
            FROM position_snapshot
            WHERE quantity > 0
            """
        ).fetchone()

    return {
        "available": row is not None,
        "holding_count": int(
            row["holding_count"] or 0
        ) if row else 0,
        "latest_updated_at": (
            row["latest_updated_at"]
            if row else None
        ),
    }

def get_all_position_counts() -> dict[str, int]:
    if not table_exists(POSITION_DB, "position_strategy"):
        return {}

    with _connect_readonly(POSITION_DB) as con:
        rows = con.execute(
            """
            SELECT strategy_name, COUNT(*) AS count
            FROM position_strategy
            WHERE quantity > 0
            GROUP BY strategy_name
            """
        ).fetchall()

    return {
        str(row["strategy_name"]): int(row["count"] or 0)
        for row in rows
    }


def get_strategy_trades(
    strategy_name: str,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not table_exists(MONITORING_DB, "real_trades"):
        return []

    with _connect_readonly(MONITORING_DB) as con:
        rows = con.execute(
            """
            SELECT *
            FROM real_trades
            WHERE strategy_name = ?
            ORDER BY sell_date DESC, created_at DESC
            LIMIT ? OFFSET ?
            """,
            (
                strategy_name,
                max(1, min(int(limit), 1000)),
                max(0, int(offset)),
            ),
        ).fetchall()

    return _rows_to_dicts(rows)


def get_strategy_signals(
    strategy_name: str,
    signal_type: str | None = None,
    signal_date: str | None = None,
    order_result: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not table_exists(MONITORING_DB, "strategy_signal_log"):
        return []

    where = ["strategy_name = ?"]
    params: list[Any] = [strategy_name]

    if signal_type:
        where.append("signal_type = ?")
        params.append(str(signal_type).upper())

    if signal_date:
        where.append("signal_date = ?")
        params.append(str(signal_date))

    if order_result:
        where.append("order_result = ?")
        params.append(str(order_result).upper())

    params.extend(
        [
            max(1, min(int(limit), 1000)),
            max(0, int(offset)),
        ]
    )

    sql = f"""
        SELECT *
        FROM strategy_signal_log
        WHERE {' AND '.join(where)}
        ORDER BY last_detected_at DESC
        LIMIT ? OFFSET ?
    """

    with _connect_readonly(MONITORING_DB) as con:
        rows = con.execute(sql, params).fetchall()

    result = _rows_to_dicts(rows)

    for item in result:
        item["condition_data"] = _safe_json_loads(
            item.get("condition_data")
        )
        item["order_attempted"] = bool(
            item.get("order_attempted")
        )

    return result


def get_signal_summary(
    strategy_name: str,
    signal_date: str | None = None,
) -> dict[str, int]:
    if not table_exists(MONITORING_DB, "strategy_signal_log"):
        return {
            "buy": 0,
            "sell": 0,
            "order_sent": 0,
            "blocked": 0,
            "order_failed": 0,
        }

    where = ["strategy_name = ?"]
    params: list[Any] = [strategy_name]

    if signal_date:
        where.append("signal_date = ?")
        params.append(signal_date)

    with _connect_readonly(MONITORING_DB) as con:
        row = con.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN signal_type = 'BUY' THEN 1 ELSE 0 END), 0)
                    AS buy_count,
                COALESCE(SUM(CASE WHEN signal_type = 'SELL' THEN 1 ELSE 0 END), 0)
                    AS sell_count,
                COALESCE(SUM(CASE WHEN order_result = 'ORDER_SENT' THEN 1 ELSE 0 END), 0)
                    AS order_sent_count,
                COALESCE(SUM(CASE WHEN order_result = 'BLOCKED' THEN 1 ELSE 0 END), 0)
                    AS blocked_count,
                COALESCE(SUM(CASE WHEN order_result = 'ORDER_FAILED' THEN 1 ELSE 0 END), 0)
                    AS order_failed_count
            FROM strategy_signal_log
            WHERE {' AND '.join(where)}
            """,
            params,
        ).fetchone()

    return {
        "buy": int(row["buy_count"] or 0),
        "sell": int(row["sell_count"] or 0),
        "order_sent": int(row["order_sent_count"] or 0),
        "blocked": int(row["blocked_count"] or 0),
        "order_failed": int(row["order_failed_count"] or 0),
    }



def get_latest_strategy_equity(
    strategy_name: str,
) -> dict[str, Any] | None:
    if not table_exists(
        MONITORING_DB,
        "strategy_equity_snapshot",
    ):
        return None

    with _connect_readonly(
        MONITORING_DB
    ) as con:
        row = con.execute(
            """
            SELECT *
            FROM strategy_equity_snapshot
            WHERE strategy_name = ?
            ORDER BY snapshot_at DESC
            LIMIT 1
            """,
            (strategy_name,),
        ).fetchone()

    return (
        dict(row)
        if row
        else None
    )


def get_strategy_equity_history(
    strategy_name: str,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    if not table_exists(
        MONITORING_DB,
        "strategy_equity_snapshot",
    ):
        return []

    where = [
        "strategy_name = ?"
    ]
    params: list[Any] = [
        strategy_name
    ]

    if date_from:
        where.append(
            "snapshot_date >= ?"
        )
        params.append(date_from)

    if date_to:
        where.append(
            "snapshot_date <= ?"
        )
        params.append(date_to)

    params.append(
        max(
            1,
            min(
                int(limit),
                10000,
            ),
        )
    )

    with _connect_readonly(
        MONITORING_DB
    ) as con:
        rows = con.execute(
            f"""
            SELECT *
            FROM strategy_equity_snapshot
            WHERE {' AND '.join(where)}
            ORDER BY snapshot_at ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return _rows_to_dicts(rows)

def get_strategy_performance(strategy_name: str) -> dict[str, Any]:
    """실현 거래 기준 집계.

    realized_return_pct는 '매도 완료된 거래들의 매입원금 합계 대비 순실현손익'이다.
    전략 NAV 누적수익률이 아니므로 cumulative_return_pct라는 이름을 쓰지 않는다.
    """
    if not table_exists(MONITORING_DB, "real_trades"):
        return {
            "trade_count": 0,
            "winning_trades": 0,
            "win_rate_pct": 0.0,
            "gross_realized_pnl": 0,
            "net_realized_pnl": 0,
            "invested_amount": 0,
            "realized_return_pct": 0.0,
        }

    with _connect_readonly(MONITORING_DB) as con:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS trade_count,
                COALESCE(SUM(
                    CASE
                        WHEN COALESCE(
                            net_realized_pnl,
                            gross_realized_pnl,
                            realized_pnl,
                            0
                        ) > 0
                        THEN 1 ELSE 0
                    END
                ), 0) AS winning_trades,
                COALESCE(SUM(gross_realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                COALESCE(SUM(
                    CASE
                        WHEN buy_price IS NOT NULL
                        THEN buy_price * quantity
                        ELSE 0
                    END
                ), 0) AS invested_amount
            FROM real_trades
            WHERE strategy_name = ?
            """,
            (strategy_name,),
        ).fetchone()

    trade_count = int(row["trade_count"] or 0)
    winning_trades = int(row["winning_trades"] or 0)
    invested_amount = int(row["invested_amount"] or 0)
    net_realized_pnl = int(row["net_realized_pnl"] or 0)

    return {
        "trade_count": trade_count,
        "winning_trades": winning_trades,
        "win_rate_pct": (
            winning_trades / trade_count * 100.0
            if trade_count
            else 0.0
        ),
        "gross_realized_pnl": int(
            row["gross_realized_pnl"] or 0
        ),
        "net_realized_pnl": net_realized_pnl,
        "invested_amount": invested_amount,
        "realized_return_pct": (
            net_realized_pnl / invested_amount * 100.0
            if invested_amount
            else 0.0
        ),
    }


def get_latest_strategy_daily_summary(
    strategy_name: str,
) -> dict[str, Any] | None:
    if not table_exists(MONITORING_DB, "strategy_daily_summary"):
        return None

    with _connect_readonly(MONITORING_DB) as con:
        row = con.execute(
            """
            SELECT *
            FROM strategy_daily_summary
            WHERE strategy_name = ?
            ORDER BY date DESC, updated_at DESC
            LIMIT 1
            """,
            (strategy_name,),
        ).fetchone()

    return dict(row) if row else None
