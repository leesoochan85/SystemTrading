from __future__ import annotations

from datetime import datetime
import time

from util.db_helper import save_strategy_signal
from util.time_helper import check_transaction_open
from util.virtual_trading import (
    bulk_update_virtual_positions,
    close_virtual_position,
    get_all_virtual_positions,
    get_virtual_entry_keys_for_date,
    open_virtual_position,
    update_virtual_position,
)


class VirtualStrategyEngine:
    """실계좌와 독립된 웹 전략 가상체결 엔진.

    구조
    ----
    - BUY/SELL 전략 판정은 Kiwoom 실시간 틱마다 즉시 수행한다.
    - 열린 가상 포지션은 메모리 캐시에 유지한다.
    - 틱마다 SQLite를 읽거나 현재가를 UPDATE하지 않는다.
    - 현재가/미실현수익률/runtime state는 5초마다 한 번에 DB로 저장한다.
    - BUY/SELL/부분청산 같은 중요 이벤트는 즉시 DB에 반영한다.
    """

    SNAPSHOT_INTERVAL_SECONDS = 5.0

    def __init__(self, strategies):
        self.strategies = {
            strategy.strategy_name: strategy
            for strategy in strategies
        }

        # key = (strategy_name, code)
        self.position_cache = {}
        self.dirty_position_keys = set()
        self.last_snapshot_at = 0.0

        self.reentry_guard_date = None
        self.entered_today_keys = set()

        self.reload_positions_from_db()
        self._reload_reentry_guard()

    # ------------------------------------------------------------------
    # 메모리 캐시 / 5초 DB 스냅샷
    # ------------------------------------------------------------------

    @staticmethod
    def _position_key(strategy_name, code):
        return (
            str(strategy_name),
            str(code).strip().upper().zfill(6),
        )

    def reload_positions_from_db(self):
        """프로그램 재시작 시 DB의 열린 가상 포지션을 메모리에 복원한다."""
        self.position_cache = {}

        for position in get_all_virtual_positions():
            key = self._position_key(
                position.get("strategy_name"),
                position.get("code"),
            )
            self.position_cache[key] = position

        self.dirty_position_keys.clear()

        print(
            "[VirtualStrategyEngine] 가상 포지션 캐시 복원: "
            f"{len(self.position_cache)}건"
        )

    def _reload_reentry_guard(self, force=False):
        today = datetime.now().strftime("%Y%m%d")

        if not force and self.reentry_guard_date == today:
            return

        self.reentry_guard_date = today
        self.entered_today_keys = set(
            get_virtual_entry_keys_for_date(today)
        )

        print(
            "[VirtualStrategyEngine] 당일 재진입 차단 복원: "
            f"{today} / {len(self.entered_today_keys)}건"
        )

    def _refresh_reentry_guard_date(self):
        today = datetime.now().strftime("%Y%m%d")
        if self.reentry_guard_date != today:
            self._reload_reentry_guard(force=True)

    def _has_entered_today(self, strategy_name, code):
        self._refresh_reentry_guard_date()
        return self._position_key(strategy_name, code) in self.entered_today_keys

    def _mark_entered_today(self, strategy_name, code):
        self._refresh_reentry_guard_date()
        self.entered_today_keys.add(self._position_key(strategy_name, code))

    def get_open_codes(self):
        """가상 보유 중인 종목을 실시간 등록 대상에 강제 포함하기 위해 반환한다."""
        return sorted({code for _, code in self.position_cache.keys()})

    def _get_cached_position(self, strategy_name, code):
        return self.position_cache.get(
            self._position_key(strategy_name, code)
        )

    def _cache_open_position(
        self,
        strategy_name,
        code,
        code_name,
        entry_price,
        buy_reason_code,
        buy_reason,
        state,
    ):
        now_text = datetime.now().strftime("%Y%m%d%H%M%S")
        key = self._position_key(strategy_name, code)

        self.position_cache[key] = {
            "strategy_name": strategy_name,
            "code": key[1],
            "code_name": code_name or key[1],
            "entry_price": float(entry_price),
            "entry_at": now_text,
            "remaining_ratio": 1.0,
            "current_price": float(entry_price),
            "unrealized_return_pct": 0.0,
            "buy_reason_code": buy_reason_code,
            "buy_reason": buy_reason,
            "state": dict(state or {}),
            "updated_at": now_text,
        }

    def _update_cached_market_state(self, position, current_price):
        entry_price = float(position.get("entry_price") or 0)
        current_price = float(current_price or 0)

        if entry_price <= 0 or current_price <= 0:
            return

        position["current_price"] = current_price
        position["unrealized_return_pct"] = (
            (current_price - entry_price) / entry_price * 100.0
        )
        position["updated_at"] = datetime.now().strftime("%Y%m%d%H%M%S")

        self.dirty_position_keys.add(
            self._position_key(
                position.get("strategy_name"),
                position.get("code"),
            )
        )

    def flush_snapshot_if_due(self, force=False):
        """변경된 가상 포지션을 최대 5초에 한 번 SQLite에 일괄 저장한다."""
        now = time.time()

        if (
            not force
            and now - self.last_snapshot_at < self.SNAPSHOT_INTERVAL_SECONDS
        ):
            return 0

        dirty_positions = [
            self.position_cache[key]
            for key in list(self.dirty_position_keys)
            if key in self.position_cache
        ]

        saved = bulk_update_virtual_positions(dirty_positions)

        self.dirty_position_keys.clear()
        self.last_snapshot_at = now
        return saved

    # ------------------------------------------------------------------
    # 실시간 틱 처리
    # ------------------------------------------------------------------

    def on_realtime_tick(self, raw_code, tick):
        if not check_transaction_open():
            return

        code = str(raw_code).strip().upper().zfill(6)
        current_price = float(tick.get("현재가", 0) or 0)
        if current_price <= 0:
            return

        for strategy in self.strategies.values():
            if not getattr(strategy, "is_init_success", False):
                continue

            position = self._get_cached_position(
                strategy.strategy_name,
                code,
            )

            # 신규 매수 검사는 현재 전략 유니버스 종목만 수행한다.
            # 이미 열린 가상 포지션은 유니버스에서 빠져도 매도 관리를 계속한다.
            if position is None and code not in getattr(strategy, "universe", {}):
                continue

            try:
                self._process_strategy(
                    strategy,
                    code,
                    tick,
                    current_price,
                    position,
                )
            except Exception as exc:
                print(
                    f"[VirtualStrategyEngine] {strategy.strategy_name} "
                    f"{code} 처리 오류: {exc}"
                )

    def _process_strategy(
        self,
        strategy,
        code,
        tick,
        current_price,
        position,
    ):
        if position:
            # SQLite UPDATE가 아니라 메모리만 갱신한다.
            self._update_cached_market_state(position, current_price)
            self._process_sell(
                strategy,
                code,
                tick,
                position,
                current_price,
            )
            return

        if self._has_entered_today(strategy.strategy_name, code):
            return

        self._process_buy(strategy, code, tick, current_price)

    def _save_signal(
        self,
        strategy,
        code,
        signal_type,
        reason_code,
        reason,
        price,
        data,
    ):
        cached = self._get_cached_position(strategy.strategy_name, code)
        code_name = (
            strategy.universe.get(code, {}).get("code_name")
            or ((cached or {}).get("code_name"))
            or code
        )

        return save_strategy_signal(
            strategy_name=strategy.strategy_name,
            code=code,
            code_name=code_name,
            signal_type=signal_type,
            reason_code=reason_code,
            signal_reason=reason,
            current_price=price,
            condition_data=data,
        )

    # ------------------------------------------------------------------
    # BUY
    # ------------------------------------------------------------------

    def _process_buy(self, strategy, code, tick, current_price):
        name = strategy.strategy_name
        signal = None
        state = {}

        if name == "HighBreakoutStrategy":
            signal = strategy._get_buy_signal(code, tick=tick)

        elif name == "PullbackTrendStrategy":
            metric = strategy._metric(code)
            mas = strategy._dynamic_mas(code, current_price)
            if not metric or mas is None:
                return

            ma5, ma20, ma60 = mas
            if ma20 <= 0:
                return

            distance = current_price / ma20 * 100.0
            if not (
                ma5 > ma20 > ma60
                and strategy.DISTANCE_MIN_PCT
                <= distance
                <= strategy.DISTANCE_MAX_PCT
                and current_price >= ma20
            ):
                return

            signal = {
                "reason_code": "PULLBACK_ENTRY",
                "signal_reason": (
                    "MA5 > MA20 > MA60 정배열 + 20일선 눌림목 조건 충족"
                ),
                "current_price": current_price,
                "condition_data": {
                    "current_price": current_price,
                    "ma5": ma5,
                    "ma20": ma20,
                    "ma60": ma60,
                    "distance_pct": distance,
                },
            }
            state = {
                "previous_high": float(
                    metric.get("previous_high", 0) or 0
                ),
                "high_touch_date": None,
                "high_touch_volume": 0,
                "partial_exit_requested": False,
            }

        elif name == "ValueQualityStrategy":
            if not strategy._is_qualified_candidate(code):
                return

            info = strategy.universe.get(code, {})
            signal = {
                "reason_code": "VALUE_QUALITY_ENTRY",
                "signal_reason": (
                    "PER·PBR·매출총이익률·총자산회전율 조건 충족"
                ),
                "current_price": current_price,
                "condition_data": {
                    "per_ttm": info.get("per_ttm"),
                    "pbr": info.get("pbr"),
                    "gross_margin_pct": info.get("gross_margin_pct"),
                    "asset_turnover": info.get("asset_turnover"),
                },
            }

        if not signal:
            return

        code_name = strategy.universe.get(code, {}).get("code_name") or code

        # BUY는 중요 이벤트이므로 즉시 DB 저장한다.
        opened = open_virtual_position(
            strategy_name=name,
            code=code,
            code_name=code_name,
            entry_price=current_price,
            buy_reason_code=signal["reason_code"],
            buy_reason=signal["signal_reason"],
            state=state,
        )

        if opened:
            self._mark_entered_today(name, code)

            self._cache_open_position(
                strategy_name=name,
                code=code,
                code_name=code_name,
                entry_price=current_price,
                buy_reason_code=signal["reason_code"],
                buy_reason=signal["signal_reason"],
                state=state,
            )
            self._save_signal(
                strategy,
                code,
                "BUY",
                signal["reason_code"],
                signal["signal_reason"],
                current_price,
                signal.get("condition_data"),
            )

    # ------------------------------------------------------------------
    # SELL
    # ------------------------------------------------------------------

    def _process_sell(self, strategy, code, tick, position, current_price):
        name = strategy.strategy_name
        entry_price = float(position["entry_price"])
        return_pct = (
            (current_price - entry_price) / entry_price * 100.0
        )

        if name == "HighBreakoutStrategy":
            if return_pct <= strategy.STOP_LOSS_PCT:
                return self._close(
                    strategy,
                    code,
                    current_price,
                    "STOP_LOSS",
                    (
                        f"가상 수익률 {return_pct:.2f}% "
                        f"<= {strategy.STOP_LOSS_PCT:.2f}%"
                    ),
                    1.0,
                    {
                        "entry_price": entry_price,
                        "return_pct": return_pct,
                    },
                )

            ma20 = strategy._dynamic_ma20(code, current_price)
            if ma20 is not None and current_price < ma20:
                return self._close(
                    strategy,
                    code,
                    current_price,
                    "MA20_BREAKDOWN",
                    f"현재가 {current_price:,.0f}원 < MA20 {ma20:,.1f}원",
                    1.0,
                    {
                        "entry_price": entry_price,
                        "return_pct": return_pct,
                        "ma20": ma20,
                    },
                )
            return

        if name == "ValueQualityStrategy":
            if return_pct <= strategy.STOP_LOSS_PCT:
                return self._close(
                    strategy,
                    code,
                    current_price,
                    "STOP_LOSS",
                    (
                        f"가상 수익률 {return_pct:.2f}% "
                        f"<= {strategy.STOP_LOSS_PCT:.0f}%"
                    ),
                    1.0,
                    {
                        "entry_price": entry_price,
                        "return_pct": return_pct,
                    },
                )

            if return_pct >= strategy.TAKE_PROFIT_PCT:
                return self._close(
                    strategy,
                    code,
                    current_price,
                    "TAKE_PROFIT",
                    (
                        f"가상 수익률 {return_pct:.2f}% "
                        f">= +{strategy.TAKE_PROFIT_PCT:.0f}%"
                    ),
                    1.0,
                    {
                        "entry_price": entry_price,
                        "return_pct": return_pct,
                    },
                )
            return

        if name != "PullbackTrendStrategy":
            return

        state = position.setdefault("state", {})

        # 고정 손절은 과거 지표 유무와 무관하게 즉시 검사한다.
        if return_pct <= strategy.STOP_LOSS_PCT:
            return self._close(
                strategy,
                code,
                current_price,
                "STOP_LOSS",
                (
                    f"가상 수익률 {return_pct:.2f}% "
                    f"<= {strategy.STOP_LOSS_PCT:.0f}%"
                ),
                1.0,
                {
                    "entry_price": entry_price,
                    "return_pct": return_pct,
                },
            )

        metric = strategy._metric(code)
        if not metric:
            return

        mas = strategy._dynamic_mas(code, current_price)
        if mas is not None:
            _, ma20, _ = mas
            if current_price < ma20:
                return self._close(
                    strategy,
                    code,
                    current_price,
                    "MA20_BREAKDOWN",
                    f"현재가 {current_price:,.0f}원 < MA20 {ma20:,.1f}원",
                    1.0,
                    {
                        "entry_price": entry_price,
                        "return_pct": return_pct,
                        "ma20": ma20,
                    },
                )

        if strategy._bearish_volume_exit(code, tick):
            return self._close(
                strategy,
                code,
                current_price,
                "BEARISH_VOLUME",
                "전일 대비 거래량 15% 이상 증가 + 음봉",
                1.0,
                {
                    "entry_price": entry_price,
                    "return_pct": return_pct,
                },
            )

        today = datetime.now().strftime("%Y%m%d")
        previous_high = float(
            state.get("previous_high")
            or metric.get("previous_high", 0)
            or 0
        )

        # 전고점 도달 당일 누적거래량은 메모리에서만 계속 최신화한다.
        # 5초 스냅샷 때 state_json과 함께 DB에 저장된다.
        if state.get("high_touch_date") == today:
            state["high_touch_volume"] = int(
                tick.get("누적거래량", 0) or 0
            )
            self.dirty_position_keys.add(
                self._position_key(name, code)
            )

        if (
            state.get("partial_exit_requested")
            and strategy._is_next_day_volume_fade(code, tick, state)
        ):
            return self._close(
                strategy,
                code,
                current_price,
                "NEXT_DAY_VOLUME_FADE",
                "전고점 도달 다음 거래일 거래량 감소",
                float(position.get("remaining_ratio") or 1.0),
                {
                    "entry_price": entry_price,
                    "return_pct": return_pct,
                },
            )

        if (
            previous_high > 0
            and current_price >= previous_high
            and not state.get("partial_exit_requested")
        ):
            closed = self._close(
                strategy,
                code,
                current_price,
                "PREVIOUS_HIGH_PARTIAL_EXIT",
                "전고점 도달 30% 부분매도",
                strategy.PARTIAL_EXIT_RATIO,
                {
                    "entry_price": entry_price,
                    "return_pct": return_pct,
                    "previous_high": previous_high,
                },
            )

            if closed:
                remaining_position = self._get_cached_position(name, code)
                if remaining_position:
                    state = remaining_position.setdefault("state", {})
                    state["high_touch_date"] = today
                    state["high_touch_volume"] = int(
                        tick.get("누적거래량", 0) or 0
                    )
                    state["partial_exit_requested"] = True

                    # 부분청산 여부는 재시작 후에도 반드시 유지돼야 하므로
                    # 이 이벤트 순간에는 5초를 기다리지 않고 즉시 저장한다.
                    update_virtual_position(
                        name,
                        code,
                        current_price,
                        state=state,
                    )
                    self.dirty_position_keys.discard(
                        self._position_key(name, code)
                    )

    def _close(
        self,
        strategy,
        code,
        price,
        reason_code,
        reason,
        ratio,
        data,
    ):
        name = strategy.strategy_name
        key = self._position_key(name, code)
        position = self.position_cache.get(key)

        if not position:
            return False

        remaining_before = float(
            position.get("remaining_ratio") or 0
        )
        actual_ratio = max(
            0.0,
            min(float(ratio), remaining_before),
        )
        if actual_ratio <= 0:
            return False

        # SELL/부분청산은 중요 이벤트이므로 즉시 DB 저장한다.
        closed = close_virtual_position(
            strategy_name=name,
            code=code,
            exit_price=price,
            sell_reason_code=reason_code,
            sell_reason=reason,
            exit_ratio=actual_ratio,
        )

        if not closed:
            return False

        remaining_after = max(
            0.0,
            remaining_before - actual_ratio,
        )

        if remaining_after <= 1e-9:
            self.position_cache.pop(key, None)
            self.dirty_position_keys.discard(key)
        else:
            position["remaining_ratio"] = remaining_after
            self._update_cached_market_state(position, price)

        self._save_signal(
            strategy,
            code,
            "SELL",
            reason_code,
            reason,
            price,
            data,
        )
        return True
