from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

# =========================
# 설정값
# =========================
START_DATE = "2021-04-12"
END_DATE = "2026-04-11"
INITIAL_CAPITAL = 100_000_000
MAX_POSITIONS = 10
BUY_FEE_RATE = 0.00035
SELL_FEE_RATE = 0.00035
USE_CURRENT_UNIVERSE = True
UNIVERSE_LIMIT = 200
MIN_DATA_LEN = 70

# =========================
# 데이터 로더
# =========================
def load_current_universe_map() -> Dict[str, str]:
    from util.make_up_universe import get_universe

    df = get_universe(return_df=True)
    if df is None or df.empty:
        raise ValueError("get_universe(return_df=True) 결과가 비어 있습니다.")

    required = {"종목코드", "종목명"}
    if not required.issubset(df.columns):
        raise ValueError(f"유니버스 컬럼 누락: {required - set(df.columns)}")

    df = df.copy()
    df["종목코드"] = df["종목코드"].astype(str).str.strip().str.zfill(6)
    df = df[df["종목코드"].str.fullmatch(r"\d{6}", na=False)]
    df = df.drop_duplicates(subset=["종목코드"]).head(UNIVERSE_LIMIT)

    return dict(zip(df["종목코드"], df["종목명"]))

def fetch_ohlcv_5y(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    from pykrx import stock

    df = stock.get_market_ohlcv_by_date(
        start_date.replace("-", ""),
        end_date.replace("-", ""),
        ticker,
    )
    if df is None or df.empty:
        return pd.DataFrame()

    rename_map = {
        "시가": "open",
        "고가": "high",
        "저가": "low",
        "종가": "close",
        "거래량": "volume",
    }
    df = df.rename(columns=rename_map)
    keep_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep_cols].copy()
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]
    return df

# =========================
# 지표
# =========================
def add_rsi_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    diff = out["close"].diff(1)
    up = np.where(diff > 0, diff, 0)
    down = np.where(diff < 0, -diff, 0)  # 기존 실시간 코드와 동일하게 음수 유지
    au = pd.Series(up, index=out.index).rolling(window=14).mean()
    ad = pd.Series(down, index=out.index).rolling(window=14).mean()
    out["rsi14"] = au / (au + ad) * 100
    out["ma20"] = out["close"].rolling(window=20, min_periods=1).mean()
    out["ma60"] = out["close"].rolling(window=60, min_periods=1).mean()
    out["close_14d_ago"] = out["close"].shift(14)
    out["price_diff_14d_pct"] = (out["close"] - out["close_14d_ago"]) / out["close_14d_ago"] * 100
    return out

def add_band_reversion_indicators(df: pd.DataFrame, mfi_period: int = 14) -> pd.DataFrame:
    """BandReversionStrategy.py와 동일한 계산을 유지한다."""
    out = df.copy()
    out["mid20"] = out["close"].rolling(window=20).mean()
    out["std20"] = out["close"].rolling(window=20).std(ddof=0)
    out["upper"] = out["mid20"] + out["std20"] * 2
    out["lower"] = out["mid20"] - out["std20"] * 2

    # 현재 BandReversionStrategy와 동일한 사용자식 %b 유지
    pb_den = out["upper"] - out["mid20"]
    out["percent_b"] = np.where(pb_den != 0, (out["close"] - out["lower"]) / pb_den, np.nan)
    out["band_width"] = np.where(out["mid20"] != 0, (out["upper"] - out["lower"]) / out["mid20"], np.nan)

    out["typical_price"] = (out["high"] + out["low"] + out["close"]) / 3
    out["money_flow"] = out["typical_price"] * out["volume"]
    tp_diff = out["typical_price"].diff()
    out["positive_money_flow"] = np.where(tp_diff > 0, out["money_flow"], 0)
    out["negative_money_flow"] = np.where(tp_diff < 0, out["money_flow"], 0)

    pos_sum = pd.Series(out["positive_money_flow"], index=out.index).rolling(window=mfi_period).sum()
    neg_sum = pd.Series(out["negative_money_flow"], index=out.index).rolling(window=mfi_period).sum()

    money_ratio = np.where(neg_sum != 0, pos_sum / neg_sum, np.nan)
    out["mfi"] = 100 - (100 / (1 + money_ratio))
    out.loc[(neg_sum == 0) & (pos_sum > 0), "mfi"] = 100
    out.loc[(neg_sum == 0) & (pos_sum == 0), "mfi"] = 50

    hl_diff = out["high"] - out["low"]
    out["intraday_intensity"] = np.where(
        hl_diff != 0,
        ((2 * out["close"] - out["high"] - out["low"]) / hl_diff) * out["volume"],
        0,
    )
    ii_sum = out["intraday_intensity"].rolling(window=21).sum()
    vol_sum = out["volume"].rolling(window=21).sum()
    out["iip21"] = np.where(vol_sum != 0, (ii_sum / vol_sum) * 100, np.nan)
    return out

