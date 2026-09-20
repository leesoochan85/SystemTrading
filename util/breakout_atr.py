"""신고가 부근·돌파 전략용 Wilder ATR(14) 및 단조 증가 청산선.

주문/Qt 의존성이 없어 실주문과 가상매매에서 동일한 계산을 사용한다.
ATR은 첫 14개 TR의 단순평균으로 시작하고 이후 Wilder 방식으로 평활한다.
첫 일봉은 전일 종가 제공용이므로 최초 ATR에는 일봉 15개가 필요하다.
"""
from __future__ import annotations

from contextlib import closing
from itertools import groupby
from pathlib import Path
import json
import math
import sqlite3


ATR_PERIOD = 14
INITIAL_ATR_MULTIPLIER = 2.0
TRAILING_ATR_MULTIPLIER = 3.0


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value > 0


def iter_atr_bars(rows, period=ATR_PERIOD):
    """오름차순 (date, high, low, close) -> 완결일별 Wilder ATR.

    잘못된 OHLC를 건너뛰어 다른 기간을 14일로 오인하지 않고 오류로 처리한다.
    """
    if period < 1:
        raise ValueError("ATR period must be positive")
    previous_close = None
    previous_date = None
    seed = []
    atr = None
    for date, high, low, close in rows:
        date = str(date)
        if previous_date is not None and date <= previous_date:
            raise ValueError("ATR dates must be strictly increasing")
        if not all(_positive(v) for v in (high, low, close)):
            raise ValueError("ATR requires finite positive OHLC")
        high, low, close = float(high), float(low), float(close)
        if not low <= close <= high:
            raise ValueError("Invalid OHLC range")
        if previous_close is not None:
            tr = max(high - low, abs(high - previous_close), abs(low - previous_close))
            if atr is None:
                seed.append(tr)
                if len(seed) == period:
                    atr = sum(seed) / period
            else:
                atr = (atr * (period - 1) + tr) / period
            if atr is not None:
                yield {"date": date, "close": close, "atr": atr}
        previous_close = close
        previous_date = date


def _read_connection(db_path):
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)


def load_atr_metrics(codes, db_path, before_date, period=ATR_PERIOD):
    """모든 이력을 커서로 순회하되 종목별 최신 ATR만 보관한다 (32bit 메모리 제한)."""
    selected = {str(code).zfill(6) for code in codes}
    result = {}
    with closing(_read_connection(db_path)) as con:
        cursor = con.execute(
            "SELECT code, date, high, low, close FROM daily_price "
            "WHERE date < ? ORDER BY code, date", (before_date,),
        )
        for code, rows in groupby(cursor, key=lambda row: row[0]):
            if code not in selected:
                continue
            try:
                last = None
                for bar in iter_atr_bars((row[1:] for row in rows), period):
                    last = bar
                if last and _positive(last["atr"]):
                    result[code] = {"atr14": last["atr"], "atr_date": last["date"]}
            except ValueError as exc:
                print(f"[HighBreakout ATR] 지표 제외 {code}: {exc}")
    return result


def load_atr_history(code, db_path, before_date, period=ATR_PERIOD, from_date=None):
    """보유/진입 종목만 일별 ATR을 읽는다. 재시작 시 누락된 날의 청산선을 재구성한다."""
    with closing(_read_connection(db_path)) as con:
        rows = con.execute(
            "SELECT date, high, low, close FROM daily_price "
            "WHERE code = ? AND date < ? ORDER BY date", (str(code).zfill(6), before_date),
        )
        result = []
        preceding = None
        for bar in iter_atr_bars(rows, period):
            if from_date is not None and bar["date"] < from_date:
                preceding = bar
                continue
            if preceding is not None:
                result.append(preceding)
                preceding = None
            result.append(bar)
        if not result and preceding is not None:
            result.append(preceding)
        return result


def new_atr_state(entry_price, entry_at, bars, initial_multiple=INITIAL_ATR_MULTIPLIER):
    """진입일 전까지 알려진 ATR을 고정한다. 기존 보유도 실제 진입일을 기준으로 복원한다."""
    entry_at = str(entry_at or "")
    entry_date = entry_at[:8]
    if len(entry_date) != 8 or not entry_date.isdigit() or not _positive(entry_price):
        raise ValueError("ATR 상태에 유효한 실제 진입일/매입가가 필요합니다")
    before_entry = [bar for bar in bars if bar["date"] < entry_date]
    if not before_entry or not _positive(before_entry[-1]["atr"]):
        raise ValueError("매수일 이전 ATR 이력이 부족합니다. 과거 일봉을 보충하세요")
    return new_atr_state_from_value(
        entry_price,
        entry_at,
        before_entry[-1]["atr"],
        before_entry[-1]["date"],
        initial_multiple,
    )


