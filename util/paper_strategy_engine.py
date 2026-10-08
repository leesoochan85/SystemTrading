"""A/B/C paper comparison on one tick stream and identical account constraints."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import os
import time

from util.breakout_atr import new_atr_state_from_value
from util.paper_breakout_baseline import PaperBreakoutBaseline
from util.momentum_breakout import momentum_exit
from util.paper_trading import PaperBroker, BASELINE, FILTERED, MOMENTUM
from util.paper_tick_recorder import PaperTickRecorder
from util.time_helper import check_adjacent_transaction_closed_for_buying, check_transaction_open, get_market_open_time


class PaperComparisonEngine:
    def __init__(self, strategies, db_path=None, record_ticks=True, enable_baseline=True):
        self.momentum = next((s for s in strategies if s.strategy_name == MOMENTUM), None)
        self.baseline = next((s for s in strategies if isinstance(s, PaperBreakoutBaseline)), None)
        if self.baseline is None and self.momentum and enable_baseline:
            self.baseline = PaperBreakoutBaseline(market_db_path=self.momentum.market_history_db_path, clock=self.momentum.clock)
        self.baseline_retry_after = 0.0
        self.broker = None
        self.last_snapshot_at = 0.0
        self.dirty = False
        self.last_tick_times = {}
        self.tick_date = None
        self.metrics_retry_after = 0.0
        self.tick_recorder = None
        if self.momentum:
            path = db_path or os.getenv("SYSTEMTRADING_PAPER_DB") or self.momentum.market_history_db_path.parent / "monitoring.db"
            self.broker = PaperBroker(path, self.momentum.config, self.momentum.clock())
            self.momentum.watchlist_writer = self.broker.save_watchlist
            if record_ticks and os.getenv("SYSTEMTRADING_PAPER_TICK_CSV"):
                try:
                    self.tick_recorder = PaperTickRecorder(os.environ["SYSTEMTRADING_PAPER_TICK_CSV"])
                except (OSError, ValueError) as exc:
                    print(f"[PaperComparison] 틱 기록 시작 실패: {exc}")

    def prepare_baseline(self, force=False):
        if not self.baseline:
            return
        if not force and time.monotonic() < self.baseline_retry_after:
            return
        try:
            if not self.baseline.universe:
                self.baseline.check_and_get_universe()
            if force:
                self.baseline.check_and_get_price_data()
            else:
                self.baseline._ensure_daily_metrics()
            self.baseline.is_init_success = True
            self.baseline_retry_after = 0.0
        except Exception as exc:
            self.baseline.is_init_success = False
            self.baseline_retry_after = time.monotonic() + 60
            print(f"[PaperComparison] A/B 기준값 준비 실패 (C 손절 관리 유지): {exc}")

    def get_candidate_codes(self, force=False):
        self.prepare_baseline(force=force)
        return self.baseline.get_realtime_candidate_codes() if self.baseline and self.baseline.is_init_success else []

    def _record_tick(self, code, tick, now):
        if not self.tick_recorder:
            return
        if not (self.momentum.metrics.get(code, {}).get("eligible") or (MOMENTUM, code) in self.broker.positions):
            return
        try:
            self.tick_recorder.record(code, tick, now, self.momentum.opening.registered_at)
        except (OSError, ValueError) as exc:
            print(f"[PaperComparison] 틱 기록 중단: {exc}")
            try:
                self.tick_recorder.close()
            except OSError:
                pass
            self.tick_recorder = None

    def get_open_codes(self):
        return {code for _, code in self.broker.positions} if self.broker else set()

    def _reject_tick(self, code, now, reason):
        self.momentum.reject_opening_tick(code, now, reason)
        self.dirty = True

    def on_realtime_tick(self, code, tick):
        if not self.broker:
            return
        now = self.momentum.clock()
        if not check_transaction_open(now):
            return
        if time.monotonic() >= self.metrics_retry_after:
            try:
                self.momentum.ensure_daily_metrics(now)
                self.metrics_retry_after = 0.0
            except Exception as exc:
                # Fixed stops remain active even if today's candidate/MA data is missing.
                self.momentum.metrics = {}
                self.metrics_retry_after = time.monotonic() + 60
                print(f"[PaperComparison] 후보 갱신 실패: {exc}")
        self.prepare_baseline()
        self._record_tick(code, tick, now)
        day = now.strftime("%Y%m%d")
        if self.tick_date != day:
            self.tick_date, self.last_tick_times = day, {}
        raw_time = str(tick.get("체결시간") or "").strip()
        try:
            if len(raw_time) != 6 or not raw_time.isdigit():
                self._reject_tick(code, now, "MISSING_TRADE_TIME")
                return
            tick_at = datetime.strptime(day + raw_time, "%Y%m%d%H%M%S")
        except ValueError:
            self._reject_tick(code, now, "INVALID_TRADE_TIME")
            return
        if tick_at < datetime.combine(now.date(), get_market_open_time(now)) or tick_at > now + timedelta(seconds=5) or now - tick_at > timedelta(seconds=30):
            self._reject_tick(code, now, "STALE_OR_OUTSIDE_SESSION")
            return
        if code in self.last_tick_times and tick_at < self.last_tick_times[code]:
            self._reject_tick(code, now, "OUT_OF_ORDER_TICK")
            return
        self.last_tick_times[code] = tick_at
        try:
            price = float(tick.get("현재가") or 0)
        except (TypeError, ValueError):
            self._reject_tick(code, now, "INVALID_TICK_PRICE")
            return
        if not math.isfinite(price) or price <= 0:
            self._reject_tick(code, now, "INVALID_TICK_PRICE")
            return
        self.broker.mark(code, price, now)
        self.dirty = True
        for profile in (BASELINE, FILTERED, MOMENTUM):
            position = self.broker.positions.get((profile, code))
            try:
                if position:
                    self._sell(profile, position, price, now)
                else:
                    self._buy(profile, code, tick, price, now)
            except Exception as exc:
                print(f"[PaperComparison] {profile} {code}: {exc}")

    def _buy(self, profile, code, tick, price, now):
        if check_adjacent_transaction_closed_for_buying(now):
            return
        if profile == MOMENTUM:
            if not self.momentum.is_init_success:
                return
            signal = self.momentum.get_buy_signal(code, tick, now)
            if not signal:
                return
            state = {"initial_stop": signal["stop_price"], "stop_price": signal["stop_price"], "partial_done": False}
            metric = signal["condition_data"]
            position, reason = self.broker.buy(profile, code, self.momentum.universe.get(code, {}).get("code_name", code),
                price, signal["stop_price"], state, signal["reason_code"], metric, now,
                max_stop_distance=metric["atr14"] * self.momentum.rules["stop_atr_multiple"])
            self.momentum.runtime[code]["reason"] = reason or "PAPER_ENTRY_FILLED"
            return
        if not self.baseline or not self.baseline.is_init_success or code not in self.baseline.universe:
            return
        if profile == FILTERED and not self.momentum.metrics.get(code, {}).get("eligible"):
            return
        signal = self.baseline._get_buy_signal(code, tick=tick)
        if not signal:
            return
        metric = signal["condition_data"]
        fill = self.broker.buy_price(price)
        state = new_atr_state_from_value(fill, now.strftime("%Y%m%d%H%M%S"), metric["atr14"], metric["atr_date"], self.baseline.INITIAL_ATR_MULTIPLIER)
        self.broker.buy(profile, code, self.baseline.universe[code]["code_name"], price, state["initial_stop"],
                        state, signal["reason_code"], metric, now)

    def _sell(self, profile, position, price, now):
        if profile == MOMENTUM:
            # Do not require a successful daily DB read to honor a stored stop.
            context = {}
            if price > position["state"]["stop_price"]:
                context = self.momentum.exit_context(position, now)
            signal = momentum_exit(position, price, context, self.momentum.rules, self.broker.costs)
            if signal:
                self.broker.sell(position, price, signal["quantity"], signal["reason_code"], now, signal.get("partial", False))
            return
        if not self.baseline:
            return
        signal, state = self.baseline.evaluate_sell_signal(position["code"], price, position["entry_price"], position["entry_at"], position["state"], now=now)
        self.broker.update_state(position, state)
        if signal:
            self.broker.sell(position, price, position["quantity"], signal["reason_code"], now)

    def flush_snapshot_if_due(self, force=False):
        if self.tick_recorder:
            try:
                self.tick_recorder.flush()
            except OSError as exc:
                print(f"[PaperComparison] 틱 파일 저장 실패: {exc}")
                try:
                    self.tick_recorder.close()
                except OSError:
                    pass
                self.tick_recorder = None
        if not self.broker or not self.dirty:
            return 0
        stamp = time.monotonic()
        if not force and stamp - self.last_snapshot_at < 5.0:
            return 0
        result = self.broker.snapshot(self.momentum.clock(), self.momentum.runtime)
        self.last_snapshot_at, self.dirty = stamp, False
        return result