def add_band_trend_indicators(df: pd.DataFrame, mfi_period: int = 14) -> pd.DataFrame:
    out = df.copy()
    out["mid20"] = out["close"].rolling(window=20).mean()
    out["std20"] = out["close"].rolling(window=20).std(ddof=0)
    out["upper"] = out["mid20"] + out["std20"] * 2
    out["lower"] = out["mid20"] - out["std20"] * 2

    # BandTrendStrategy.py와 동일한 표준 %B
    pb_den = out["upper"] - out["lower"]
    out["trend_percent_b"] = np.where(
        pb_den != 0,
        (out["close"] - out["lower"]) / pb_den,
        np.nan,
    )

    out["band_width"] = np.where(
        out["mid20"] != 0,
        (out["upper"] - out["lower"]) / out["mid20"],
        np.nan,
    )

    out["typical_price"] = (out["high"] + out["low"] + out["close"]) / 3
    out["money_flow"] = out["typical_price"] * out["volume"]
    tp_diff = out["typical_price"].diff()
    out["positive_money_flow"] = np.where(tp_diff > 0, out["money_flow"], 0)
    out["negative_money_flow"] = np.where(tp_diff < 0, out["money_flow"], 0)

    pos_sum = pd.Series(out["positive_money_flow"], index=out.index).rolling(window=mfi_period).sum()
    neg_sum = pd.Series(out["negative_money_flow"], index=out.index).rolling(window=mfi_period).sum()
    money_ratio = np.where(neg_sum != 0, pos_sum / neg_sum, np.nan)
    out["trend_mfi"] = 100 - (100 / (1 + money_ratio))
    out.loc[(neg_sum == 0) & (pos_sum > 0), "trend_mfi"] = 100
    out.loc[(neg_sum == 0) & (pos_sum == 0), "trend_mfi"] = 50
    return out

# =========================
# 시그널
# =========================
def rsi_buy(row: pd.Series) -> bool:
    vals = [row.get("ma20"), row.get("ma60"), row.get("rsi2"), row.get("price_diff_2d_pct")]
    if any(pd.isna(v) for v in vals):
        return False
    return row["ma20"] > row["ma60"] and row["rsi2"] < 20 and row["price_diff_2d_pct"] < -2

def rsi_sell(row: pd.Series, entry_price: float) -> bool:
    rsi = row.get("rsi2")
    close = row.get("close")
    if pd.isna(rsi) or pd.isna(close):
        return False
    return rsi > 80 and close > entry_price

def band_reversion_buy(row: pd.Series) -> bool:
    pb = row.get("percent_b")
    iip = row.get("iip21")
    if pd.isna(pb) or pd.isna(iip):
        return False
    return pb < 0.05 and iip > 0

def band_reversion_sell(row: pd.Series, entry_price: float) -> bool:
    pb = row.get("percent_b")
    iip = row.get("iip21")
    if pd.isna(pb) or pd.isna(iip):
        return False
    return pb > 0.95 and iip < 0

def band_trend_buy(row: pd.Series) -> bool:
    pb = row.get("trend_percent_b")
    mfi = row.get("trend_mfi")
    if pd.isna(pb) or pd.isna(mfi):
        return False
    return pb > 0.8 and mfi > 80

def band_trend_sell(row: pd.Series, entry_price: float) -> bool:
    pb = row.get("trend_percent_b")
    mfi = row.get("trend_mfi")
    if pd.isna(pb) or pd.isna(mfi):
        return False
    return pb < 0.2 and mfi < 20

BUY_FUNCS: Dict[str, Callable[[pd.Series], bool]] = {
    "RSI": rsi_buy,
    "BAND_REVERSION": band_reversion_buy,
    "BAND_TREND": band_trend_buy,

}

