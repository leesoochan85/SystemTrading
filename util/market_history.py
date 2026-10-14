"""64bit 외부 수집기와 32bit Kiwoom 자동매매가 공유하는 시장 일봉 저장소.

역할 분리
---------
1) 64bit Python
   - pykrx 최신 버전으로 과거 일봉을 수집한다.
   - market_history.db의 daily_price 테이블에 저장한다.

2) 32bit Python (Kiwoom)
   - pykrx를 사용하지 않는다.
   - market_history.db의 과거 일봉을 읽어서 전략 계산에 사용한다.
   - 장 종료 후 Kiwoom 실시간 체결로 완성된 당일 OHLCV를 같은 DB에 이어 저장한다.

두 프로세스가 같은 프로젝트 폴더의 DB를 바라보도록 DB_PATH를 이 파일 기준 절대경로로 잡는다.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import time
from typing import Dict, Iterable, Iterator

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "market_history.db"
DEFAULT_LOOKBACK_CALENDAR_DAYS = 400
DEFAULT_REQUIRED_HISTORY_ROWS = 180
DEFAULT_INCREMENTAL_OVERLAP_CALENDAR_DAYS = 10
EXCLUDED_COMPANY_NAME_KEYWORDS = ("스팩", "리츠")
SQLITE_BUSY_TIMEOUT_MS = 15_000


@contextmanager
def _connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """SQLite 연결을 트랜잭션 종료 후 운영체제 수준에서도 확실히 닫는다."""
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    try:
        con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        # 읽기(32bit 자동매매)와 쓰기(64bit 수집기/장마감 저장)의 충돌 가능성을 줄인다.
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_price (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open INTEGER NOT NULL,
                high INTEGER NOT NULL,
                low INTEGER NOT NULL,
                close INTEGER NOT NULL,
                volume INTEGER NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (code, date)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_price_date ON daily_price(date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_price_code_date ON daily_price(code, date)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS stock_master (
                code TEXT PRIMARY KEY,
                code_name TEXT NOT NULL,
                market TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_master_market ON stock_master(market)")
        con.execute("""CREATE TABLE IF NOT EXISTS history_backfill_state (
            code TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            required_rows INTEGER NOT NULL, stored_count INTEGER NOT NULL
        )""")
        with con:
            yield con
    finally:
        con.close()


def get_db_path() -> Path:
    return DB_PATH


def _normalize_code_ohlcv_frame(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """종목 1개의 기간 OHLCV를 daily_price 저장 형식으로 변환한다."""
    if df is None or df.empty:
        return pd.DataFrame()

    frame = df.copy()
    aliases = {
        "시가": "open",
        "고가": "high",
        "저가": "low",
        "종가": "close",
        "거래량": "volume",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    rename_map = {col: aliases[col] for col in frame.columns if col in aliases}
    frame = frame.rename(columns=rename_map)

    required = ["open", "high", "low", "close", "volume"]
    if not all(col in frame.columns for col in required):
        raise RuntimeError(
            "외부 OHLCV 컬럼 형식이 예상과 다릅니다. "
            f"code={code}, received={list(frame.columns)}"
        )

    frame = frame[required].copy()
    for col in required:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.dropna(subset=required)
    frame = frame[
        (frame["open"] > 0)
        & (frame["high"] > 0)
        & (frame["low"] > 0)
        & (frame["close"] > 0)
        & (frame["volume"] >= 0)
    ]
    if frame.empty:
        return frame

    # 최신 pykrx의 종목별 기간 조회는 DatetimeIndex(이름: 날짜)를 반환한다.
    date_index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.loc[~date_index.isna()].copy()
    date_index = date_index[~date_index.isna()]
    frame["date"] = date_index.strftime("%Y%m%d")
    frame["code"] = str(code).strip().upper().zfill(6)
    frame["source"] = "pykrx_64bit"
    frame["updated_at"] = datetime.now().strftime("%Y%m%d%H%M%S")

    for col in required:
        frame[col] = frame[col].round().astype("int64")

    return frame.reset_index(drop=True)[
        ["code", "date", "open", "high", "low", "close", "volume", "source", "updated_at"]
    ]


def _fetch_code_ohlcv(start_date: str, end_date: str, code: str) -> pd.DataFrame:
    """종목 1개의 기간 OHLCV를 조회한다.

    일반 주식 조회를 먼저 시도하고, pykrx가 주식 ISIN을 찾지 못하거나
    빈 DataFrame을 반환하면 ETF 전용 조회로 자동 전환한다.
    """
    try:
        from pykrx import stock
    except ImportError as exc:
        raise RuntimeError(
            "pykrx가 설치되어 있지 않습니다. 이 함수는 Kiwoom 32bit 환경이 아니라 "
            "64bit marketdata 환경에서 실행해야 합니다."
        ) from exc

    normalized_code = str(code).strip().upper().zfill(6)
    market_error = None

    # 1) 일반 주식 경로
    if hasattr(stock, "get_market_ohlcv"):
        try:
            result = stock.get_market_ohlcv(
                start_date,
                end_date,
                normalized_code,
                adjusted=False,
            )
            if result is not None and not result.empty:
                return result
        except Exception as exc:
            # ETF를 일반 주식 API로 조회하면 pykrx 내부에서 ISIN 조회 오류가
            # 발생할 수 있으므로, 여기서는 ETF fallback을 위해 보관만 한다.
            market_error = exc

    # 2) ETF 경로
    if hasattr(stock, "get_etf_ohlcv_by_date"):
        try:
            result = stock.get_etf_ohlcv_by_date(
                start_date,
                end_date,
                normalized_code,
            )
            if result is not None and not result.empty:
                return result
        except Exception as etf_exc:
            if market_error is not None:
                raise RuntimeError(
                    f"일반주식/ETF OHLCV 조회 모두 실패: "
                    f"market={market_error} / etf={etf_exc}"
                ) from etf_exc
            raise

    if market_error is not None:
        raise market_error

    return pd.DataFrame()


def _is_company_stock_name(code_name: str) -> bool:
    """신고가/가치전략 대상에서 SPAC·리츠를 제외한다.

    KOSPI/KOSDAQ 주식 ticker 목록 자체가 ETF/ETN/ELW와는 별도이므로,
    여기서는 기업주식 성격과 거리가 있는 SPAC/리츠만 이름으로 추가 제외한다.
    우선주는 기업 주식이므로 유지한다.
    """
    name = str(code_name or "").strip()
    if not name:
        return False
    return not any(keyword in name for keyword in EXCLUDED_COMPANY_NAME_KEYWORDS)


def load_company_stock_master(db_path: Path = DB_PATH, read_only=False) -> pd.DataFrame:
    """32bit Kiwoom도 사용할 수 있도록 DB에 저장된 활성 기업주식 목록을 읽는다."""
    connection = (closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15))
                  if read_only else _connect(db_path))
    with connection as con:
        df = pd.read_sql_query(
            """
            SELECT code, code_name, market
            FROM stock_master
            WHERE active = 1
            ORDER BY market, code
            """,
            con,
        )
    return df


def refresh_company_stock_master(
    as_of_date: str | None = None,
    db_path: Path = DB_PATH,
) -> pd.DataFrame:
    """64bit pykrx로 KOSPI+KOSDAQ 기업주식 마스터를 갱신한다.

    종목명은 기존 stock_master에 있으면 재사용하고, 신규 코드만 pykrx에서 조회한다.
    """
    try:
        from pykrx import stock
    except ImportError as exc:
        raise RuntimeError(
            "기업주식 마스터 갱신은 pykrx가 설치된 64bit marketdata 환경에서 실행해야 합니다."
        ) from exc

    lookup_date = as_of_date
    market_codes: dict[str, list[str]] = {}

    for market in ("KOSPI", "KOSDAQ"):
        codes = stock.get_market_ticker_list(lookup_date, market=market)
        market_codes[market] = [
            str(code).strip().upper().zfill(6)
            for code in codes
            if str(code).strip()
        ]

    with _connect(db_path) as con:
        existing_rows = con.execute(
            "SELECT code, code_name FROM stock_master"
        ).fetchall()
    existing_names = {str(code): str(name) for code, name in existing_rows}

    rows = []
    now_text = datetime.now().strftime("%Y%m%d%H%M%S")
    for market, codes in market_codes.items():
        for code in codes:
            code_name = existing_names.get(code)
            if not code_name:
                try:
                    code_name = str(stock.get_market_ticker_name(code) or "").strip()
                except Exception as exc:
                    print(f"[종목명 조회 실패] {code}: {exc}")
                    code_name = ""

            if not _is_company_stock_name(code_name):
                continue

            rows.append((code, code_name, market, 1, now_text))

    active_codes = {row[0] for row in rows}
    with _connect(db_path) as con:
        con.execute("UPDATE stock_master SET active = 0")
        con.executemany(
            """
            INSERT INTO stock_master (code, code_name, market, active, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                code_name=excluded.code_name,
                market=excluded.market,
                active=excluded.active,
                updated_at=excluded.updated_at
            """,
            rows,
        )

    df = pd.DataFrame(rows, columns=["code", "code_name", "market", "active", "updated_at"])
    if df.empty:
        raise RuntimeError("KOSPI/KOSDAQ 기업주식 목록을 한 종목도 만들지 못했습니다.")

    print(
        f"[기업주식 마스터 갱신] {len(df)}종목 "
        f"(KOSPI {(df['market'] == 'KOSPI').sum()} / "
        f"KOSDAQ {(df['market'] == 'KOSDAQ').sum()})"
    )
    return df[["code", "code_name", "market"]].copy()


def bootstrap_market_history(
    db_path: Path = DB_PATH,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
    request_sleep: float = 0.35,
    codes: Iterable[str] | None = None,
    incremental_overlap_calendar_days: int = DEFAULT_INCREMENTAL_OVERLAP_CALENDAR_DAYS,
    required_history_rows: int = DEFAULT_REQUIRED_HISTORY_ROWS,
) -> dict:
    """현재 전략 유니버스의 과거 일봉을 종목별 기간 조회로 적재한다.

    동작 방식
    ---------
    - required_history_rows 미만: 최신 날짜까지 있어도 과거 조회 범위를 다시 수집한다.
    - 충분한 이력: 마지막 저장일 직전 며칠부터 어제까지 누락분만 보충한다.
      기존 날짜는 (code, date) PRIMARY KEY + INSERT OR REPLACE로 안전하게 덮어쓴다.

    따라서 Kiwoom 프로그램이 장마감까지 실행된 날은 당일 일봉이 DB에 이어지고,
    실행되지 못한 날은 다음 수집기 실행 시 pykrx가 빠진 구간을 보충한다.
    """
    if required_history_rows < 1 or lookback_calendar_days < 1:
        raise ValueError("required_history_rows와 lookback_calendar_days는 양수여야 합니다.")
    end_date = datetime.now().date() - timedelta(days=1)
    start_date = end_date - timedelta(days=lookback_calendar_days)
    start_text = start_date.strftime("%Y%m%d")
    end_text = end_date.strftime("%Y%m%d")

    if codes is None:
        company_df = refresh_company_stock_master(
            as_of_date=end_text,
            db_path=db_path,
        )
        target_codes = company_df["code"].astype(str).tolist()
    else:
        target_codes = list(
            dict.fromkeys(str(code).strip().upper().zfill(6) for code in codes)
        )

    if not target_codes:
        raise RuntimeError("과거 일봉을 수집할 종목코드가 없습니다.")

    print(
        f"[수집 대상] {len(target_codes)}종목 / "
        f"기간 {start_text}~{end_text} / 종목별 기간 조회"
    )

    fetched_codes = 0
    up_to_date_codes = 0
    initial_build_codes = 0
    incremental_codes = 0
    backfill_codes = 0
    inserted_rows = 0
    failed_codes = []
    empty_codes = []

    completed_before = (end_date + timedelta(days=1)).strftime("%Y%m%d")
    history_counts = get_code_history_counts(
        target_codes, db_path=db_path, before_date=completed_before
    )
    latest_dates = get_code_latest_dates(target_codes, db_path=db_path)
    with _connect(db_path) as con:
        full_scans = {row[0]: row[1:] for row in con.execute("SELECT * FROM history_backfill_state")}

    overlap_days = max(1, int(incremental_overlap_calendar_days))

    for idx, code in enumerate(target_codes, start=1):
        stored_count = int(history_counts.get(code, 0) or 0)
        latest_date_text = latest_dates.get(code)

        # Verify the configured history target. Recent dates alone do not prove
        # sufficient history; refill the requested past range when short.
        needs_backfill = stored_count < required_history_rows
        scan = full_scans.get(code)
        recent_full_scan = bool(scan and scan[0] <= start_text
            and scan[2] == required_history_rows and stored_count >= scan[3]
            and 0 <= (end_date - datetime.strptime(scan[1], "%Y%m%d").date()).days < 7)
        if latest_date_text and latest_date_text >= end_text and (not needs_backfill or recent_full_scan):
            up_to_date_codes += 1
            print(
                f"[{idx}/{len(target_codes)}] {code}: 최신 {latest_date_text} / "
                f"{stored_count}거래일 / "
                f"{'최근 전체 범위 조회 완료' if needs_backfill else '목표 이력 충족'} - 건너뜀"
            )
            continue

        if not latest_date_text:
            fetch_start_text = start_text
            mode_text = f"초기 구축 ({stored_count}/{required_history_rows}봉)"
            initial_build_codes += 1
        elif needs_backfill and not recent_full_scan:
            fetch_start_text = start_text
            mode_text = f"과거 이력 보충 ({stored_count}/{required_history_rows}봉)"
            backfill_codes += 1
        else:
            latest_date = datetime.strptime(latest_date_text, "%Y%m%d").date()
            fetch_start_date = latest_date - timedelta(days=overlap_days)
            fetch_start_text = fetch_start_date.strftime("%Y%m%d")
            mode_text = f"증분 보충 (기존 {stored_count}봉 / 최신 {latest_date_text})"
            incremental_codes += 1

        print(
            f"[{idx}/{len(target_codes)}] {code}: {mode_text} / "
            f"조회 {fetch_start_text}~{end_text}"
        )

        try:
            raw = _fetch_code_ohlcv(fetch_start_text, end_text, code)
            if raw is None or raw.empty:
                empty_codes.append(code)
                print(f"[{idx}/{len(target_codes)}] {code}: 데이터 없음")
                time.sleep(request_sleep)
                continue

            frame = _normalize_code_ohlcv_frame(raw, code)
            if frame.empty:
                empty_codes.append(code)
                print(f"[{idx}/{len(target_codes)}] {code}: 정규화 후 0건")
                time.sleep(request_sleep)
                continue

            rows = [tuple(row) for row in frame.itertuples(index=False, name=None)]
            with _connect(db_path) as con:
                con.executemany("""
                    INSERT OR REPLACE INTO daily_price
                    (code, date, open, high, low, close, volume, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
                if fetch_start_text == start_text:
                    count = con.execute("SELECT COUNT(*) FROM daily_price WHERE code=? AND date<=?",
                                        (code, end_text)).fetchone()[0]
                    con.execute("""INSERT INTO history_backfill_state VALUES (?,?,?,?,?)
                        ON CONFLICT(code) DO UPDATE SET start_date=excluded.start_date,
                        end_date=excluded.end_date, required_rows=excluded.required_rows,
                        stored_count=excluded.stored_count""",
                        (code, start_text, end_text, required_history_rows, count))

            fetched_codes += 1
            inserted_rows += len(frame)
            print(
                f"[{idx}/{len(target_codes)}] {code}: "
                f"{len(frame)}거래일 저장 "
                f"({frame.iloc[0]['date']}~{frame.iloc[-1]['date']})"
            )

        except Exception as exc:
            failed_codes.append((code, str(exc)))
            print(f"[{idx}/{len(target_codes)}] {code}: 실패 - {exc}")

        time.sleep(request_sleep)

    status = get_history_status(db_path=db_path)
    completed_counts = get_code_history_counts(
        target_codes, db_path=db_path, before_date=completed_before
    )
    missing_codes = [code for code in target_codes
                     if completed_counts.get(code, 0) < required_history_rows]

    # Newly listed stocks may not have enough actual trading days. Keep
    # their real history and report the shortage without padding or deleting.
    if missing_codes:
        preview = ", ".join(missing_codes[:10])
        print(
            f"[이력 축적 중] 목표 {required_history_rows}봉 미만 {len(missing_codes)}종목 - "
            f"목표 이력 부족 (다른 전략의 자격은 별도 판단). 예: {preview}"
        )

    return {
        **status,
        "target_code_count": len(target_codes),
        "fetched_codes": fetched_codes,
        "up_to_date_codes": up_to_date_codes,
        "initial_build_codes": initial_build_codes,
        "incremental_codes": incremental_codes,
        "backfill_codes": backfill_codes,
        "required_history_rows": required_history_rows,
        "inserted_rows": inserted_rows,
        "eligible_code_count": len(target_codes) - len(missing_codes),
        "insufficient_history_count": len(missing_codes),
        "insufficient_history_codes": missing_codes,
        "empty_codes": empty_codes,
        "failed_codes": failed_codes,
        "db_path": str(Path(db_path).resolve()),
    }

def get_history_status(db_path: Path = DB_PATH) -> dict:
    with _connect(db_path) as con:
        date_count = int(
            con.execute("SELECT COUNT(DISTINCT date) FROM daily_price").fetchone()[0] or 0
        )
        code_count = int(
            con.execute("SELECT COUNT(DISTINCT code) FROM daily_price").fetchone()[0] or 0
        )
        row_count = int(con.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0] or 0)
        latest_date = con.execute("SELECT MAX(date) FROM daily_price").fetchone()[0]
        earliest_date = con.execute("SELECT MIN(date) FROM daily_price").fetchone()[0]

    return {
        "date_count": date_count,
        "code_count": code_count,
        "row_count": row_count,
        "earliest_date": earliest_date,
        "latest_date": latest_date,
    }


def get_code_history_counts(codes: Iterable[str], db_path: Path = DB_PATH,
                            before_date: str | None = None) -> Dict[str, int]:
    normalized = list(dict.fromkeys(str(code).strip().upper().zfill(6) for code in codes))
    if not normalized:
        return {}

    placeholders = ",".join("?" for _ in normalized)
    date_filter = " AND date < ?" if before_date is not None else ""
    params = normalized + ([before_date] if before_date is not None else [])
    with _connect(db_path) as con:
        rows = con.execute(
            f"SELECT code, COUNT(*) FROM daily_price WHERE code IN ({placeholders}){date_filter} GROUP BY code",
            params,
        ).fetchall()

    counts = {str(code): int(count) for code, count in rows}
    return {code: counts.get(code, 0) for code in normalized}


def get_code_latest_dates(codes: Iterable[str], db_path: Path = DB_PATH) -> Dict[str, str | None]:
    """종목별 DB 최신 일봉 날짜(YYYYMMDD)를 반환한다."""
    normalized = list(dict.fromkeys(str(code).strip().upper().zfill(6) for code in codes))
    if not normalized:
        return {}

    placeholders = ",".join("?" for _ in normalized)
    with _connect(db_path) as con:
        rows = con.execute(
            f"SELECT code, MAX(date) FROM daily_price WHERE code IN ({placeholders}) GROUP BY code",
            normalized,
        ).fetchall()

    latest = {str(code): (str(date) if date else None) for code, date in rows}
    return {code: latest.get(code) for code in normalized}


def build_realtime_daily_bars(realtime_map: Dict[str, dict], codes: Iterable[str] | None = None) -> Dict[str, dict]:
    target_codes = None
    if codes is not None:
        target_codes = {str(code).strip().upper().zfill(6) for code in codes}

    bars: Dict[str, dict] = {}
    for raw_code, rt in realtime_map.items():
        code = str(raw_code).strip().upper().zfill(6)
        if target_codes is not None and code not in target_codes:
            continue

        bar = {
            "open": int(rt.get("시가", 0) or 0),
            "high": int(rt.get("고가", 0) or 0),
            "low": int(rt.get("저가", 0) or 0),
            "close": int(rt.get("현재가", 0) or 0),
            "volume": int(rt.get("누적거래량", 0) or 0),
        }
        if min(bar["open"], bar["high"], bar["low"], bar["close"]) <= 0:
            continue
        bars[code] = bar
    return bars


def save_realtime_daily_bars(
    bars: Dict[str, dict],
    trade_date: str | None = None,
    db_path: Path = DB_PATH,
) -> int:
    """32bit Kiwoom이 장마감 후 당일 완성 OHLCV를 같은 DB에 저장한다."""
    trade_date = trade_date or datetime.now().strftime("%Y%m%d")
    rows = []
    now_text = datetime.now().strftime("%Y%m%d%H%M%S")

    for code, bar in bars.items():
        open_price = int(bar.get("open", 0) or 0)
        high = int(bar.get("high", 0) or 0)
        low = int(bar.get("low", 0) or 0)
        close = int(bar.get("close", 0) or 0)
        volume = int(bar.get("volume", 0) or 0)
        if min(open_price, high, low, close) <= 0:
            continue
        rows.append(
            (
                str(code).strip().upper().zfill(6),
                trade_date,
                open_price,
                high,
                low,
                close,
                volume,
                "kiwoom_realtime_32bit",
                now_text,
            )
        )

    if not rows:
        return 0

    with _connect(db_path) as con:
        con.executemany("""
            INSERT OR REPLACE INTO daily_price
            (code, date, open, high, low, close, volume, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
    return len(rows)