def new_atr_state_from_value(
    entry_price,
    entry_at,
    atr,
    atr_date,
    initial_multiple=INITIAL_ATR_MULTIPLIER,
):
    """이관 시 과거 진입 전 봉이 없을 때도 명시적인 ATR 값으로 상태를 만든다."""
    entry_at = str(entry_at or "")
    if len(entry_at[:8]) != 8 or not entry_at[:8].isdigit():
        raise ValueError("ATR 상태에 유효한 실제 진입일이 필요합니다")
    if not _positive(entry_price) or not _positive(atr):
        raise ValueError("ATR 상태에 유효한 매입가/ATR이 필요합니다")
    atr = float(atr)
    stop = float(entry_price) - initial_multiple * atr
    if stop <= 0:
        raise ValueError("ATR 초기 청산선이 0원 이하입니다")
    return {
        "version": 1,
        "entry_at": entry_at,
        "entry_price": float(entry_price),
        "entry_atr": atr,
        "initial_stop": stop,
        "stop_price": stop,
        "highest_close": 0.0,
        "last_bar_date": str(atr_date),
    }


def advance_atr_state(state, entry_price, bars,
                      initial_multiple=INITIAL_ATR_MULTIPLIER,
                      trailing_multiple=TRAILING_ATR_MULTIPLIER):
    """완결봉으로만 갱신. ATR 확대/재시작/부분체결 평단 변경에도 청산선은 낮추지 않는다."""
    updated = dict(state)
    if not all(_positive(updated.get(key)) for key in ("entry_atr", "initial_stop", "stop_price")):
        raise ValueError("저장된 ATR 청산 상태가 유효하지 않습니다")
    if not _positive(entry_price):
        raise ValueError("Invalid entry price")
    updated["entry_price"] = float(entry_price)
    updated["initial_stop"] = max(
        float(updated["initial_stop"]), float(entry_price) - initial_multiple * updated["entry_atr"],
    )
    updated["stop_price"] = max(updated["stop_price"], updated["initial_stop"])
    for bar in bars:
        if bar["date"] < updated["entry_at"][:8] or bar["date"] <= updated["last_bar_date"]:
            continue
        if not _positive(bar["atr"]):
            continue
        updated["highest_close"] = max(updated["highest_close"], bar["close"])
        updated["stop_price"] = max(
            updated["stop_price"], updated["highest_close"] - trailing_multiple * bar["atr"],
        )
        updated["last_bar_date"] = bar["date"]
    return updated


def atr_exit_signal(current_price, state):
    if not _positive(current_price) or current_price > state["stop_price"]:
        return None
    trailing = state["stop_price"] > state["initial_stop"]
    reason = "ATR 추적 청산선 도달" if trailing else "ATR 초기 손절선 도달"
    return {
        "reason_code": "ATR_TRAILING_STOP" if trailing else "ATR_INITIAL_STOP",
        "signal_reason": f"{reason}: 현재가 {current_price:,.0f}원 <= 청산선 {state['stop_price']:,.2f}원",
        "current_price": current_price,
        "condition_data": dict(state),
        "market_order": True,
    }


class AtrStateStore:
    """실계좌 전용 상태. 가상 포지션은 기존 monitoring.db의 state_json을 사용한다."""
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=15)
        con.execute("CREATE TABLE IF NOT EXISTS breakout_atr_state (code TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
        return con

    def load(self, code):
        with closing(self._connect()) as con:
            row = con.execute("SELECT state_json FROM breakout_atr_state WHERE code = ?", (str(code).zfill(6),)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, code, state):
        with closing(self._connect()) as con, con:
            con.execute("INSERT OR REPLACE INTO breakout_atr_state VALUES (?, ?)",
                        (str(code).zfill(6), json.dumps(state, ensure_ascii=False, allow_nan=False)))

    def delete(self, code):
        with closing(self._connect()) as con, con:
            con.execute("DELETE FROM breakout_atr_state WHERE code = ?", (str(code).zfill(6),))