SELL_FUNCS: Dict[str, Callable[[pd.Series, float], bool]] = {
    "RSI": rsi_sell,
    "BAND_REVERSION": band_reversion_sell,
     "BAND_TREND": band_trend_sell,
}


# =========================
# 백테스트 엔진
# =========================
@dataclass
class Position:
    ticker: str
    shares: int
    entry_price: float
    entry_date: pd.Timestamp
    strategy_label: str

@dataclass
class Trade:
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    return_pct: float
    strategy_label: str

class Backtester:
    def __init__(self, data_map: Dict[str, pd.DataFrame], initial_capital: float = INITIAL_CAPITAL):
        self.data_map = data_map
        self.initial_capital = initial_capital

    def run(self, active_strategies: Sequence[str]) -> Tuple[pd.DataFrame, List[Trade], Dict[str, float]]:
        all_dates = sorted(set().union(*[set(df.index) for df in self.data_map.values() if not df.empty]))
        cash = float(self.initial_capital)
        positions: Dict[str, Position] = {}
        trades: List[Trade] = []
        daily_rows = []

        strategy_label = "+".join(active_strategies)

        for i in range(len(all_dates) - 1):
            date = all_dates[i]
            next_date = all_dates[i + 1]

            # 1) 청산: 당일 신호 -> 다음날 시가
            to_sell: List[str] = []
            for ticker, pos in list(positions.items()):
                df = self.data_map.get(ticker)
                if df is None or date not in df.index or next_date not in df.index:
                    continue
                row = df.loc[date]
                if any(SELL_FUNCS[s](row, pos.entry_price) for s in active_strategies):
                    to_sell.append(ticker)

            for ticker in to_sell:
                pos = positions.pop(ticker)
                next_open = float(self.data_map[ticker].loc[next_date, "open"])
                gross = pos.shares * next_open
                net = gross * (1 - SELL_FEE_RATE)
                cash += net
                pnl = net - (pos.shares * pos.entry_price * (1 + BUY_FEE_RATE))
                ret = (next_open - pos.entry_price) / pos.entry_price * 100
                trades.append(
                    Trade(
                        ticker=ticker,
                        entry_date=pos.entry_date,
                        exit_date=next_date,
                        entry_price=pos.entry_price,
                        exit_price=next_open,
                        shares=pos.shares,
                        pnl=pnl,
                        return_pct=ret,
                        strategy_label=strategy_label,
                    )
                )

            # 2) 진입: 당일 신호 -> 다음날 시가
            free_slots = MAX_POSITIONS - len(positions)
            if free_slots > 0:
                buy_candidates = []
                for ticker, df in self.data_map.items():
                    if ticker in positions or date not in df.index or next_date not in df.index:
                        continue
                    row = df.loc[date]
                    if any(BUY_FUNCS[s](row) for s in active_strategies):
                        buy_candidates.append(ticker)

                if buy_candidates:
                    buy_candidates = sorted(buy_candidates)
                    max_new = min(free_slots, len(buy_candidates))
                    alloc_per_trade = cash / max_new if max_new > 0 else 0

                    for ticker in buy_candidates[:max_new]:
                        next_open = float(self.data_map[ticker].loc[next_date, "open"])
                        if next_open <= 0:
                            continue
                        shares = math.floor(alloc_per_trade / (next_open * (1 + BUY_FEE_RATE)))
                        if shares < 1:
                            continue
                        cost = shares * next_open * (1 + BUY_FEE_RATE)
                        if cost > cash:
                            continue
                        cash -= cost
                        positions[ticker] = Position(
                            ticker=ticker,
                            shares=shares,
                            entry_price=next_open,
                            entry_date=next_date,
                            strategy_label=strategy_label,
                        )

            # 3) 일별 평가
            market_value = 0.0
            for ticker, pos in positions.items():
                df = self.data_map[ticker]
                if date in df.index:
                    market_value += pos.shares * float(df.loc[date, "close"])
            equity = cash + market_value
            daily_rows.append({"date": date, "cash": cash, "market_value": market_value, "equity": equity})

        if not daily_rows:
            equity_df = pd.DataFrame(columns=["cash", "market_value", "equity"])
            equity_df.index.name = "date"
            metrics = compute_metrics(pd.Series(dtype=float), trades, self.initial_capital)
            return equity_df, trades, metrics

        equity_df = pd.DataFrame(daily_rows).set_index("date")
        metrics = compute_metrics(equity_df["equity"], trades, self.initial_capital)
        return equity_df, trades, metrics

