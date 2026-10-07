"""PDF/video breakout rules. Pure calculations; no Qt, orders, or look-ahead.

ADR is the author's 20-session H/L formula. Pattern thresholds are configurable
approximations; they are not fixed rules stated in the source material.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path
import json
import math
import sqlite3

from util.breakout_atr import iter_atr_bars, load_atr_metrics
from util.time_helper import get_market_open_time, is_market_business_day


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "momentum_contraction.json"


def load_config(path=DEFAULT_CONFIG_PATH):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    rules, account = config["strategy"], config["account"]
    if rules["momentum_mode"] not in ("video_low126", "pdf_returns"):
        raise ValueError("momentum_mode must be video_low126 or pdf_returns")
    for name in ("consolidation_days", "prior_move_window", "recent_range_days",
                 "opening_range_minutes", "partial_after_sessions", "trailing_ma_period"):
        value = rules[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not 2 * rules["recent_range_days"] < rules["consolidation_days"] <= 40:
        raise ValueError("base must contain two low blocks and a preceding TR block (max 40)")
    if not 1 <= rules["top_percent"] <= 2:
        raise ValueError("top_percent must be between 1 and 2")
    if not 0 < rules["max_tr_ratio"] <= 1 or not 0 < rules["partial_ratio"] <= 0.5:
        raise ValueError("invalid contraction or partial ratio")
    if not 2 <= rules["trailing_ma_period"] <= 60 or rules["opening_range_minutes"] > 60:
        raise ValueError("invalid MA or opening range period")
    for name, value in account.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid account setting: {name}")
    if account["initial_cash"] <= 0 or not 0 < account["risk_ratio"] <= 0.01:
        raise ValueError("invalid initial cash or risk ratio")
    if not 0 < account["max_position_ratio"] <= 1 or not isinstance(account["max_positions"], int) or account["max_positions"] < 1:
        raise ValueError("invalid position limits")
    if account["buy_fee_rate"] >= 1 or account["sell_fee_rate"] + account["sell_tax_rate"] >= 1 or account["slippage_bps"] >= 10000:
        raise ValueError("invalid execution costs")
    for name in ("min_adr_pct", "min_avg_trading_value", "min_prior_move_pct", "stop_atr_multiple"):
        value = rules[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid strategy setting: {name}")
    if rules["stop_atr_multiple"] <= 0 or not isinstance(rules["partial_only_profit"], bool):
        raise ValueError("invalid stop or partial profit setting")
    return config


def previous_market_date(before_date):
    day = datetime.strptime(before_date, "%Y%m%d") - timedelta(days=1)
    while not is_market_business_day(day):
        day -= timedelta(days=1)
    return day.strftime("%Y%m%d")


def analyze_bars(rows, rules, atr=None):
    """Ascending complete OHLCV dicts, already cut off before the decision day."""
    base_days = rules["consolidation_days"]
    required = max(127, base_days + rules["prior_move_window"], rules["trailing_ma_period"])
    if len(rows) < required:
        return None
    try:
        dates = [row["date"] for row in rows]
        if any(not isinstance(day, str) or len(day) != 8 or not day.isdigit() for day in dates):
            return None
        for day in dates:
            datetime.strptime(day, "%Y%m%d")
        if any(a >= b for a, b in zip(dates, dates[1:])):
            return None
        rows = [{"date": row["date"], **{k: float(row[k]) for k in
            ("open", "high", "low", "close", "volume")}} for row in rows]
        for row in rows:
            if any(not math.isfinite(row[k]) or row[k] <= 0 for k in ("open", "high", "low", "close")):
                return None
            if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
                return None
            if not math.isfinite(row["volume"]) or row["volume"] < 0:
                return None
    except (KeyError, TypeError, ValueError):
        return None
    close = float(rows[-1]["close"])
    recent20 = rows[-20:]
    adr = 100.0 * (sum(r["high"] / r["low"] for r in recent20) / 20 - 1)
    base = rows[-base_days:]
    before_base = rows[-base_days - rules["prior_move_window"]:-base_days]
    prior_move = (before_base[-1]["close"] / min(r["close"] for r in before_base) - 1) * 100
    true_ranges = []
    for previous, row in zip(rows[:-1], rows[1:]):
        true_ranges.append(max(row["high"] - row["low"], abs(row["high"] - previous["close"]), abs(row["low"] - previous["close"])))
    n = rules["recent_range_days"]
    base_tr = true_ranges[-base_days:]
    recent_tr = sum(base_tr[-n:]) / n
    prior_tr = sum(base_tr[:-n]) / (base_days - n)
    ratio = recent_tr / prior_tr if prior_tr > 0 else None
    low_recent = min(r["low"] for r in base[-n:])
    low_previous = min(r["low"] for r in base[-2*n:-n])
    if atr is None:
        bars = list(iter_atr_bars((r["date"], r["high"], r["low"], r["close"]) for r in rows))
        atr = bars[-1]["atr"] if bars else 0.0
    if not math.isfinite(atr) or atr <= 0:
        return None
    return {
        "history_last_date": dates[-1], "prev_close": close,
        "low126_score": close / min(r["low"] for r in rows[-126:]),
        "return21_pct": (close / rows[-22]["close"] - 1) * 100,
        "return63_pct": (close / rows[-64]["close"] - 1) * 100,
        "return126_pct": (close / rows[-127]["close"] - 1) * 100,
        "adr20_pct": adr, "atr14": float(atr), "atr_date": dates[-1],
        "trading_value_ma20": sum(r["close"] * r["volume"] for r in recent20) / 20,
        "prior_move_pct": prior_move, "tr_ratio": ratio,
        "low_recent": low_recent, "low_previous": low_previous,
        "base_high": max(r["high"] for r in base),
        "base_start_date": base[0]["date"], "base_days": base_days,
    }


def rank_and_filter(metrics, rules):
    """Rank the same eligible, fresh universe before applying ADR/pattern filters."""
    ranked = {code: dict(metric) for code, metric in metrics.items()}
    cutoff = max(1, math.ceil(len(ranked) * rules["top_percent"] / 100))
    fields = ("low126_score", "return21_pct", "return63_pct", "return126_pct")
    for field in fields:
        for index, code in enumerate(sorted(ranked, key=lambda c: (-ranked[c][field], c)), 1):
            ranked[code][field + "_rank"] = index
    counts = {"valid_history": len(ranked), "top_ranked": 0, "adr": 0,
              "liquid": 0, "prior_move": 0, "contraction": 0, "qualified": 0}
    for metric in ranked.values():
        rank = metric["low126_score_rank"] if rules["momentum_mode"] == "video_low126" else min(metric[f + "_rank"] for f in fields[1:])
        metric["momentum_rank"] = rank
        metric["rank_cutoff"] = cutoff
        checks = (
            (rank <= cutoff, "top_ranked", "MOMENTUM_RANK"),
            (metric["adr20_pct"] > rules["min_adr_pct"], "adr", "ADR_TOO_LOW"),
            (metric["trading_value_ma20"] >= rules["min_avg_trading_value"], "liquid", "ILLIQUID"),
            (metric["prior_move_pct"] >= rules["min_prior_move_pct"], "prior_move", "PRIOR_MOVE_TOO_SMALL"),
            (metric["tr_ratio"] is not None and metric["tr_ratio"] <= rules["max_tr_ratio"], "contraction", "RANGE_NOT_TIGHT"),
            (metric["low_recent"] >= metric["low_previous"], "qualified", "LOW_NOT_RISING"),
        )
        metric.update(eligible=True, rejection_reason=None)
        for passed, stage, reason in checks:
            if not passed:
                metric.update(eligible=False, rejection_reason=reason)
                break
            counts[stage] += 1
    return ranked, counts


def load_momentum_metrics(codes, db_path, before_date, rules):
    selected = {str(code).zfill(6) for code in codes}
    reference = previous_market_date(before_date)
    atr_metrics = load_atr_metrics(selected, db_path, before_date)
    window = max(127, rules["consolidation_days"] + rules["prior_move_window"])
    result = {}
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as con:
        cursor = con.execute("""
            WITH ranked AS (
                SELECT code,date,open,high,low,close,volume,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                FROM daily_price WHERE date < ?
            )
            SELECT code,date,open,high,low,close,volume FROM ranked
            WHERE rn <= ? ORDER BY code,date
        """, (before_date, window))
        for code, group in groupby(cursor, key=lambda r: r[0]):
            if code not in selected:
                continue
            rows = [dict(zip(("date", "open", "high", "low", "close", "volume"), r[1:])) for r in group]
            if not rows or rows[-1]["date"] != reference:
                continue
            atr = atr_metrics.get(code, {})
            if atr.get("atr_date") != reference:
                continue
            metric = analyze_bars(rows, rules, atr["atr14"])
            if metric:
                result[code] = metric
    ranked, counts = rank_and_filter(result, rules)
    return ranked, {**counts, "universe": len(selected), "excluded_history": len(selected) - len(result), "reference_date": reference}


class OpeningRange:
    """Observed first N minutes, with a conservative volume continuity guard.

    FID13 deltas must equal abs(FID15), including the first post-range tick.
    This is an experimental feed-integrity policy, not an exchange guarantee.
    An unexplained starting balance (e.g. premarket volume) also blocks entry;
    missing trades are never reconstructed from the day's later high.
    """
    def __init__(self, minutes=5):
        self.minutes = minutes
        self.registered_at = None
        self.day = None
        self.ranges = {}

    def registered(self, now):
        self.registered_at = now
        self.day, self.ranges = now.strftime("%Y%m%d"), {}

    def _state(self, code, now):
        day = now.strftime("%Y%m%d")
        if self.day != day:
            self.day, self.ranges = day, {}
        return self.ranges.setdefault(code, {"high": 0.0, "last_time": None,
            "volume": None, "last_price": None, "frozen": False, "error": None})

    def invalidate(self, code, now, reason):
        state = self._state(code, now)
        opening = datetime.combine(now.date(), get_market_open_time(now))
        if now >= opening and not state["frozen"]:
            state["error"] = state["error"] or reason
        return state["error"] or reason

    def observe(self, code, tick, now):
        day = now.strftime("%Y%m%d")
        state = self._state(code, now)
        if state["error"]:
            return None, state["error"]
        raw_time = str(tick.get("체결시간") or "").strip()
        if len(raw_time) != 6 or not raw_time.isdigit():
            return None, self.invalidate(code, now, "MISSING_TRADE_TIME")
        try:
            tick_at = datetime.strptime(day + raw_time, "%Y%m%d%H%M%S")
        except ValueError:
            return None, self.invalidate(code, now, "INVALID_TRADE_TIME")
        opening = datetime.combine(now.date(), get_market_open_time(now))
        end = opening + timedelta(minutes=self.minutes)
        if tick_at < opening or tick_at > now + timedelta(seconds=5) or now - tick_at > timedelta(seconds=30):
            return None, self.invalidate(code, now, "STALE_OR_OUTSIDE_SESSION")
        if not self.registered_at or self.registered_at > opening:
            return None, "OPENING_RANGE_MISSING"
        if state["last_time"] and tick_at < state["last_time"]:
            return None, self.invalidate(code, now, "OUT_OF_ORDER_TICK")
        try:
            price = float(tick["현재가"])
            if not math.isfinite(price) or price <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            return None, self.invalidate(code, now, "INVALID_TICK_PRICE")
        if state["frozen"]:
            state["last_time"] = tick_at
            return state["high"], None
        try:
            cumulative = int(str(tick["누적거래량"]))
            quantity = abs(int(str(tick["거래량"])))
            if cumulative <= 0 or quantity <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            return None, self.invalidate(code, now, "OPENING_VOLUME_MISSING")
        if state["volume"] is None:
            if tick_at >= end:
                return None, self.invalidate(code, now, "OPENING_RANGE_MISSING")
            if cumulative != quantity:
                return None, self.invalidate(code, now, "OPENING_START_UNVERIFIED")
        else:
            delta = cumulative - state["volume"]
            if delta == 0 and tick_at == state["last_time"] and price == state["last_price"]:
                return None, "DUPLICATE_TICK"
            if delta != quantity:
                return None, self.invalidate(code, now, "OPENING_VOLUME_GAP")
        state["last_time"] = tick_at
        state["volume"], state["last_price"] = cumulative, price
        if tick_at < end:
            state["high"] = max(state["high"], price)
            return None, "WAIT_OPENING_RANGE"
        if state["high"] <= 0:
            return None, "OPENING_RANGE_MISSING"
        state["frozen"] = True
        return state["high"], None


def momentum_exit(position, current_price, context, rules, costs):
    """State changes for a partial exit are applied only by the paper fill transaction."""
    state = position["state"]
    if current_price <= state["stop_price"]:
        return {"reason_code": "DAY_LOW_STOP", "quantity": position["quantity"]}
    age = context.get("elapsed_sessions", 0)
    entry = position["entry_price"]
    modeled_sell = max(1, math.floor(current_price * (1 - costs["slippage_bps"] / 10000)))
    net_per_share = modeled_sell * (1 - costs["sell_fee_rate"] - costs["sell_tax_rate"]) - entry * (1 + costs["buy_fee_rate"])
    if not state.get("partial_done") and age >= rules["partial_after_sessions"]:
        if not rules["partial_only_profit"] or net_per_share > 0:
            qty = max(1, int(position["initial_quantity"] * rules["partial_ratio"]))
            if qty < position["quantity"]:
                return {"reason_code": "TIME_PARTIAL_EXIT", "quantity": qty, "partial": True}
    trailing = state.get("partial_done") or (position["initial_quantity"] == 1 and age >= rules["partial_after_sessions"])
    if trailing and context.get("ma_date", "") >= position["entry_at"][:8] and context.get("close", 0) < context.get("ma", 0):
        return {"reason_code": "MA_CLOSE_EXIT", "quantity": position["quantity"]}
    return None
