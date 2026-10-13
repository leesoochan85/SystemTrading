"""Cash-backed momentum account queries, including archived-profile exclusion."""
from __future__ import annotations

from typing import Any
from .query_context import rows_to_dicts as _rows_to_dicts, safe_json_loads as _safe_json_loads
from .query_context import ReadOnlyDatabase


def get_paper_account(context: ReadOnlyDatabase, candidate_limit=100, trade_limit=100):
    """Read-only momentum account view; archived profiles are excluded. Does not create a paper DB."""
    from util.paper_trading import MOMENTUM
    empty = {"ready": False, "accounts": [], "watchlist": [], "stats": {}, "settings": None,
             "positions": [], "trades": [], "equity_history": [], "as_of": None,
             "metric_type": "CASH_BACKED_PAPER_NAV"}
    if not context.table_exists("paper_account"):
        return empty
    with context.connect() as con:
        con.execute("BEGIN")
        accounts = _rows_to_dicts(con.execute("SELECT * FROM paper_account WHERE strategy_name=?", (MOMENTUM,)))
        positions = _rows_to_dicts(con.execute("SELECT * FROM paper_position WHERE strategy_name=? ORDER BY code", (MOMENTUM,)))
        equity = _rows_to_dicts(con.execute("SELECT * FROM paper_equity WHERE strategy_name=? ORDER BY snapshot_date", (MOMENTUM,)))
        status = con.execute("SELECT * FROM paper_run_status ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        watchlist = []
        if status:
            watchlist = _rows_to_dicts(con.execute("""SELECT * FROM paper_watchlist WHERE snapshot_date=?
                ORDER BY eligible DESC,momentum_rank,code LIMIT ?""", (status["snapshot_date"], candidate_limit)))
        trades = _rows_to_dicts(con.execute("SELECT * FROM paper_trade WHERE strategy_name=? ORDER BY trade_id DESC LIMIT ?", (MOMENTUM, trade_limit)))
        completed = _rows_to_dicts(con.execute("""
            SELECT strategy_name,position_id,
                   SUM(CASE WHEN event_type='BUY' THEN quantity ELSE -quantity END) AS remaining,
                   SUM(CASE WHEN event_type='SELL' THEN net_pnl ELSE 0 END) AS pnl,
                   SUM(CASE WHEN event_type='BUY' THEN price*quantity+fee ELSE 0 END) AS invested
            FROM paper_trade WHERE strategy_name=?
            GROUP BY strategy_name,position_id HAVING remaining=0 AND invested>0
        """, (MOMENTUM,)))
    for position in positions:
        position["state"] = _safe_json_loads(position.pop("state_json"))
        position["remaining_ratio"] = position["quantity"] / position["initial_quantity"]
    for candidate in watchlist:
        candidate["metric"] = _safe_json_loads(candidate.pop("metric_json"))
    for trade in trades:
        trade["condition_data"] = _safe_json_loads(trade.pop("condition_json"))
    for account in accounts:
        name = account["strategy_name"]
        config = _safe_json_loads(account.pop("config_json"))
        owned = [p for p in positions if p["strategy_name"] == name]
        nav = account["cash"] + sum(p["quantity"] * p["current_price"] for p in owned)
        history = [e for e in equity if e["strategy_name"] == name]
        done = [t for t in completed if t["strategy_name"] == name]
        returns = [t["pnl"] / t["invested"] * 100 for t in done]
        profit = sum(t["pnl"] for t in done if t["pnl"] > 0)
        loss = -sum(t["pnl"] for t in done if t["pnl"] < 0)
        account.update(equity=nav, cumulative_return_pct=(nav / account["initial_cash"] - 1) * 100,
                       max_drawdown_pct=min([e["drawdown_pct"] for e in history] + [account.get("max_drawdown_pct", 0), 0]),
                       open_position_count=len(owned), costs=config["account"],
                       performance={"open_position_count": len(owned), "completed_trade_count": len(done),
                           "winning_trade_count": sum(r > 0 for r in returns),
                           "win_rate_pct": sum(r > 0 for r in returns) / len(done) * 100 if done else 0,
                           "average_return_pct": sum(returns) / len(done) if done else 0,
                           "best_return_pct": max(returns) if done else 0,
                           "worst_return_pct": min(returns) if done else 0,
                           "profit_factor": profit / loss if loss else None})
    return {**empty, "ready": bool(accounts), "accounts": accounts, "positions": [p for p in positions if p["strategy_name"] == MOMENTUM],
            "trades": trades, "watchlist": watchlist, "equity_history": equity,
            "stats": _safe_json_loads(status["stats_json"]) if status else {},
            "settings": _safe_json_loads(status["settings_json"]) if status else None,
            "watchlist_date": status["snapshot_date"] if status else None,
            "as_of": max((a["updated_at"] for a in accounts), default=None)}
