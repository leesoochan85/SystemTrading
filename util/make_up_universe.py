"""네이버 증권의 KOSPI/KOSDAQ 시가총액 데이터를 이용해 매매 유니버스를 구성한다.

구성 기준:
- KOSPI와 KOSDAQ을 합친 시가총액 상위 100종목
- 시장별 상위 100종목씩만 조회한 뒤 통합 정렬한다.
  (어느 시장에서 101위 이하인 종목은 이미 같은 시장 종목 100개보다 시총이 작으므로
   전체 통합 상위 100위에 들어갈 수 없다.)

주의:
- 새 네이버 증권(stock.naver.com)은 화면을 JavaScript로 구성하므로 HTML 테이블을 직접
  파싱하지 않고, 화면에서 사용하는 JSON API 계열을 호출한다.
- ROE, PER, 거래량, 종목명 필터는 사용하지 않는다.
- 네이버의 비공식 웹 API이므로 응답 필드나 URL이 바뀔 가능성이 있다.
"""
from pathlib import Path
import re

import pandas as pd
import requests

NAVER_STOCK_PAGE_URL = "https://stock.naver.com/market/stock/kr/stocklist/capitalization"
API_BASE_URL = "https://m.stock.naver.com/api/stocks/marketValue/"
MARKETS = ("KOSPI", "KOSDAQ")
TOP_N = 100
FETCH_SIZE_PER_MARKET = 100
REQUEST_TIMEOUT = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Referer": NAVER_STOCK_PAGE_URL,
    "Accept": "application/json, text/plain, */*",
}

RAW_OUTPUT_PATH = Path("NaverFinance.xlsx")
UNIVERSE_OUTPUT_PATH = Path("universe.xlsx")


def _clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def _to_number(value):
    """콤마/기호가 섞인 숫자 문자열을 float로 변환한다."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value

    text = _clean_text(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None

    number = float(match.group())
    return int(number) if number.is_integer() else number


def _market_value_to_eok(value):
    """네이버 시가총액 표시값을 비교 가능한 '억원' 단위 숫자로 정규화한다.

    예:
    - '428조 4,893억' -> 4,284,893
    - '35조' -> 350,000
    - '12,345억' -> 12,345
    - 12345 -> 12,345

    API가 숫자 타입을 반환하면 해당 값 자체를 비교값으로 사용한다.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = _clean_text(value).replace(",", "")

    jo_match = re.search(r"(-?\d+(?:\.\d+)?)\s*조", text)
    eok_match = re.search(r"(-?\d+(?:\.\d+)?)\s*억", text)

    if jo_match or eok_match:
        jo = float(jo_match.group(1)) if jo_match else 0.0
        eok = float(eok_match.group(1)) if eok_match else 0.0
        return jo * 10000 + eok

    return _to_number(text)


def _pick(data, *keys, default=None):
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _normalize_stock(stock, market, market_rank):
    """네이버 JSON 한 종목을 프로그램 표준 컬럼으로 변환한다."""
    code = _clean_text(
        _pick(stock, "itemCode", "symbolCode", "code", "stockCode", default="")
    ).upper()
    code = code.zfill(6)

    name = _clean_text(_pick(stock, "stockName", "name", default=""))
    market_value_raw = _pick(
        stock,
        "marketValue",
        "marketCap",
        "marketValueHangeul",
        default=None,
    )

    if not code or not name or market_value_raw in (None, ""):
        return None

    return {
        "종목코드": code,
        "종목명": name,
        "시장": market,
        "시장내시총순위": int(market_rank),
        "현재가": _to_number(_pick(stock, "closePrice", "currentPrice")),
        "거래량": _to_number(
            _pick(stock, "accumulatedTradingVolume", "tradingVolume", "volume")
        ),
        "시가총액": _clean_text(market_value_raw),
        "시가총액_정렬값": _market_value_to_eok(market_value_raw),
        "PER": _to_number(_pick(stock, "per")),
        "ROE": _to_number(_pick(stock, "roe")),
    }


def _request_market_json(market, page=1, page_size=FETCH_SIZE_PER_MARKET):
    """네이버 증권의 시장별 시가총액 JSON을 요청한다."""
    url = f"{API_BASE_URL}{market}"
    response = requests.get(
        url,
        params={"page": page, "pageSize": page_size},
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"네이버 시가총액 API가 JSON을 반환하지 않았습니다: {response.url}"
        ) from exc

    return data


