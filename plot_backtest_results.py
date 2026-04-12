import os
import glob
import pandas as pd
import matplotlib.pyplot as plt


BASE_DIR = "."


plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False


def load_summary(base_dir=BASE_DIR):
    path = os.path.join(base_dir, "backtest_summary.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"파일이 없습니다: {path}")
    return pd.read_csv(path, encoding="utf-8-sig")


def load_equity_files(base_dir=BASE_DIR):
    files = sorted(glob.glob(os.path.join(base_dir, "equity_*.csv")))
    equity_map = {}

    for path in files:
        strategy = os.path.basename(path).replace("equity_", "").replace(".csv", "")
        df = pd.read_csv(path, encoding="utf-8-sig")

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"]).sort_values("date")

        equity_map[strategy] = df

    return equity_map


def load_trade_files(base_dir=BASE_DIR):
    files = sorted(glob.glob(os.path.join(base_dir, "trades_*.csv")))
    trade_map = {}

    for path in files:
        strategy = os.path.basename(path).replace("trades_", "").replace(".csv", "")
        df = pd.read_csv(path, encoding="utf-8-sig")
        trade_map[strategy] = df

    return trade_map


def plot_bar(df, column, title, ylabel, ascending=False):
    plot_df = df.sort_values(column, ascending=ascending)

    plt.figure(figsize=(12, 6))
    plt.bar(plot_df["strategy"], plot_df[column])
    plt.title(title)
    plt.xlabel("전략")
    plt.ylabel(ylabel)
    plt.xticks(rotation=25, ha="right")

    for i, v in enumerate(plot_df[column]):
        if pd.notna(v):
            plt.text(i, v, f"{v:.2f}", ha="center", va="bottom" if v >= 0 else "top", fontsize=9)

    plt.tight_layout()
    plt.show()


def plot_equity_curves(equity_map, summary_df):
    ordered_strategies = summary_df["strategy"].tolist()

    plt.figure(figsize=(13, 7))
    for strategy in ordered_strategies:
        df = equity_map.get(strategy)
        if df is None or df.empty:
            continue
        if "date" not in df.columns or "equity" not in df.columns:
            continue
        plt.plot(df["date"], df["equity"], label=strategy)

    plt.title("전략별 자산곡선")
    plt.xlabel("날짜")
    plt.ylabel("자산")
    plt.legend()
    plt.tight_layout()
    plt.show()


def plot_drawdown_curves(equity_map, strategies):
    plt.figure(figsize=(13, 7))

    for strategy in strategies:
        df = equity_map.get(strategy)
        if df is None or df.empty:
            continue
        if "date" not in df.columns or "equity" not in df.columns:
            continue

        eq = df["equity"]
        running_max = eq.cummax()
        drawdown = (eq / running_max - 1) * 100
        plt.plot(df["date"], drawdown, label=strategy)

    plt.title("전략별 낙폭곡선(%)")
    plt.xlabel("날짜")
    plt.ylabel("낙폭(%)")
    plt.legend()
    plt.tight_layout()
    plt.show()


def plot_return_vs_mdd(summary_df):
    plt.figure(figsize=(10, 7))

    x = summary_df["mdd_pct"].abs()
    y = summary_df["cagr_pct"]

    plt.scatter(x, y)

    for _, row in summary_df.iterrows():
        plt.annotate(
            row["strategy"],
            (abs(row["mdd_pct"]), row["cagr_pct"]),
            textcoords="offset points",
            xytext=(5, 5),
            fontsize=9,
        )

    plt.title("수익률 vs 최대낙폭")
    plt.xlabel("최대낙폭 절대값(%)")
    plt.ylabel("CAGR(%)")
    plt.tight_layout()
    plt.show()


def plot_trade_count_vs_winrate(summary_df):
    plt.figure(figsize=(10, 7))

    x = summary_df["trade_count"]
    y = summary_df["win_rate_pct"]

    plt.scatter(x, y)

    for _, row in summary_df.iterrows():
        plt.annotate(
            row["strategy"],
            (row["trade_count"], row["win_rate_pct"]),
            textcoords="offset points",
            xytext=(5, 5),
            fontsize=9,
        )

    plt.title("거래횟수 vs 승률")
    plt.xlabel("거래횟수")
    plt.ylabel("승률(%)")
    plt.tight_layout()
    plt.show()


def plot_monthly_pnl(trade_map, strategies=None):
    if strategies is None:
        strategies = list(trade_map.keys())

    plt.figure(figsize=(13, 7))

    for strategy in strategies:
        df = trade_map.get(strategy)
        if df is None or df.empty:
            continue
        if "exit_date" not in df.columns or "pnl" not in df.columns:
            continue

        temp = df.copy()
        temp["exit_date"] = pd.to_datetime(temp["exit_date"], errors="coerce")
        temp = temp.dropna(subset=["exit_date"])
        if temp.empty:
            continue

        monthly = temp.groupby(temp["exit_date"].dt.to_period("M"))["pnl"].sum()
        monthly.index = monthly.index.to_timestamp()
        plt.plot(monthly.index, monthly.values, label=strategy)

    plt.title("월별 실현손익")
    plt.xlabel("월")
    plt.ylabel("월별 손익")
    plt.legend()
    plt.tight_layout()
    plt.show()


def main():
    summary_df = load_summary()
    equity_map = load_equity_files()
    trade_map = load_trade_files()

    summary_df = summary_df.sort_values("cagr_pct", ascending=False).reset_index(drop=True)

    print("=== backtest_summary.csv ===")
    print(summary_df)

    plot_bar(summary_df, "final_equity", "전략별 최종자산", "최종자산", ascending=False)
    plot_bar(summary_df, "total_return_pct", "전략별 총수익률(%)", "총수익률(%)", ascending=False)
    plot_bar(summary_df, "cagr_pct", "전략별 CAGR(%)", "CAGR(%)", ascending=False)
    plot_bar(summary_df, "mdd_pct", "전략별 최대낙폭(MDD, %)", "MDD(%)", ascending=True)
    plot_bar(summary_df, "sharpe", "전략별 샤프지수", "Sharpe", ascending=False)
    plot_bar(summary_df, "trade_count", "전략별 거래횟수", "거래횟수", ascending=False)
    plot_bar(summary_df, "win_rate_pct", "전략별 승률(%)", "승률(%)", ascending=False)

    plot_equity_curves(equity_map, summary_df)
    plot_drawdown_curves(equity_map, summary_df["strategy"].tolist())
    plot_return_vs_mdd(summary_df)
    plot_trade_count_vs_winrate(summary_df)
    plot_monthly_pnl(trade_map, summary_df["strategy"].tolist())


if __name__ == "__main__":
    main()