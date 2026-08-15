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

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import time
from typing import Dict, Iterable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "market_history.db"
MIN_HISTORY_DAYS = 65
DEFAULT_LOOKBACK_CALENDAR_DAYS = 180
DEFAULT_INCREMENTAL_OVERLAP_CALENDAR_DAYS = 10
EXCLUDED_COMPANY_NAME_KEYWORDS = ("스팩", "리츠")
SQLITE_BUSY_TIMEOUT_MS = 15_000


def _connect(db_path: Path = DB_PATH):
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
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
    return con


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


def load_company_stock_master(db_path: Path = DB_PATH) -> pd.DataFrame:
    """32bit Kiwoom도 사용할 수 있도록 DB에 저장된 활성 기업주식 목록을 읽는다."""
    with _connect(db_path) as con:
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


def get_current_universe_codes(db_path: Path = DB_PATH) -> list[str]:
    """현재 활성 KOSPI+KOSDAQ 기업주식 전체 코드를 반환한다.

    64bit bootstrap 실행 시에는 pykrx로 stock_master를 먼저 갱신하고,
    32bit 런타임에서는 이 함수가 외부 조회 없이 DB만 읽는다.
    """
    df = load_company_stock_master(db_path=db_path)
    if df.empty:
        return []
    return list(dict.fromkeys(df["code"].astype(str).str.upper().str.zfill(6).tolist()))