def crawler(market, page=1, page_size=FETCH_SIZE_PER_MARKET):
    """KOSPI 또는 KOSDAQ 시가총액 상위 종목을 DataFrame으로 반환한다."""
    if market not in MARKETS:
        raise ValueError(f"지원하지 않는 시장입니다: {market}")

    data = _request_market_json(market, page=page, page_size=page_size)

    if isinstance(data, dict):
        stocks = data.get("stocks") or data.get("items") or data.get("result")
        if isinstance(stocks, dict):
            stocks = stocks.get("stocks") or stocks.get("items") or stocks.get("list")
    elif isinstance(data, list):
        stocks = data
    else:
        stocks = None

    if not isinstance(stocks, list):
        raise RuntimeError(
            f"네이버 시가총액 API 응답에서 종목 목록을 찾지 못했습니다: market={market}"
        )

    rows = []
    for rank, stock in enumerate(stocks, start=1):
        if not isinstance(stock, dict):
            continue
        normalized = _normalize_stock(stock, market, rank)
        if normalized is not None:
            rows.append(normalized)

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"{market} 시가총액 종목을 한 건도 가져오지 못했습니다.")

    df = df.drop_duplicates(subset=["종목코드"])
    df = df.head(page_size).reset_index(drop=True)
    df["시장내시총순위"] = range(1, len(df) + 1)
    return df


def execute_crawler():
    """KOSPI/KOSDAQ 후보를 합쳐 시가총액 통합 상위 100종목을 만든다."""
    market_frames = []

    for market in MARKETS:
        market_df = crawler(market, page=1, page_size=FETCH_SIZE_PER_MARKET)
        if len(market_df) < FETCH_SIZE_PER_MARKET:
            raise RuntimeError(
                f"{market} 시가총액 상위 {FETCH_SIZE_PER_MARKET}종목 수집 실패: "
                f"실제 {len(market_df)}종목"
            )
        market_frames.append(market_df)

    candidates = pd.concat(market_frames, ignore_index=True)
    candidates = candidates.drop_duplicates(subset=["종목코드"])

    if candidates["시가총액_정렬값"].isna().any():
        bad = candidates.loc[
            candidates["시가총액_정렬값"].isna(),
            ["종목코드", "종목명", "시가총액"],
        ]
        raise RuntimeError(
            "시가총액 숫자 변환에 실패한 종목이 있습니다. "
            f"네이버 응답 형식을 확인하세요: {bad.to_dict('records')[:5]}"
        )

    universe_df = (
        candidates.sort_values(
            ["시가총액_정렬값", "시장", "시장내시총순위"],
            ascending=[False, True, True],
        )
        .head(TOP_N)
        .reset_index(drop=True)
    )
    universe_df.insert(3, "통합시총순위", range(1, len(universe_df) + 1))

    if len(universe_df) != TOP_N:
        raise RuntimeError(
            f"통합 시가총액 상위 {TOP_N}종목 구성 실패: 실제 {len(universe_df)}종목"
        )

    # 수집 원본 확인용: 두 시장 후보 200개를 저장한다.
    raw_df = candidates.sort_values(
        ["시장", "시장내시총순위"], ascending=[True, True]
    ).reset_index(drop=True)
    raw_df.to_excel(RAW_OUTPUT_PATH, index=False)

    return universe_df


def get_universe(return_df=False):
    """전략에서 사용할 KOSPI+KOSDAQ 통합 시총 상위 100 유니버스를 반환한다."""
    df = execute_crawler().copy()

    df["종목코드"] = (
        df["종목코드"].astype(str).str.strip().str.upper().str.zfill(6)
    )
    # KRX 단축코드에는 우선주 등 영문자가 포함된 6자리 코드가 존재할 수 있다.
    df = df[df["종목코드"].str.fullmatch(r"[0-9A-Za-z]{6}", na=False)]
    df = df.drop_duplicates(subset=["종목코드"])

    if len(df) != TOP_N:
        raise RuntimeError(
            f"종목코드 정규화 후 유니버스 수가 {TOP_N}개가 아닙니다: {len(df)}개"
        )

    desired_columns = [
        "종목코드",
        "종목명",
        "시장",
        "통합시총순위",
        "시장내시총순위",
        "현재가",
        "거래량",
        "시가총액",
        "PER",
        "ROE",
    ]
    columns_to_save = [column for column in desired_columns if column in df.columns]
    df = df[columns_to_save].sort_values("통합시총순위").reset_index(drop=True)

    kospi_count = int((df["시장"] == "KOSPI").sum())
    kosdaq_count = int((df["시장"] == "KOSDAQ").sum())

    df.to_excel(UNIVERSE_OUTPUT_PATH, index=False)
    print(
        f"[유니버스 생성 완료] 통합 시총 상위 {len(df)}개 "
        f"(KOSPI {kospi_count}개 / KOSDAQ {kosdaq_count}개)"
    )

    if return_df:
        return df
    return df["종목명"].tolist()


if __name__ == "__main__":
    print("Start!")
    get_universe()
    print("End")
