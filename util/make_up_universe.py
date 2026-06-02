"""코스피/코스닥 시가총액 상위 종목으로 매매 유니버스를 구성한다.

구성 기준:
- KOSPI 시가총액 순위 1~100위
- KOSDAQ 시가총액 순위 1~100위
- 총 200종목

주의:
- ROE, PER, 거래량, 종목명 필터는 사용하지 않는다.
- 네이버 금융의 시장별 시가총액 순서가 그대로 유니버스 순위가 된다.
"""
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://finance.naver.com/sise/sise_market_sum.naver?sosok="
MARKETS = {
    "0": "KOSPI",
    "1": "KOSDAQ",
}
TOP_N_PER_MARKET = 100
ROWS_PER_PAGE = 50
PAGES_PER_MARKET = TOP_N_PER_MARKET // ROWS_PER_PAGE

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

RAW_OUTPUT_PATH = Path("NaverFinance.xlsx")
UNIVERSE_OUTPUT_PATH = Path("universe.xlsx")


def _clean_text(value):
    return " ".join(str(value).split())


def _to_numeric(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).replace("N/A", "0"),
        errors="coerce",
    )


def crawler(market_code, page):
    """네이버 금융의 시장별 시총 순위 한 페이지를 읽는다."""
    url = f"{BASE_URL}{market_code}&page={page}"
    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")

    table_html = soup.select_one("div.box_type_l table.type_2, div.box_type_1 table.type_2")
    if table_html is None:
        raise RuntimeError(f"시가총액 테이블을 찾지 못했습니다: market={market_code}, page={page}")

    table_columns = [
        th.get_text(" ", strip=True)
        for th in table_html.select("thead th")
    ][1:-1]
    header_data = ["종목코드", "시장", "시장내시총순위"] + table_columns

    rows = []
    for tr in table_html.select("tbody tr"):
        rank_td = tr.select_one("td.no")
        name_a = tr.select_one("a.tltle")
        if rank_td is None or name_a is None:
            continue

        href = name_a.get("href", "")
        if "code=" not in href:
            continue

        stock_code = href.split("code=")[-1].split("&")[0].strip().zfill(6)
        rank_text = _clean_text(rank_td.get_text(" ", strip=True))
        if not rank_text.isdigit():
            continue

        tds = tr.find_all("td")
        data_tds = tds[1 : 1 + len(table_columns)]
        if len(data_tds) != len(table_columns):
            continue

        row = [stock_code, MARKETS[market_code], int(rank_text)]
        row.extend(_clean_text(td.get_text(" ", strip=True)) for td in data_tds)
        rows.append(row)

    return pd.DataFrame(rows, columns=header_data)


def execute_crawler():
    """각 시장 시총 상위 100개씩 총 200개를 수집한다."""
    market_frames = []

    for market_code, market_name in MARKETS.items():
        page_frames = [crawler(market_code, page) for page in range(1, PAGES_PER_MARKET + 1)]
        market_df = pd.concat(page_frames, ignore_index=True)
        market_df = market_df.drop_duplicates(subset=["종목코드"])
        market_df = market_df.sort_values("시장내시총순위").head(TOP_N_PER_MARKET)

        if len(market_df) != TOP_N_PER_MARKET:
            raise RuntimeError(
                f"{market_name} 상위 {TOP_N_PER_MARKET}종목 수집 실패: "
                f"실제 {len(market_df)}종목"
            )

        market_frames.append(market_df)

    universe_df = pd.concat(market_frames, ignore_index=True)
    if len(universe_df) != TOP_N_PER_MARKET * len(MARKETS):
        raise RuntimeError(f"전체 유니버스 종목 수 오류: 실제 {len(universe_df)}종목")

    universe_df.to_excel(RAW_OUTPUT_PATH, index=False)
    return universe_df


def get_universe(return_df=False):
    """전략에서 사용할 KOSPI 100 + KOSDAQ 100 유니버스를 반환한다."""
    df = execute_crawler().copy()

    df["종목코드"] = df["종목코드"].astype(str).str.strip().str.upper().str.zfill(6)
    # KRX 단축코드에는 우선주 등 영문자가 포함된 6자리 코드가 존재할 수 있다.
    # 숫자만 허용하면 시총 상위 종목이 누락되므로 영문/숫자 6자리를 허용한다.
    df = df[df["종목코드"].str.fullmatch(r"[0-9A-Za-z]{6}", na=False)]

    numeric_columns = ["현재가", "거래량", "시가총액", "PER", "ROE"]
    for column in numeric_columns:
        if column in df.columns:
            df[column] = _to_numeric(df[column])

    desired_columns = [
        "종목코드",
        "종목명",
        "시장",
        "시장내시총순위",
        "현재가",
        "거래량",
        "시가총액",
        "PER",
        "ROE",
    ]
    columns_to_save = [column for column in desired_columns if column in df.columns]
    df["시장"] = pd.Categorical(df["시장"], categories=["KOSPI", "KOSDAQ"], ordered=True)
    df = df[columns_to_save].sort_values(["시장", "시장내시총순위"]).reset_index(drop=True)
    df["시장"] = df["시장"].astype(str)

    kospi_count = int((df["시장"] == "KOSPI").sum())
    kosdaq_count = int((df["시장"] == "KOSDAQ").sum())
    if kospi_count != TOP_N_PER_MARKET or kosdaq_count != TOP_N_PER_MARKET:
        raise RuntimeError(
            f"유니버스 구성 오류: KOSPI {kospi_count}개 / KOSDAQ {kosdaq_count}개"
        )

    df.to_excel(UNIVERSE_OUTPUT_PATH, index=False)
    print(f"[유니버스 생성 완료] KOSPI {kospi_count}개 + KOSDAQ {kosdaq_count}개 = {len(df)}개")

    if return_df:
        return df
    return df["종목명"].tolist()


if __name__ == "__main__":
    print("Start!")
    get_universe()
    print("End")
