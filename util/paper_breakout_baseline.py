"""A/B comparison rules only: no broker orders, listeners or Qt dependency."""
from datetime import datetime, timedelta
import math
import time
from util.market_history import get_db_path as get_market_history_db_path, load_company_stock_master, load_breakout_metrics, load_completed_close_sum
from util.breakout_atr import ATR_PERIOD as DEFAULT_ATR_PERIOD, INITIAL_ATR_MULTIPLIER as DEFAULT_INITIAL_ATR_MULTIPLIER, TRAILING_ATR_MULTIPLIER as DEFAULT_TRAILING_ATR_MULTIPLIER, load_atr_metrics, load_atr_history, new_atr_state, new_atr_state_from_value, advance_atr_state, atr_exit_signal, ma_profit_exit_signal
from util.strategy_settings import load_ma_settings_for_date, acknowledge_ma_settings
from util.momentum_breakout import previous_market_date
from util.time_helper import is_market_business_day

class PaperBreakoutBaseline:
    strategy_name = "HighBreakoutBaselinePaper"
    paper_only = True
    BREAKOUT_WINDOW = 180
    VOLUME_AVG_WINDOW = 20
    MIN_AVG_TRADING_VALUE = 2_000_000_000
    ATR_PERIOD = DEFAULT_ATR_PERIOD
    INITIAL_ATR_MULTIPLIER = DEFAULT_INITIAL_ATR_MULTIPLIER
    TRAILING_ATR_MULTIPLIER = DEFAULT_TRAILING_ATR_MULTIPLIER

    def __init__(self, kiwoom=None, auto_init=False, market_db_path=None, clock=datetime.now):
        self.kiwoom = kiwoom
        self.clock = clock
        self.market_history_db_path = market_db_path or get_market_history_db_path()
        self.universe = {}
        self.breakout_metrics = {}
        self.is_init_success = False
        self.metrics_date = None
        self.atr_history_date = None
        self.atr_history_cache = {}
        self.last_signal_log_at = {}
        self.ma_settings_date = None
        self.active_ma_settings = None
        self.ma_close_sum_cache = {}
        self.ma_settings_db_path = self.market_history_db_path.parent / "strategy_settings.db"
        if auto_init:
            self.check_and_get_universe()
            self.check_and_get_price_data()
            self.is_init_success = True

    def check_and_get_universe(self, shared_universe_df=None):
        master = load_company_stock_master(db_path=self.market_history_db_path, read_only=True)
        if master.empty:
            raise RuntimeError("A/B 기업주식 마스터 없음: bootstrap_market_history.py 실행 필요")
        self.universe = {str(row.code).zfill(6): {"code_name": row.code_name, "market": row.market} for row in master.itertuples(index=False)}

    def get_realtime_candidate_codes(self):
        return sorted(self.breakout_metrics)

    def check_and_get_price_data(self, shared_price_map=None):
        """신고가 계산에 필요한 요약 지표만 메모리에 올린다."""
        codes = list(self.universe.keys())

        now = self.clock()
        # 장 종료 후 Manager가 저장한 당일 완결봉은 다음 장 준비에 사용한다.
        cutoff = now + timedelta(days=1) if now.hour * 60 + now.minute > 930 else now
        before_date = cutoff.strftime("%Y%m%d")
        metrics = load_breakout_metrics(
            codes,
            breakout_window=self.BREAKOUT_WINDOW,
            volume_window=self.VOLUME_AVG_WINDOW,
            ma_window=20,
            db_path=self.market_history_db_path,
            before_date=before_date,
        )
        atr_metrics = load_atr_metrics(
            codes, self.market_history_db_path, before_date, self.ATR_PERIOD
        )
        for code, metric in metrics.items():
            atr = atr_metrics.get(code)
            if atr and atr["atr_date"] == metric["history_last_date"]:
                metric.update(atr)

        self.breakout_metrics = metrics
        self.metrics_date = before_date
        self.atr_history_cache.clear()
        missing = [
            code
            for code in codes
            if code not in metrics
        ]

        print(
            "[HighBreakout] 신고가 기준값 준비: "
            f"{len(metrics)}/{len(codes)}종목 / "
            f"이력부족 {len(missing)}종목"
        )

        if not metrics:
            raise RuntimeError(
                "신고가 계산 가능한 종목이 없습니다. "
                f"최소 {self.BREAKOUT_WINDOW}거래일을 구축하세요."
            )
        self._ensure_ma_settings(now)


    def _metric(self, code):
        return self.breakout_metrics.get(
            str(code)
            .strip()
            .upper()
            .zfill(6)
        )


    def _ensure_daily_metrics(self):
        if self.metrics_date != self.clock().strftime("%Y%m%d"):
            self.check_and_get_price_data()


    def _atr_history(self, code, entry_at):
        today = self.clock().strftime("%Y%m%d")
        if self.atr_history_date != today:
            self.atr_history_cache.clear()
            self.atr_history_date = today
        key = (code, str(entry_at)[:8])
        if key not in self.atr_history_cache:
            self.atr_history_cache[key] = load_atr_history(
                code, self.market_history_db_path, today, self.ATR_PERIOD,
                from_date=str(entry_at)[:8],
            )
        return self.atr_history_cache[key]


    def evaluate_atr_exit(self, code, current_price, entry_price, entry_at, state=None):
        """A/B 가상 비교: 완결 일봉으로 청산선을 갱신하고 현재가와 비교한다."""
        try:
            bars = self._atr_history(code, entry_at)
        except Exception:
            if not state:
                raise
            # 일봉 DB가 일시적으로 읽히지 않아도 저장된 청산선은 해제하지 않는다.
            bars = []
            last = self.last_signal_log_at.get(("atr_history", code), 0)
            if time.time() - last >= 60:
                print(f"[HighBreakout ATR] {code}: 일봉 조회 실패, 저장된 청산선 유지")
                self.last_signal_log_at[("atr_history", code)] = time.time()
        if not state:
            try:
                state = new_atr_state(
                    entry_price, entry_at, bars, self.INITIAL_ATR_MULTIPLIER
                )
            except ValueError:
                if not bars:
                    raise
                latest = bars[-1]
                state = new_atr_state_from_value(
                    entry_price,
                    entry_at,
                    latest["atr"],
                    latest["date"],
                    self.INITIAL_ATR_MULTIPLIER,
                )
                print(
                    f"[HighBreakout ATR] {code}: 진입 전 이력 부족 - "
                    "최신 완결 ATR로 기존 보유 상태 이관"
                )
        if (state["entry_price"] == entry_price
                and (not bars or state["last_bar_date"] >= bars[-1]["date"])):
            return atr_exit_signal(current_price, state), state
        updated = advance_atr_state(
            state, entry_price, bars, self.INITIAL_ATR_MULTIPLIER,
            self.TRAILING_ATR_MULTIPLIER,
        )
        return atr_exit_signal(current_price, updated), updated


    def _ensure_ma_settings(self, now=None):
        now = now or self.clock()
        today = now.strftime("%Y%m%d")
        if self.ma_settings_date == today:
            return
        if not is_market_business_day(now):
            return
        settings = load_ma_settings_for_date(today, db_path=self.ma_settings_db_path)
        acknowledge_ma_settings(settings, today, db_path=self.ma_settings_db_path, now=now)
        self.active_ma_settings = settings
        self.ma_settings_date = today
        self.ma_close_sum_cache.clear()
        print(f"[HighBreakout] MA 청산 설정 적용: v{settings['revision']} / MA{settings['ma_period']} × {settings['exit_ratio']:.3f} / {today}")


    def evaluate_sell_signal(self, code, current_price, entry_price, entry_at, state=None, now=None):
        """A/B cash-backed paper accounts only; ATR rules precede MA exit."""
        now = now or self.clock()
        signal, updated = self.evaluate_atr_exit(code, current_price, entry_price, entry_at, state)
        if signal:
            return signal, updated
        try:
            self._ensure_ma_settings(now)
            settings = self.active_ma_settings
            if settings is None:
                return None, updated
            today = now.strftime("%Y%m%d")
            if not (151900 <= int(now.strftime("%H%M%S")) < 152000):
                return None, updated
            key = (code, today, settings["ma_period"])
            if key not in self.ma_close_sum_cache:
                self.ma_close_sum_cache[key] = load_completed_close_sum(
                    code, today, settings["ma_period"] - 1, db_path=self.market_history_db_path)
            summary = self.ma_close_sum_cache[key]
            previous_sum = summary["close_sum"] if summary and summary["latest_date"] == previous_market_date(today) else None
            return ma_profit_exit_signal(current_price, entry_price, updated, previous_sum,
                                         settings["ma_period"], settings["exit_ratio"], now)
        except Exception as exc:
            # A settings/MA database failure must never disable an existing ATR stop.
            key = ("ma_settings", code)
            if time.time() - self.last_signal_log_at.get(key, 0) >= 60:
                print(f"[HighBreakout] MA 설정 적용 지연: {exc}")
                self.last_signal_log_at[key] = time.time()
            return None, updated


    def _get_buy_signal(self, code, tick=None):
        """순수 전략 매수조건만 판정한다. 주문 가능 여부는 여기서 판단하지 않는다."""
        self._ensure_daily_metrics()
        metric = self._metric(code)
        if not metric:
            return None

        rt = tick or {}
        current_price = int(rt.get("현재가", 0) or 0)
        volume = int(rt.get("누적거래량", 0) or 0)
        breakout_price = float(metric.get("breakout_price", 0) or 0)
        prev_high = float(metric.get("prev_high", 0) or 0)
        atr = float(metric.get("atr14", 0) or 0)
        volume_ma20 = float(metric.get("volume_ma20", 0) or 0)
        trading_value_ma20 = float(metric.get("trading_value_ma20", 0) or 0)

        if (
            current_price <= 0
            or breakout_price <= 0
            or volume_ma20 < 0
            or trading_value_ma20 <= 0
            or prev_high <= 0
            or not math.isfinite(atr)
            or atr <= 0
            or current_price - self.INITIAL_ATR_MULTIPLIER * atr <= 0
        ):
            return None

        if not (
            current_price >= breakout_price
            and current_price > prev_high
            and volume >= volume_ma20
            and trading_value_ma20 >= self.MIN_AVG_TRADING_VALUE
        ):
            return None

        return {
            "reason_code": "BREAKOUT_ENTRY",
            "signal_reason": (
                f"직전 {self.BREAKOUT_WINDOW}거래일 최고가 돌파 + 전일 고가 초과 + "
                f"누적거래량 20일 평균 이상 + 20일 평균거래대금 20억원 이상"
            ),
            "current_price": current_price,
            "condition_data": {
                "current_price": current_price,
                "breakout_price": breakout_price,
                "prev_high": prev_high,
                "high_distance_pct": (current_price / breakout_price - 1) * 100,
                "atr14": atr,
                "atr_date": metric.get("atr_date"),
                "volume": volume,
                "volume_ma20": volume_ma20,
                "trading_value_ma20": trading_value_ma20,
                "min_avg_trading_value": self.MIN_AVG_TRADING_VALUE,
            },
        }
