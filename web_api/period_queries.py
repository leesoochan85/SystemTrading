"""Read isolated period portfolios without creating or migrating any DB."""
import sqlite3

from util.momentum_periods import PERIODS, period_db_path
from .paper_queries import get_paper_account
from .query_context import ReadOnlyDatabase, DatabaseNotReadyError


def get_momentum_periods(context, candidate_limit=100, trade_limit=100):
    profiles = []
    for period in PERIODS:
        try:
            view = get_paper_account(ReadOnlyDatabase(period_db_path(context.path, period["id"]),
                                                     context.busy_timeout_ms), candidate_limit, trade_limit)
            error = None
        except (sqlite3.Error, DatabaseNotReadyError) as exc:
            view, error = {"ready": False}, str(exc)
        profiles.append({**period, "account": view, "error": error})
    ready = all(p["account"]["ready"] for p in profiles)
    same_start = same_costs = same_rules = same_reference = False
    if ready:
        views = [p["account"] for p in profiles]
        accounts = [v["accounts"][0] for v in views]
        same_start = len({a["created_at"] for a in accounts}) == 1
        same_costs = all(a["costs"] == accounts[0]["costs"] for a in accounts)
        rules = [{k: value for k, value in (v.get("settings") or {}).get("strategy", {}).items()
                  if k != "momentum_mode"} for v in views]
        same_rules = bool(rules[0]) and all(r == rules[0] for r in rules)
        refs = [v["stats"].get("reference_date") for v in views]
        same_reference = bool(refs[0]) and len(set(refs)) == 1
    return {"ready": ready, "profiles": profiles,
            "comparison": {"same_start_date": same_start, "same_account_rules": same_costs,
                           "same_strategy_rules": same_rules, "same_reference_date": same_reference},
            "metric_type": "CASH_BACKED_PAPER_NAV",
            "formula": "(latest_completed_close / close_N_sessions_ago - 1) * 100",
            "universe_policy": "COMMON_VALID_127_PLUS_BARS"}