def bootstrap_market_history(
    db_path: Path = DB_PATH,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
    request_sleep: float = 0.35,
    codes: Iterable[str] | None = None,
    incremental_overlap_calendar_days: int = DEFAULT_INCREMENTAL_OVERLAP_CALENDAR_DAYS,
) -> dict:
    """현재 전략 유니버스의 과거 일봉을 종목별 기간 조회로 적재한다.

    동작 방식
    ---------
    - 65거래일 미만: 최근 lookback_calendar_days 전체 구간을 조회해 초기 구축한다.
    - 65거래일 이상: 마지막 저장일 직전 며칠부터 어제까지 다시 조회해 누락분만 보충한다.
      기존 날짜는 (code, date) PRIMARY KEY + INSERT OR REPLACE로 안전하게 덮어쓴다.

    따라서 Kiwoom 프로그램이 장마감까지 실행된 날은 당일 일봉이 DB에 이어지고,
    실행되지 못한 날은 다음 수집기 실행 시 pykrx가 빠진 구간을 보충한다.
    """
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
    inserted_rows = 0
    failed_codes = []
    empty_codes = []

    history_counts = get_code_history_counts(target_codes, db_path=db_path)
    latest_dates = get_code_latest_dates(target_codes, db_path=db_path)

    overlap_days = max(1, int(incremental_overlap_calendar_days))

    for idx, code in enumerate(target_codes, start=1):
        stored_count = int(history_counts.get(code, 0) or 0)
        latest_date_text = latest_dates.get(code)

        # 최신 일봉까지 이미 있으면 이력 길이와 관계없이 외부 요청을 건너뛴다.
        # 신규상장 종목처럼 65거래일 미만인 종목도 DB에는 그대로 유지하고
        # Kiwoom 장마감 저장 또는 다음 증분 수집을 통해 자연스럽게 일봉을 쌓는다.
        if latest_date_text and latest_date_text >= end_text:
            up_to_date_codes += 1
            eligibility = (
                "전략대상" if stored_count >= MIN_HISTORY_DAYS
                else f"이력축적중({stored_count}/{MIN_HISTORY_DAYS})"
            )
            print(
                f"[{idx}/{len(target_codes)}] {code}: "
                f"최신 {latest_date_text} / {stored_count}거래일 / {eligibility} - 건너뜀"
            )
            continue

        # 데이터가 한 건도 없을 때만 180일 전체 초기 구축을 수행한다.
        # 1거래일이라도 이미 있으면 65일 미만이어도 마지막 저장일 주변만 증분 보충한다.
        if not latest_date_text:
            fetch_start_text = start_text
            mode_text = f"초기 구축 ({stored_count}거래일 보유)"
            initial_build_codes += 1
        else:
            latest_date = datetime.strptime(latest_date_text, "%Y%m%d").date()
            fetch_start_date = latest_date - timedelta(days=overlap_days)
            fetch_start_text = fetch_start_date.strftime("%Y%m%d")
            eligibility = (
                "전략대상" if stored_count >= MIN_HISTORY_DAYS
                else f"이력축적중({stored_count}/{MIN_HISTORY_DAYS})"
            )
            mode_text = (
                f"증분 보충 (기존 {stored_count}거래일 / "
                f"최신 {latest_date_text} / {eligibility})"
            )
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
    missing_codes = get_missing_history_codes(
        target_codes,
        minimum_rows=MIN_HISTORY_DAYS,
        db_path=db_path,
    )

    # 전체시장에는 신규상장 등으로 구조적으로 65거래일을 채울 수 없는 종목이
    # 항상 존재할 수 있다. 따라서 bootstrap 자체를 실패시키지 않는다.
    # 이 종목들은 DB에 계속 누적하고, 65거래일을 채운 날부터 전략 계산에 자동 편입한다.
    if missing_codes:
        preview = ", ".join(missing_codes[:10])
        print(
            f"[이력 축적 중] {MIN_HISTORY_DAYS}거래일 미만 {len(missing_codes)}종목 - "
            f"현재는 전략 대상에서 제외됩니다. 예: {preview}"
        )

    return {
        **status,
        "target_code_count": len(target_codes),
        "fetched_codes": fetched_codes,
        "up_to_date_codes": up_to_date_codes,
        "initial_build_codes": initial_build_codes,
        "incremental_codes": incremental_codes,
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


def get_code_history_counts(codes: Iterable[str], db_path: Path = DB_PATH) -> Dict[str, int]:
    normalized = list(dict.fromkeys(str(code).strip().upper().zfill(6) for code in codes))
    if not normalized:
        return {}

    placeholders = ",".join("?" for _ in normalized)
    with _connect(db_path) as con:
        rows = con.execute(
            f"SELECT code, COUNT(*) FROM daily_price WHERE code IN ({placeholders}) GROUP BY code",
            normalized,
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

def get_missing_history_codes(
    codes: Iterable[str],
    minimum_rows: int = MIN_HISTORY_DAYS,
    db_path: Path = DB_PATH,
) -> list[str]:
    counts = get_code_history_counts(codes, db_path=db_path)
    return [code for code, count in counts.items() if count < minimum_rows]


def load_daily_price_map(
    codes: Iterable[str],
    minimum_rows: int = MIN_HISTORY_DAYS,
    db_path: Path = DB_PATH,
) -> Dict[str, pd.DataFrame]:
    """32bit 자동매매가 공통 과거 일봉을 읽는 주 진입점.

    반환 DataFrame 형식은 기존 전략의 price_df와 동일하게
    index=date, columns=open/high/low/close/volume 로 맞춘다.
    """
    normalized = list(dict.fromkeys(str(code).strip().upper().zfill(6) for code in codes))
    if not normalized:
        return {}

    placeholders = ",".join("?" for _ in normalized)
    with _connect(db_path) as con:
        df = pd.read_sql_query(
            f"""
            SELECT code, date, open, high, low, close, volume
            FROM daily_price
            WHERE code IN ({placeholders})
            ORDER BY code, date
            """,
            con,
            params=normalized,
        )

    result: Dict[str, pd.DataFrame] = {}
    if df.empty:
        return result

    for code, group in df.groupby("code", sort=False):
        group = group.sort_values("date").copy()
        if len(group) < minimum_rows:
            continue
        price_df = group[["date", "open", "high", "low", "close", "volume"]].copy()
        price_df["date"] = price_df["date"].astype(str)
        price_df = price_df.set_index("date")
        for col in ["open", "high", "low", "close", "volume"]:
            price_df[col] = pd.to_numeric(price_df[col], errors="coerce")
        result[str(code)] = price_df

    return result


def load_breakout_metrics(
    codes: Iterable[str],
    breakout_window: int = 60,
    volume_window: int = 20,
    ma_window: int = 20,
    db_path: Path = DB_PATH,
) -> Dict[str, dict]:
    """종목별 실제 최근 일봉을 기준으로 신고가 계산값을 만든다.

    기존 방식처럼 시장 전체의 최근 N개 날짜를 공통 cutoff로 사용하지 않는다.
    거래정지·휴지·개별 데이터 공백이 있었던 종목도 그 종목 자체의 최근
    거래일 기준으로 필요한 봉 수를 확보한다.

    SQLite window function으로 종목별 최신 max_window개 행만 읽기 때문에
    daily_price 전체를 pandas에 올리는 것보다 메모리 사용도 제한된다.
    """
    normalized = {str(code).strip().upper().zfill(6) for code in codes}
    if not normalized:
        return {}

    max_window = max(breakout_window, volume_window, ma_window)

    with _connect(db_path) as con:
        df = pd.read_sql_query(
            """
            WITH ranked AS (
                SELECT
                    code, date, open, high, low, close, volume,
                    ROW_NUMBER() OVER (
                        PARTITION BY code
                        ORDER BY date DESC
                    ) AS rn
                FROM daily_price
            )
            SELECT code, date, open, high, low, close, volume
            FROM ranked
            WHERE rn <= ?
            ORDER BY code, date
            """,
            con,
            params=(max_window,),
        )

    if df.empty:
        return {}

    # stock_master 전체가 아닌 특정 codes가 전달되는 경우도 안전하게 지원한다.
    df = df[df["code"].isin(normalized)]
    if df.empty:
        return {}

    metrics: Dict[str, dict] = {}
    for code, group in df.groupby("code", sort=False):
        group = group.sort_values("date").copy()

        # 각 종목 자체의 실제 최근 거래일 기준으로 최소 60봉을 요구한다.
        if len(group) < max_window:
            continue

        for column in ("high", "close", "volume"):
            group[column] = pd.to_numeric(group[column], errors="coerce")

        group = group.dropna(subset=["high", "close", "volume"])
        if len(group) < max_window:
            continue

        last_breakout = group.tail(breakout_window)
        last_volume = group.tail(volume_window)
        last_ma = group.tail(ma_window)
        last_19 = group.tail(19)

        # 최근 20거래일 평균 거래대금.
        # 별도 TR/FID를 사용하지 않고 저장된 일봉의 종가 × 거래량으로 계산한다.
        trading_value_ma20 = float(
            (
                last_volume["close"].astype("float64")
                * last_volume["volume"].astype("float64")
            ).mean()
        )

        metrics[str(code)] = {
            "breakout_price": float(last_breakout["high"].max()),
            "volume_ma20": float(last_volume["volume"].mean()),
            "trading_value_ma20": trading_value_ma20,
            "ma20": float(last_ma["close"].mean()),
            "ma19_close_sum": float(last_19["close"].sum()),
            "ma19_count": int(len(last_19)),
            "prev_close": float(group.iloc[-1]["close"]),
            "history_last_date": str(group.iloc[-1]["date"]),
        }

    return metrics


def load_daily_bar(
    code: str,
    trade_date: str,
    db_path: Path = DB_PATH,
) -> dict | None:
    """특정 종목/거래일의 완성 일봉 1건을 반환한다."""
    normalized_code = str(code).strip().upper().zfill(6)
    trade_date = str(trade_date).strip()
    if not normalized_code or not trade_date:
        return None

    with _connect(db_path) as con:
        row = con.execute(
            """
            SELECT code, date, open, high, low, close, volume, source, updated_at
            FROM daily_price
            WHERE code = ? AND date = ?
            LIMIT 1
            """,
            (normalized_code, trade_date),
        ).fetchone()

    if row is None:
        return None

    return {
        "code": str(row[0]),
        "date": str(row[1]),
        "open": int(row[2]),
        "high": int(row[3]),
        "low": int(row[4]),
        "close": int(row[5]),
        "volume": int(row[6]),
        "source": str(row[7]),
        "updated_at": str(row[8]),
    }


def has_completed_daily_bar_after(
    code: str,
    after_date: str,
    before_date: str | None = None,
    db_path: Path = DB_PATH,
) -> bool:
    """after_date 이후에 이미 완성된 일봉이 존재하는지 확인한다.

    before_date가 주어지면 ``after_date < date < before_date`` 범위만 확인한다.
    눌림목 전략에서 '전고점 도달 바로 다음 거래일'이 이미 지나갔는지 판별할 때 사용한다.
    """
    normalized_code = str(code).strip().upper().zfill(6)
    after_date = str(after_date).strip()

    if before_date is None:
        sql = """
            SELECT 1
            FROM daily_price
            WHERE code = ? AND date > ?
            LIMIT 1
        """
        params = (normalized_code, after_date)
    else:
        before_date = str(before_date).strip()
        sql = """
            SELECT 1
            FROM daily_price
            WHERE code = ? AND date > ? AND date < ?
            LIMIT 1
        """
        params = (normalized_code, after_date, before_date)

    with _connect(db_path) as con:
        return con.execute(sql, params).fetchone() is not None


def load_pullback_metrics(
    codes: Iterable[str],
    previous_high_window: int = 60,
    db_path: Path = DB_PATH,
) -> Dict[str, dict]:
    """눌림목 전략이 장중 O(1) 계산에 사용할 전일 기준값을 준비한다.

    오늘 실시간 가격을 포함한 MA5/20/60을 빠르게 계산하기 위해 각각
    직전 4/19/59개 종가 합을 저장한다. 또한 전일 거래량과 직전
    previous_high_window 거래일 최고가를 함께 반환한다.

    시스템 공통 정책에 맞춰 MIN_HISTORY_DAYS(65) 미만 종목은 제외한다.
    """
    normalized = {
        str(code).strip().upper().zfill(6)
        for code in codes
        if str(code).strip()
    }
    if not normalized:
        return {}

    required_rows = max(
        MIN_HISTORY_DAYS,
        int(previous_high_window),
        59,
    )

    # 전체 DB를 전부 읽지 않고 최근 거래일만 읽는다. 종목별 휴장/거래정지로
    # 일부 날짜가 빠질 수 있으므로 필요한 행 수보다 여유 있게 날짜 범위를 잡는다.
    with _connect(db_path) as con:
        recent_dates = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT date FROM daily_price ORDER BY date DESC LIMIT ?",
                (required_rows + 20,),
            ).fetchall()
        ]
        if not recent_dates:
            return {}

        cutoff = min(recent_dates)
        placeholders = ",".join("?" for _ in normalized)
        df = pd.read_sql_query(
            f"""
            SELECT code, date, open, high, low, close, volume
            FROM daily_price
            WHERE date >= ? AND code IN ({placeholders})
            ORDER BY code, date
            """,
            con,
            params=[cutoff, *sorted(normalized)],
        )

    if df.empty:
        return {}

    metrics: Dict[str, dict] = {}
    for code, group in df.groupby("code", sort=False):
        group = group.sort_values("date").copy()
        for col in ["open", "high", "low", "close", "volume"]:
            group[col] = pd.to_numeric(group[col], errors="coerce")
        group = group.dropna(subset=["high", "close", "volume"])

        if len(group) < required_rows:
            continue

        last_4 = group.tail(4)
        last_19 = group.tail(19)
        last_59 = group.tail(59)
        previous_high_rows = group.tail(int(previous_high_window))
        latest = group.iloc[-1]

        if len(last_4) < 4 or len(last_19) < 19 or len(last_59) < 59:
            continue

        metrics[str(code)] = {
            "close_sum_4": float(last_4["close"].sum()),
            "close_sum_19": float(last_19["close"].sum()),
            "close_sum_59": float(last_59["close"].sum()),
            "previous_high": float(previous_high_rows["high"].max()),
            "prev_volume": float(latest["volume"]),
            "prev_close": float(latest["close"]),
            "history_last_date": str(latest["date"]),
            "history_count": int(len(group)),
        }

    return metrics

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
