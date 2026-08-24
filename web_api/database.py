from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
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


def get_virtual_positions(strategy_name: str | None = None) -> list[dict[str, Any]]:
    if not table_exists(MONITORING_DB, "virtual_position"):
        return []
    where = ""
    params: list[Any] = []
    if strategy_name:
        where = "WHERE strategy_name = ?"
        params.append(strategy_name)
    with _connect_readonly(MONITORING_DB) as con:
        rows = con.execute(
            f"""
            SELECT strategy_name, code, code_name, entry_price, entry_at,
                   remaining_ratio, current_price, unrealized_return_pct,
                   buy_reason_code, buy_reason, state_json, updated_at
            FROM virtual_position
            {where}
            ORDER BY updated_at DESC, code ASC
            """,
            params,
        ).fetchall()
    result = _rows_to_dicts(rows)
    for item in result:
        item["state"] = _safe_json_loads(item.pop("state_json", None)) or {}
    return result


def get_virtual_trades(
    strategy_name: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not table_exists(MONITORING_DB, "virtual_trade"):
        return []
    where = ""
    params: list[Any] = []
    if strategy_name:
        where = "WHERE strategy_name = ?"
        params.append(strategy_name)
    params.extend([max(1, min(int(limit), 5000)), max(0, int(offset))])
    with _connect_readonly(MONITORING_DB) as con:
        rows = con.execute(
            f"""
            SELECT * FROM virtual_trade
            {where}
            ORDER BY exit_at DESC, trade_id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return _rows_to_dicts(rows)


def get_virtual_event_summary(
    strategy_name: str,
    event_date: str | None = None,
) -> dict[str, int]:
    """가상 체결만 기준으로 하루 BUY/SELL 이벤트 수를 집계한다.

    BUY는 같은 전략/종목/진입시각을 한 번만 센다.
    SELL은 부분청산을 포함한 실제 가상 청산 이벤트(virtual_trade row)를 센다.
    """
    event_date = str(event_date or datetime.now().strftime("%Y%m%d"))

    if not table_exists(MONITORING_DB, "virtual_position"):
        open_buy_count = 0
    else:
        with _connect_readonly(MONITORING_DB) as con:
            row = con.execute(
                """
                SELECT COUNT(*) AS count
                FROM virtual_position
                WHERE strategy_name = ?
                  AND substr(entry_at, 1, 8) = ?
                """,
                (strategy_name, event_date),
            ).fetchone()
        open_buy_count = int(row["count"] or 0)

    closed_buy_keys: set[tuple[str, str]] = set()
    sell_count = 0

    if table_exists(MONITORING_DB, "virtual_trade"):
        with _connect_readonly(MONITORING_DB) as con:
            rows = con.execute(
                """
                SELECT code, entry_at, exit_at
                FROM virtual_trade
                WHERE strategy_name = ?
                  AND (
                      substr(entry_at, 1, 8) = ?
                      OR substr(exit_at, 1, 8) = ?
                  )
                """,
                (strategy_name, event_date, event_date),
            ).fetchall()

        for row in rows:
            if str(row["entry_at"] or "")[:8] == event_date:
                closed_buy_keys.add((str(row["code"]), str(row["entry_at"])))
            if str(row["exit_at"] or "")[:8] == event_date:
                sell_count += 1

    # 당일 진입 후 아직 보유 중인 종목 + 당일 진입 후 일부/전부 청산된 진입을 합친다.
    # 같은 진입이 position과 trade 양쪽에 존재하는 부분청산 케이스 중복을 제거하기 위해
    # open position의 code/entry_at도 key 집합으로 다시 계산한다.
    buy_keys = set(closed_buy_keys)
    if table_exists(MONITORING_DB, "virtual_position"):
        with _connect_readonly(MONITORING_DB) as con:
            rows = con.execute(
                """
                SELECT code, entry_at
                FROM virtual_position
                WHERE strategy_name = ?
                  AND substr(entry_at, 1, 8) = ?
                """,
                (strategy_name, event_date),
            ).fetchall()
        buy_keys.update((str(row["code"]), str(row["entry_at"])) for row in rows)

    return {
        "buy": len(buy_keys),
        "sell": int(sell_count),
        "total": len(buy_keys) + int(sell_count),
    }


def get_virtual_events(
    strategy_name: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """가상 포지션/거래 테이블에서 BUY·SELL 이벤트 타임라인을 구성한다."""
    limit = max(1, min(int(limit), 1000))
    positions = get_virtual_positions(strategy_name)
    trades = get_virtual_trades(strategy_name, limit=5000, offset=0)

    events: list[dict[str, Any]] = []
    buy_events: dict[tuple[str, str], dict[str, Any]] = {}

    for position in positions:
        key = (str(position.get("code")), str(position.get("entry_at")))
        buy_events[key] = {
            "event_id": f"BUY|{strategy_name}|{key[0]}|{key[1]}",
            "strategy_name": strategy_name,
            "event_type": "BUY",
            "code": position.get("code"),
            "code_name": position.get("code_name"),
            "price": float(position.get("entry_price") or 0),
            "reason_code": position.get("buy_reason_code"),
            "reason": position.get("buy_reason"),
            "event_at": position.get("entry_at"),
            "position_status": "OPEN",
            "return_pct": None,
            "ratio": 1.0,
        }

    for trade in trades:
        key = (str(trade.get("code")), str(trade.get("entry_at")))
        buy_events.setdefault(
            key,
            {
                "event_id": f"BUY|{strategy_name}|{key[0]}|{key[1]}",
                "strategy_name": strategy_name,
                "event_type": "BUY",
                "code": trade.get("code"),
                "code_name": trade.get("code_name"),
                "price": float(trade.get("entry_price") or 0),
                "reason_code": trade.get("buy_reason_code"),
                "reason": trade.get("buy_reason"),
                "event_at": trade.get("entry_at"),
                "position_status": "CLOSED_OR_PARTIAL",
                "return_pct": None,
                "ratio": 1.0,
            },
        )

        events.append(
            {
                "event_id": f"SELL|{trade.get('trade_id')}",
                "strategy_name": strategy_name,
                "event_type": "SELL",
                "code": trade.get("code"),
                "code_name": trade.get("code_name"),
                "price": float(trade.get("exit_price") or 0),
                "reason_code": trade.get("sell_reason_code"),
                "reason": trade.get("sell_reason"),
                "event_at": trade.get("exit_at"),
                "position_status": "SELL",
                "return_pct": float(trade.get("return_pct") or 0),
                "ratio": float(trade.get("exit_ratio") or 0),
            }
        )

    events.extend(buy_events.values())
    events.sort(key=lambda item: str(item.get("event_at") or ""), reverse=True)
    return events[:limit]



def get_virtual_daily_performance(
    strategy_name: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """가상 SELL 이벤트를 날짜별로 집계한다.

    daily_return_pct는 해당 날짜에 청산된 SELL leg의 수익률을
    실제 청산비중(exit_ratio)으로 가중평균한 값이다.

    현재 가상매매는 전략별 독립 현금/NAV를 운용하지 않으므로
    이 값은 일간 NAV 수익률이 아니다.
    아직 청산되지 않은 포지션의 미실현손익도 포함하지 않는다.
    """
    empty = {
        "items": [],
        "active_day_count": 0,
        "best_daily_return_pct": 0.0,
        "worst_daily_return_pct": 0.0,
        "average_daily_return_pct": 0.0,
        "metric_type": "REALIZED_EXIT_WEIGHTED_RETURN",
    }

    if not table_exists(MONITORING_DB, "virtual_trade"):
        return empty

    where = ["strategy_name = ?"]
    params: list[Any] = [strategy_name]

    if date_from:
        where.append("substr(exit_at, 1, 8) >= ?")
        params.append(str(date_from))

    if date_to:
        where.append("substr(exit_at, 1, 8) <= ?")
        params.append(str(date_to))

    with _connect_readonly(MONITORING_DB) as con:
        rows = con.execute(
            f"""
            SELECT
                substr(exit_at, 1, 8) AS date,
                COUNT(*) AS sell_event_count,
                COALESCE(SUM(exit_ratio), 0) AS exited_ratio,
                COALESCE(SUM(weighted_return_pct), 0)
                    AS weighted_return_sum
            FROM virtual_trade
            WHERE {' AND '.join(where)}
            GROUP BY substr(exit_at, 1, 8)
            ORDER BY date ASC
            """,
            params,
        ).fetchall()

    items = []

    for row in rows:
        exited_ratio = float(row["exited_ratio"] or 0)
        weighted_sum = float(row["weighted_return_sum"] or 0)

        daily_return_pct = (
            weighted_sum / exited_ratio
            if exited_ratio > 0
            else 0.0
        )

        items.append({
            "date": str(row["date"]),
            "sell_event_count": int(row["sell_event_count"] or 0),
            "exited_ratio": exited_ratio,
            "weighted_return_sum": weighted_sum,
            "daily_return_pct": daily_return_pct,
        })

    daily_returns = [
        float(item["daily_return_pct"])
        for item in items
    ]

    return {
        "items": items,
        "active_day_count": len(items),
        "best_daily_return_pct": (
            max(daily_returns) if daily_returns else 0.0
        ),
        "worst_daily_return_pct": (
            min(daily_returns) if daily_returns else 0.0
        ),
        "average_daily_return_pct": (
            sum(daily_returns) / len(daily_returns)
            if daily_returns
            else 0.0
        ),
        "metric_type": "REALIZED_EXIT_WEIGHTED_RETURN",
    }


def get_virtual_strategy_index_history(
    strategy_name: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    empty = {
        "items": [],
        "latest": None,
        "best_daily_return_pct": 0.0,
        "worst_daily_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "metric_type": "EQUAL_NOTIONAL_MARK_TO_MARKET_INDEX",
    }

    if not table_exists(MONITORING_DB, "virtual_strategy_daily_snapshot"):
        return empty

    where = ["strategy_name = ?"]
    params: list[Any] = [strategy_name]

    if date_from:
        where.append("snapshot_date >= ?")
        params.append(str(date_from))
    if date_to:
        where.append("snapshot_date <= ?")
        params.append(str(date_to))

    with _connect_readonly(MONITORING_DB) as con:
        rows = con.execute(
            f"""
            SELECT *
            FROM virtual_strategy_daily_snapshot
            WHERE {' AND '.join(where)}
            ORDER BY snapshot_date ASC
            """,
            params,
        ).fetchall()

    items = _rows_to_dicts(rows)
    if not items:
        return empty

    daily_returns = [float(item.get("daily_return_pct") or 0) for item in items]
    drawdowns = [float(item.get("drawdown_pct") or 0) for item in items]

    return {
        "items": items,
        "latest": items[-1],
        "best_daily_return_pct": max(daily_returns),
        "worst_daily_return_pct": min(daily_returns),
        "max_drawdown_pct": min(drawdowns),
        "metric_type": "EQUAL_NOTIONAL_MARK_TO_MARKET_INDEX",
    }


def get_virtual_performance(strategy_name: str) -> dict[str, Any]:
    """가상 BUY→완전청산 단위의 전략 성과를 반환한다.

    PullbackTrend의 30% 부분매도처럼 한 포지션이 여러 SELL leg로 나뉘는 경우,
    동일 code + entry_at을 하나의 거래로 묶고 청산비중 합계가 100%인 경우에만
    완료 거래/승률/평균수익률 계산에 포함한다.
    """
    positions = get_virtual_positions(strategy_name)

    empty = {
        "open_position_count": len(positions),
        "completed_trade_count": 0,
        "winning_trade_count": 0,
        "win_rate_pct": 0.0,
        "average_return_pct": 0.0,
        "best_return_pct": 0.0,
        "worst_return_pct": 0.0,
        "sell_event_count": 0,
        "realized_weighted_return_pct": 0.0,
        # 구버전 프론트 호환 필드
        "closed_leg_count": 0,
        "winning_leg_count": 0,
        "weighted_realized_return_pct": 0.0,
    }

    if not table_exists(MONITORING_DB, "virtual_trade"):
        return empty

    with _connect_readonly(MONITORING_DB) as con:
        leg_row = con.execute(
            """
            SELECT
                COUNT(*) AS sell_event_count,
                COALESCE(SUM(weighted_return_pct), 0) AS realized_weighted_return_pct
            FROM virtual_trade
            WHERE strategy_name = ?
            """,
            (strategy_name,),
        ).fetchone()

        completed_rows = con.execute(
            """
            WITH round_trips AS (
                SELECT
                    code,
                    entry_at,
                    SUM(exit_ratio) AS exited_ratio,
                    SUM(weighted_return_pct) AS trade_return_pct
                FROM virtual_trade
                WHERE strategy_name = ?
                GROUP BY code, entry_at
            )
            SELECT trade_return_pct
            FROM round_trips
            WHERE exited_ratio >= 0.999999
            """,
            (strategy_name,),
        ).fetchall()

    completed_returns = [float(row["trade_return_pct"] or 0) for row in completed_rows]
    completed_count = len(completed_returns)
    wins = sum(1 for value in completed_returns if value > 0)
    sell_event_count = int(leg_row["sell_event_count"] or 0)
    realized_weighted = float(leg_row["realized_weighted_return_pct"] or 0)

    return {
        "open_position_count": len(positions),
        "completed_trade_count": completed_count,
        "winning_trade_count": wins,
        "win_rate_pct": wins / completed_count * 100.0 if completed_count else 0.0,
        "average_return_pct": (
            sum(completed_returns) / completed_count if completed_count else 0.0
        ),
        "best_return_pct": max(completed_returns) if completed_returns else 0.0,
        "worst_return_pct": min(completed_returns) if completed_returns else 0.0,
        "sell_event_count": sell_event_count,
        "realized_weighted_return_pct": realized_weighted,
        # 구버전 프론트 호환 필드
        "closed_leg_count": sell_event_count,
        "winning_leg_count": wins,
        "weighted_realized_return_pct": realized_weighted,
    }
