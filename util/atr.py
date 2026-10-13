"""모멘텀 전략에서 사용하는 공용 Wilder ATR 계산과 과거 지표 조회.

주문/Qt 의존성이 없어 실주문과 가상매매에서 동일한 계산을 사용한다.
ATR은 첫 14개 TR의 단순평균으로 시작하고 이후 Wilder 방식으로 평활한다.
첫 일봉은 전일 종가 제공용이므로 최초 ATR에는 일봉 15개가 필요하다.
"""
from __future__ import annotations

from contextlib import closing
from itertools import groupby
from pathlib import Path
import math
import sqlite3


ATR_PERIOD = 14


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
                print(f"[ATR] 지표 제외 {code}: {exc}")
    return result
