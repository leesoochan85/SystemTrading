"""Legacy virtual positions, execution events and performance queries."""
from __future__ import annotations

from typing import Any
from .query_context import rows_to_dicts as _rows_to_dicts, safe_json_loads as _safe_json_loads
from .query_context import VirtualQueryContext
from .paper_queries import get_paper_account


def get_virtual_positions(context: VirtualQueryContext, strategy_name: str | None = None) -> list[dict[str, Any]]:
    if not context.monitoring.table_exists("virtual_position"):
        return []
    where = ""
    params: list[Any] = []
    if strategy_name:
        where = "WHERE strategy_name = ?"
        params.append(strategy_name)
    with context.monitoring.connect() as con:
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
    context: VirtualQueryContext,
    strategy_name: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not context.monitoring.table_exists("virtual_trade"):
        return []
    where = ""
    params: list[Any] = []
    if strategy_name:
        where = "WHERE strategy_name = ?"
        params.append(strategy_name)
    params.extend([max(1, min(int(limit), 5000)), max(0, int(offset))])
    with context.monitoring.connect() as con:
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
    context: VirtualQueryContext,
    strategy_name: str,
    event_date: str | None = None,
) -> dict[str, int]:
    """가상 체결만 기준으로 하루 BUY/SELL 이벤트 수를 집계한다.

    BUY는 같은 전략/종목/진입시각을 한 번만 센다.
    SELL은 부분청산을 포함한 실제 가상 청산 이벤트(virtual_trade row)를 센다.
    """
    event_date = str(event_date or context.clock().strftime("%Y%m%d"))

    from util.paper_trading import MOMENTUM
    if strategy_name == MOMENTUM:
        if not context.paper.table_exists("paper_trade"):
            return {"buy": 0, "sell": 0, "total": 0}
        with context.paper.connect() as con:
            counts = dict(con.execute("""SELECT event_type,COUNT(*) FROM paper_trade
                WHERE strategy_name=? AND substr(event_at,1,8)=? GROUP BY event_type""", (strategy_name, event_date)))
        return {"buy": counts.get("BUY", 0), "sell": counts.get("SELL", 0), "total": sum(counts.values())}

    if not context.monitoring.table_exists("virtual_position"):
        open_buy_count = 0
    else:
        with context.monitoring.connect() as con:
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

    if context.monitoring.table_exists("virtual_trade"):
        with context.monitoring.connect() as con:
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
    if context.monitoring.table_exists("virtual_position"):
        with context.monitoring.connect() as con:
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
    context: VirtualQueryContext,
    strategy_name: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """가상 포지션/거래 테이블에서 BUY·SELL 이벤트 타임라인을 구성한다."""
    limit = max(1, min(int(limit), 1000))
    positions = get_virtual_positions(context, strategy_name)
    trades = get_virtual_trades(context, strategy_name, limit=5000, offset=0)

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
    context: VirtualQueryContext,
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

    if not context.monitoring.table_exists("virtual_trade"):
        return empty

    where = ["strategy_name = ?"]
    params: list[Any] = [strategy_name]

    if date_from:
        where.append("substr(exit_at, 1, 8) >= ?")
        params.append(str(date_from))

    if date_to:
        where.append("substr(exit_at, 1, 8) <= ?")
        params.append(str(date_to))

    with context.monitoring.connect() as con:
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
    context: VirtualQueryContext,
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

    if not context.monitoring.table_exists("virtual_strategy_daily_snapshot"):
        return empty

    where = ["strategy_name = ?"]
    params: list[Any] = [strategy_name]

    if date_from:
        where.append("snapshot_date >= ?")
        params.append(str(date_from))
    if date_to:
        where.append("snapshot_date <= ?")
        params.append(str(date_to))

    with context.monitoring.connect() as con:
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



def get_virtual_performance(context: VirtualQueryContext, strategy_name: str) -> dict[str, Any]:
    """가상 BUY→완전청산 단위의 전략 성과를 반환한다.

    PullbackTrend의 30% 부분매도처럼 한 포지션이 여러 SELL leg로 나뉘는 경우,
    동일 code + entry_at을 하나의 거래로 묶고 청산비중 합계가 100%인 경우에만
    완료 거래/승률/평균수익률 계산에 포함한다.
    """
    if strategy_name == "MomentumContractionStrategy":
        review = get_paper_account(context.paper, candidate_limit=1, trade_limit=1)
        return next((p["performance"] for p in review["accounts"] if p["strategy_name"] == strategy_name), {})
    positions = get_virtual_positions(context, strategy_name)

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

    if not context.monitoring.table_exists("virtual_trade"):
        return empty

    with context.monitoring.connect() as con:
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
