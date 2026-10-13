"""Db schema: SQL persistence with an explicit RepositoryContext."""

import json
import sqlite3


def init_position_strategy_table(*, context):
    with context.connect(context.position_db) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS position_strategy (
                code TEXT PRIMARY KEY,
                code_name TEXT,
                strategy_name TEXT NOT NULL,
                quantity INTEGER,
                buy_price INTEGER,
                created_at TEXT
            )
        """)


def _migrate_old_order_log_if_needed(con, *, context):
    columns = con.execute("PRAGMA table_info(order_log)").fetchall()
    if not columns:
        return
    column_names = {row[1] for row in columns}
    if "event_id" in column_names:
        return
    legacy_name = (
        f"order_log_snapshot_legacy_{context.clock().strftime('%Y%m%d%H%M%S')}"
    )
    con.execute(f"ALTER TABLE order_log RENAME TO {legacy_name}")
    print(
        f"[DB] 기존 order_log를 {legacy_name}로 보존하고 이벤트 이력 테이블을 생성합니다."
    )


def _add_column_if_missing(con, table_name, column_name, column_type, *, context):
    columns = {
        row[1] for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()
    }

    if column_name not in columns:
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
        print(f"[DB] {table_name}.{column_name} 컬럼을 추가했습니다.")


def init_monitoring_tables(*, context):
    with context.connect(context.monitoring_db) as con:
        _migrate_old_order_log_if_needed(con, context=context)
        con.execute("""
            CREATE TABLE IF NOT EXISTS order_log (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL,
                request_id TEXT,
                order_no TEXT,
                original_order_no TEXT,
                code TEXT NOT NULL,
                code_name TEXT,
                order_type TEXT,
                strategy_name TEXT,
                order_quantity INTEGER,
                order_price INTEGER,
                remaining_quantity INTEGER,
                filled_quantity INTEGER,
                filled_price INTEGER,
                order_status TEXT,
                event_type TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_fill_log (
                fill_no TEXT PRIMARY KEY,
                order_no TEXT,
                code TEXT NOT NULL,
                code_name TEXT,
                order_type TEXT,
                strategy_name TEXT,
                quantity INTEGER NOT NULL,
                price INTEGER NOT NULL,
                buy_price INTEGER,
                realized_pnl INTEGER,
                filled_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS real_trades (
                trade_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                code_name TEXT,
                strategy_name TEXT,
                sell_order_no TEXT,
                quantity INTEGER NOT NULL,
                buy_date TEXT,
                buy_price INTEGER,
                sell_date TEXT NOT NULL,
                sell_price INTEGER NOT NULL,
                realized_pnl INTEGER,
                gross_realized_pnl INTEGER,
                fee INTEGER,
                tax INTEGER,
                net_realized_pnl INTEGER,
                return_pct REAL,
                sell_fill_no TEXT UNIQUE,
                created_at TEXT NOT NULL
            )
        """)

        real_trade_columns = {
            row[1] for row in con.execute("PRAGMA table_info(real_trades)").fetchall()
        }

        if "sell_order_no" not in real_trade_columns:
            con.execute("""
                ALTER TABLE real_trades
                ADD COLUMN sell_order_no TEXT
            """)

        _add_column_if_missing(
            con, "real_trades", "gross_realized_pnl", "INTEGER", context=context
        )
        _add_column_if_missing(con, "real_trades", "fee", "INTEGER", context=context)
        _add_column_if_missing(con, "real_trades", "tax", "INTEGER", context=context)
        _add_column_if_missing(
            con, "real_trades", "net_realized_pnl", "INTEGER", context=context
        )

        # 기존 저장 데이터는 수수료·세금 전 손익만 알고 있으므로
        # gross_realized_pnl만 기존 realized_pnl 값으로 이관한다.
        # 비용을 알 수 없는 과거 데이터의 net_realized_pnl은 NULL로 유지한다.
        con.execute("""
            UPDATE real_trades
            SET gross_realized_pnl = realized_pnl
            WHERE gross_realized_pnl IS NULL
              AND realized_pnl IS NOT NULL
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_equity (
                date TEXT PRIMARY KEY,
                deposit INTEGER NOT NULL,
                evaluation_amount INTEGER NOT NULL,
                total_assets INTEGER NOT NULL,
                realized_pnl INTEGER NOT NULL,
                gross_realized_pnl INTEGER,
                net_realized_pnl INTEGER,
                updated_at TEXT NOT NULL
            )
        """)
        _add_column_if_missing(
            con, "daily_equity", "gross_realized_pnl", "INTEGER", context=context
        )
        _add_column_if_missing(
            con, "daily_equity", "net_realized_pnl", "INTEGER", context=context
        )

        # 웹 실시간 보유현황용 최신 스냅샷.
        # 이 테이블은 "현재 상태"만 보존하며, 5초 간격으로 전체 교체한다.
        con.execute("""
            CREATE TABLE IF NOT EXISTS position_snapshot (
                code TEXT PRIMARY KEY,
                code_name TEXT,
                strategy_name TEXT,
                quantity INTEGER NOT NULL,
                available_quantity INTEGER NOT NULL,
                buy_price REAL NOT NULL,
                current_price REAL NOT NULL,
                purchase_amount REAL NOT NULL,
                evaluation_amount REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                return_pct REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_position_snapshot_strategy
            ON position_snapshot(strategy_name)
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS strategy_daily_summary (
                date TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                realized_pnl INTEGER NOT NULL,
                gross_realized_pnl INTEGER,
                fee INTEGER,
                tax INTEGER,
                net_realized_pnl INTEGER,
                invested_amount INTEGER NOT NULL,
                daily_return_pct REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                winning_trades INTEGER NOT NULL,
                win_rate_pct REAL NOT NULL,
                holding_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date, strategy_name)
            )
        """)
        _add_column_if_missing(
            con,
            "strategy_daily_summary",
            "gross_realized_pnl",
            "INTEGER",
            context=context,
        )
        _add_column_if_missing(
            con, "strategy_daily_summary", "fee", "INTEGER", context=context
        )
        _add_column_if_missing(
            con, "strategy_daily_summary", "tax", "INTEGER", context=context
        )
        _add_column_if_missing(
            con,
            "strategy_daily_summary",
            "net_realized_pnl",
            "INTEGER",
            context=context,
        )

        # 전략별 웹 성과곡선.
        #
        # 주의:
        # 전략별 현금 예산을 분리하지 않는 현재 구조에서는
        # 각 전략에 실제 "현금 잔액"을 귀속할 수 없다.
        # 따라서 이 테이블의 return_on_deployed_capital_pct는
        # 누적 실현+미실현 손익 / 실제 누적 배치 매입원금이다.
        # 실제 독립 계좌 NAV 수익률과는 구분한다.
        con.execute("""
            CREATE TABLE IF NOT EXISTS strategy_equity_snapshot (
                snapshot_at TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                strategy_name TEXT NOT NULL,

                holding_count INTEGER NOT NULL DEFAULT 0,

                open_cost_basis REAL NOT NULL DEFAULT 0,
                market_value REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,

                cumulative_gross_realized_pnl REAL NOT NULL DEFAULT 0,
                cumulative_fee REAL NOT NULL DEFAULT 0,
                cumulative_tax REAL NOT NULL DEFAULT 0,
                cumulative_net_realized_pnl REAL NOT NULL DEFAULT 0,

                total_pnl REAL NOT NULL DEFAULT 0,

                lifetime_sold_cost_basis REAL NOT NULL DEFAULT 0,
                deployed_capital REAL NOT NULL DEFAULT 0,
                return_on_deployed_capital_pct REAL NOT NULL DEFAULT 0,

                return_method TEXT NOT NULL DEFAULT 'DEPLOYED_CAPITAL_ROI',

                PRIMARY KEY (
                    strategy_name,
                    snapshot_at
                )
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_strategy_equity_date
            ON strategy_equity_snapshot(
                strategy_name,
                snapshot_date,
                snapshot_at
            )
        """)

        # 웹 전략 분석용: 전략 조건 충족 신호를 주문/체결과 별도로 보존한다.
        # signal_key는 전략+종목+날짜+신호종류+사유코드 단위로 유일하다.
        # 같은 신호가 장중 여러 틱에서 반복되어도 한 행만 유지하고 hit_count만 증가시킨다.
        con.execute("""
            CREATE TABLE IF NOT EXISTS strategy_signal_log (
                signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_key TEXT UNIQUE NOT NULL,
                signal_date TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                code TEXT NOT NULL,
                code_name TEXT,
                signal_type TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                signal_reason TEXT,
                current_price REAL,
                condition_data TEXT,
                order_attempted INTEGER NOT NULL DEFAULT 0,
                order_result TEXT,
                blocked_reason TEXT,
                first_detected_at TEXT NOT NULL,
                last_detected_at TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_strategy_signal_strategy_date
            ON strategy_signal_log(strategy_name, signal_date)
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_strategy_signal_code_date
            ON strategy_signal_log(code, signal_date)
        """)