# =========================
# 성과지표
# =========================
def compute_metrics(equity: pd.Series, trades: Sequence[Trade], initial_capital: float) -> Dict[str, float]:
    if equity.empty:
        return {
            "final_equity": initial_capital,
            "total_return_pct": 0.0,
            "cagr_pct": 0.0,
            "mdd_pct": 0.0,
            "daily_vol_pct": 0.0,
            "sharpe": 0.0,
            "trade_count": 0.0,
            "win_rate_pct": 0.0,
        }

    daily_ret = equity.pct_change().fillna(0)
    total_return = equity.iloc[-1] / initial_capital - 1
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    cagr = (equity.iloc[-1] / initial_capital) ** (1 / years) - 1 if equity.iloc[-1] > 0 else -1
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    mdd = drawdown.min()
    daily_vol = daily_ret.std() * np.sqrt(252)
    sharpe = 0.0
    if daily_ret.std() > 0:
        sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(252)

    win_rate = 0.0
    if trades:
        win_rate = sum(1 for t in trades if t.pnl > 0) / len(trades)

    return {
        "final_equity": float(equity.iloc[-1]),
        "total_return_pct": float(total_return * 100),
        "cagr_pct": float(cagr * 100),
        "mdd_pct": float(mdd * 100),
        "daily_vol_pct": float(daily_vol * 100),
        "sharpe": float(sharpe),
        "trade_count": float(len(trades)),
        "win_rate_pct": float(win_rate * 100),
    }

# =========================
# 준비
# =========================
def build_data_map(ticker_name_map: Dict[str, str], start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
    data_map: Dict[str, pd.DataFrame] = {}
    for idx, (ticker, name) in enumerate(ticker_name_map.items(), start=1):
        print(f"[{idx}/{len(ticker_name_map)}] {ticker} {name} 다운로드")
        try:
            df = fetch_ohlcv_5y(ticker, start_date, end_date)
            if df.empty or len(df) < MIN_DATA_LEN:
                continue
            df = add_rsi_indicators(df)
            df = add_band_reversion_indicators(df)
            df = add_band_trend_indicators(df)
            df["ticker"] = ticker
            df["name"] = name
            data_map[ticker] = df
        except Exception as e:
            print(f"[WARN] {ticker} {name} 실패: {e}")
    return data_map

def build_strategy_sets() -> List[Tuple[str, Tuple[str, ...]]]:
    return [
        ("RSI", ("RSI",)),
        ("BAND_REVERSION", ("BAND_REVERSION",)),
        ("BAND_TREND", ("BAND_TREND",)),
        ("RSI+BAND_REVERSION", ("RSI", "BAND_REVERSION")),
    ]

def main() -> None:
    print("=== 5년 백테스트 시작 ===")
    print("주의: 현재 get_universe()를 사용하면 생존편향(survivorship bias)이 있습니다.")

    if not USE_CURRENT_UNIVERSE:
        raise ValueError("현재 스크립트는 USE_CURRENT_UNIVERSE=True 기준으로 작성되었습니다.")

    ticker_name_map = load_current_universe_map()
    print(f"유니버스 종목 수: {len(ticker_name_map)}")

    data_map = build_data_map(ticker_name_map, START_DATE, END_DATE)
    print(f"가격 데이터 확보 종목 수: {len(data_map)}")

    tester = Backtester(data_map, initial_capital=INITIAL_CAPITAL)
    results = []

    for label, strategies in build_strategy_sets():
        print(f"\n=== 실행: {label} ===")
        equity_df, trades, metrics = tester.run(strategies)

        equity_df.to_csv(f"equity_{label}.csv", encoding="utf-8-sig")
        pd.DataFrame([t.__dict__ for t in trades]).to_csv(
            f"trades_{label}.csv", index=False, encoding="utf-8-sig"
        )
        results.append({"strategy": label, **metrics})

    result_df = pd.DataFrame(results).sort_values(by="cagr_pct", ascending=False)
    result_df.to_csv("backtest_summary.csv", index=False, encoding="utf-8-sig")
    print("\n=== 백테스트 요약 ===")
    print(result_df)
    print("\n파일 저장 완료: backtest_summary.csv, equity_*.csv, trades_*.csv")

if __name__ == "__main__":
    main()
