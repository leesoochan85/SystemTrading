"""Qullamaggie-inspired breakout, paper-only. No order API is referenced here."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from util.const import get_fid
from util.market_history import get_db_path, load_company_stock_master
from util.momentum_breakout import load_config, load_momentum_metrics, OpeningRange, previous_market_date
from util.time_helper import REGULAR_CLOSE_TIME, check_adjacent_transaction_closed_for_buying, check_transaction_open


def korea_now():
    return datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)


class MomentumContractionStrategy:
    strategy_name = "MomentumContractionStrategy"
    event_driven = True
    paper_only = True

    def __init__(self, kiwoom, config_path=None, market_db_path=None, clock=korea_now):
        self.kiwoom = kiwoom
        self.config = load_config(config_path) if config_path else load_config()
        self.rules = self.config["strategy"]
        self.clock = clock
        self.market_history_db_path = Path(market_db_path or get_db_path())
        self.universe = {}
        self.metrics = {}
        self.stats = {}
        self.metrics_date = None
        self.is_init_success = False
        self.opening = OpeningRange(self.rules["opening_range_minutes"])
        self.watchlist_writer = None
        self.runtime = {}
        self.exit_cache = {}
        self.exit_cache_date = None

    def check_and_get_universe(self, shared_universe_df=None):
        master = load_company_stock_master(db_path=self.market_history_db_path, read_only=True)
        if master.empty:
            raise RuntimeError("기업주식 마스터가 없습니다. bootstrap_market_history.py를 먼저 실행하세요.")
        self.universe = {str(row.code).zfill(6): {"code_name": row.code_name, "market": row.market}
                         for row in master.itertuples(index=False)}

    def check_and_get_price_data(self, shared_price_map=None):
        now = self.clock()
        cutoff = now + timedelta(days=1) if now.time() > REGULAR_CLOSE_TIME else now
        before_date = cutoff.strftime("%Y%m%d")
        metrics, stats = load_momentum_metrics(self.universe, self.market_history_db_path, before_date, self.rules)
        self.metrics, self.stats, self.metrics_date = metrics, stats, before_date
        self.runtime = {}
        self.exit_cache = {}
        self.exit_cache_date = None
        if self.watchlist_writer:
            self.watchlist_writer(before_date, self.metrics, self.universe, self.stats, now)
        print(f"[MomentumContraction] 가상 전용 후보 {stats['qualified']}/{stats['valid_history']} / ADR > {self.rules['min_adr_pct']}% / 기준일 {stats['reference_date']}")

    def ensure_daily_metrics(self, now):
        if self.metrics_date != now.strftime("%Y%m%d"):
            self.check_and_get_price_data()

    def get_realtime_candidate_codes(self):
        return sorted(code for code, metric in self.metrics.items() if metric["eligible"])

    def set_universe_real_time(self, register_market=True, codes_override=None, force_refresh=False):
        # StrategyManager performs shared registration, including A/B candidates.
        # This method also supports standalone paper startup.
        if not register_market:
            return
        codes = sorted(set(codes_override or self.get_realtime_candidate_codes()))
        fids = ";".join(get_fid(name) for name in ("체결시간", "현재가", "시가", "고가", "저가", "누적거래량", "거래량", "(최우선)매수호가"))
        for index in range(0, len(codes), 90):
            result = self.kiwoom.set_real_reg(str(7000 + index // 90), ";".join(codes[index:index+90]), fids, "0")
            if result not in (0, None):
                raise RuntimeError(f"가상 전략 실시간 등록 실패: {result}")

    def on_realtime_registered(self):
        self.opening.registered(self.clock())

    def reject_opening_tick(self, code, now, reason):
        if self.metrics.get(code, {}).get("eligible"):
            reason = self.opening.invalidate(code, now, reason)
            self.runtime[code] = {"reason": reason}

    def get_buy_signal(self, code, tick, now):
        metric = self.metrics.get(code)
        if not metric or not metric["eligible"]:
            return None
        opening_high, reason = self.opening.observe(code, tick, now)
        price = float(tick.get("현재가") or 0)
        low = float(tick.get("저가") or 0)
        runtime = {"price": price, "opening_high": opening_high, "day_low": low, "reason": reason}
        self.runtime[code] = runtime
        if reason:
            return None
        if check_adjacent_transaction_closed_for_buying(now):
            runtime["reason"] = "BUY_CUTOFF"
            return None
        pivot = max(metric["base_high"], opening_high)
        if price <= pivot:
            runtime["reason"] = "WAIT_BREAKOUT"
            return None
        if not 0 < low < price:
            runtime["reason"] = "INVALID_DAY_LOW"
            return None
        if price - low > metric["atr14"] * self.rules["stop_atr_multiple"]:
            runtime["reason"] = "STOP_TOO_WIDE"
            return None
        runtime["reason"] = "ENTRY_SIGNAL"
        return {"reason_code": "CONTRACTION_BREAKOUT", "stop_price": low,
                "condition_data": {**metric, "opening_high": opening_high, "pivot": pivot,
                                   "current_price": price, "initial_day_low": low}}

    def exit_context(self, position, now):
        today = now.strftime("%Y%m%d")
        if self.exit_cache_date != today:
            self.exit_cache, self.exit_cache_date = {}, today
        key = (position["code"], position["entry_at"][:8])
        if key in self.exit_cache:
            return self.exit_cache[key]
        with closing(sqlite3.connect(self.market_history_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as con:
            rows = con.execute("SELECT date,close FROM daily_price WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
                               (position["code"], today, self.rules["trailing_ma_period"])).fetchall()
            sessions = con.execute("SELECT COUNT(DISTINCT date) FROM daily_price WHERE date>=? AND date<?",
                                   (position["entry_at"][:8], today)).fetchone()[0]
        context = {"elapsed_sessions": sessions}
        if len(rows) == self.rules["trailing_ma_period"] and rows[0][0] == previous_market_date(today):
            context.update(ma_date=rows[0][0], close=float(rows[0][1]), ma=sum(float(r[1]) for r in rows) / len(rows))
        self.exit_cache[key] = context
        return context
