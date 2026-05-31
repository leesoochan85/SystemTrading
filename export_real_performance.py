"""monitoring.db의 실전 성과 테이블을 CSV로 내보낸다.

사용법:
    python export_real_performance.py
"""
from pathlib import Path
import sqlite3
import pandas as pd

DB_PATH = Path("monitoring.db")
OUT_DIR = Path("real_performance_exports")
TABLES = ["real_trades", "daily_equity", "order_log", "strategy_daily_summary"]


def export_tables(db_path: Path = DB_PATH, out_dir: Path = OUT_DIR) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"실전 성과 DB가 없습니다: {db_path}")
    out_dir.mkdir(exist_ok=True)
    with sqlite3.connect(db_path) as con:
        for table in TABLES:
            df = pd.read_sql_query(f"SELECT * FROM {table}", con)
            output_path = out_dir / f"{table}.csv"
            df.to_csv(output_path, index=False, encoding="utf-8-sig")
            print(f"[저장 완료] {output_path} ({len(df)}행)")


if __name__ == "__main__":
    export_tables()
