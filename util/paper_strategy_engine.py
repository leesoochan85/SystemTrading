"""Single momentum paper account on the shared tick stream."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import os
import time

from util.momentum_breakout import momentum_exit
from util.paper_trading import PaperBroker, MOMENTUM
from util.paper_tick_recorder import PaperTickRecorder
from util.time_helper import check_adjacent_transaction_closed_for_buying, check_transaction_open, get_market_open_time


class MomentumPaperEngine:
    def __init__(self, strategies, db_path=None, record_ticks=True):
        self.momentum = next((s for s in strategies if s.strategy_name == MOMENTUM), None)
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
                    print(f"[MomentumPaper] 틱 기록 시작 실패: {exc}")

    def get_candidate_codes(self, force=False):
        if not self.momentum or not self.momentum.is_init_success:
            return []
        return self.momentum.get_realtime_candidate_codes()

    def _record_tick(self, code, tick, now):
        if not self.tick_recorder:
            return
        if not (self.momentum.metrics.get(code, {}).get("eligible") or (MOMENTUM, code) in self.broker.positions):
            return
        try:
            self.tick_recorder.record(code, tick, now, self.momentum.opening.registered_at)
        except (OSError, ValueError) as exc:
            print(f"[MomentumPaper] 틱 기록 중단: {exc}")
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
                print(f"[MomentumPaper] 후보 갱신 실패: {exc}")
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
        position = self.broker.positions.get((MOMENTUM, code))
        try:
            if position:
                self._sell(position, price, now)
            else:
                self._buy(code, tick, price, now)
        except Exception as exc:
            print(f"[MomentumPaper] {code}: {exc}")

    def _buy(self, code, tick, price, now):
        if check_adjacent_transaction_closed_for_buying(now):
            return
        if not self.momentum.is_init_success:
            return
        signal = self.momentum.get_buy_signal(code, tick, now)
        if not signal:
            return
        state = {"initial_stop": signal["stop_price"], "stop_price": signal["stop_price"], "partial_done": False}
        metric = signal["condition_data"]
        position, reason = self.broker.buy(MOMENTUM, code, self.momentum.universe.get(code, {}).get("code_name", code),
            price, signal["stop_price"], state, signal["reason_code"], metric, now,
            max_stop_distance=metric["atr14"] * self.momentum.rules["stop_atr_multiple"])
        self.momentum.runtime[code]["reason"] = reason or "PAPER_ENTRY_FILLED"
        return

    def _sell(self, position, price, now):
        # Do not require a successful daily DB read to honor a stored stop.
        context = {}
        if price > position["state"]["stop_price"]:
            context = self.momentum.exit_context(position, now)
        signal = momentum_exit(position, price, context, self.momentum.rules, self.broker.costs)
        if signal:
            self.broker.sell(position, price, signal["quantity"], signal["reason_code"], now, signal.get("partial", False))
        return

    def flush_snapshot_if_due(self, force=False):
        if self.tick_recorder:
            try:
                self.tick_recorder.flush()
            except OSError as exc:
                print(f"[MomentumPaper] 틱 파일 저장 실패: {exc}")
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
