"""Three isolated paper portfolios on one completed-history universe/tick stream."""
from copy import deepcopy

from strategy.MomentumContractionStrategy import MomentumContractionStrategy
from util.momentum_breakout import rank_and_filter
from util.momentum_periods import PERIODS, period_db_path
from util.paper_strategy_engine import MomentumPaperEngine


class PeriodMomentumStrategy(MomentumContractionStrategy):
    """Reuse the baseline's full metrics, including stocks it rejected. No broker orders."""
    def __init__(self, source, period):
        super().__init__(None, market_db_path=source.market_history_db_path, clock=source.clock)
        self.source, self.period = source, period
        self.config = deepcopy(source.config)
        self.config["strategy"]["momentum_mode"] = period["mode"]
        self.rules = self.config["strategy"]
        self.source_version = None

    def refresh_from_source(self):
        if not self.source.is_init_success or not self.source.metrics_date:
            self.is_init_success = False
            return
        version = (self.source.metrics_date, id(self.source.metrics))
        if version == self.source_version:
            return
        metrics, counts = rank_and_filter(self.source.metrics, self.rules)
        stats = {**self.source.stats, **counts, "period": self.period["id"],
                 "return_sessions": self.period["sessions"]}
        # Save before publishing; a failed DB write must not enable unsaved candidates.
        self.watchlist_writer(self.source.metrics_date, metrics, self.source.universe, stats, self.clock())
        self.universe, self.metrics, self.stats = self.source.universe, metrics, stats
        self.metrics_date = self.source.metrics_date
        self.runtime, self.exit_cache, self.exit_cache_date = {}, {}, None
        self.source_version, self.is_init_success = version, True
        print(f"[MomentumPeriod:{self.period['id']}] {self.period['sessions']}거래일 수익률 / "
              f"유효 {counts['valid_history']} → 상위 {counts['top_ranked']} → ADR {counts['adr']} "
              f"→ 거래대금 {counts['liquid']} → 선행 상승 {counts['prior_move']} "
              f"→ 수축 {counts['contraction']} → 최종 {counts['qualified']}")

    def ensure_daily_metrics(self, now):
        # The baseline is refreshed first, once. Never issue another history/universe load.
        if self.source.metrics_date != now.strftime("%Y%m%d"):
            raise RuntimeError("공통 일봉 갱신 미완료")
        self.refresh_from_source()


class ParallelMomentumPaperEngine:
    """Retain the existing account and fan out ticks to three new paper accounts."""
    def __init__(self, strategies, db_path=None, record_ticks=True):
        self.baseline = MomentumPaperEngine(strategies, db_path, record_ticks)
        self.period_engines = {}
        if self.baseline.momentum:
            started_at = self.baseline.momentum.clock()
            for period in PERIODS:
                strategy = PeriodMomentumStrategy(self.baseline.momentum, period)
                # Record the union once, below; no threefold CSV writes.
                engine = MomentumPaperEngine([strategy], period_db_path(self.baseline.broker.db_path, period["id"]),
                                             False, account_started_at=started_at)
                self.period_engines[period["id"]] = engine
            self.baseline.extra_tick_filter = lambda code: any(
                e.momentum.metrics.get(code, {}).get("eligible") or code in e.get_open_codes()
                for e in self.period_engines.values())
            print("[MomentumPeriod] 1·3·6개월 독립 가상계좌 동시 운용 / 증권사 주문 없음")

    def _sync(self):
        for name, engine in self.period_engines.items():
            try:
                engine.momentum.refresh_from_source()
            except Exception as exc:
                engine.momentum.is_init_success = False
                engine.momentum.metrics = {}
                print(f"[MomentumPeriod:{name}] 후보 준비 실패: {exc}")

    def get_candidate_codes(self):
        self._sync()
        codes = set(self.baseline.get_candidate_codes())
        for engine in self.period_engines.values():
            codes.update(engine.get_candidate_codes())
        return sorted(codes)

    def get_open_codes(self):
        codes = set(self.baseline.get_open_codes())
        for engine in self.period_engines.values():
            codes.update(engine.get_open_codes())
        return codes

    def on_realtime_registered(self):
        self._sync()
        for engine in self.period_engines.values():
            engine.momentum.on_realtime_registered()

    def on_realtime_tick(self, code, tick):
        # Source first, so a daily rollover shares one completed-history load.
        for name, engine in [("baseline", self.baseline), *self.period_engines.items()]:
            try:
                engine.on_realtime_tick(code, tick)
            except Exception as exc:
                print(f"[MomentumPeriod:{name}] 틱 처리 실패: {exc}")

    def flush_snapshot_if_due(self, force=False):
        self._sync()
        saved = 0
        for name, engine in [("baseline", self.baseline), *self.period_engines.items()]:
            try:
                saved += engine.flush_snapshot_if_due(force)
            except Exception as exc:
                print(f"[MomentumPeriod:{name}] 상태 저장 실패: {exc}")
        return saved
