import sqlite3
from datetime import datetime


MONITORING_DB = "monitoring.db"
POSITION_DB = "strategy_position.db"
SENT_NEWS_DB = "sent_news.db"

conn = sqlite3.connect("universe_price.db", isolation_level=None)
cur = conn.cursor()
cur.execute('''CREATE TABLE IF NOT EXISTS balance(
                code varchar(6) PRIMARY KEY,
                bid_price int(20) NOT NULL,
                quantity int(20) NOT NULL,
                created_at varchar(14) NOT NULL,
                will_clear_at varchar(14)
            )''')


def _now_text():
    return datetime.now().strftime("%Y%m%d%H%M%S")


def init_position_strategy_table():
    with sqlite3.connect(POSITION_DB) as con:
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

def save_position_strategy(code, code_name, strategy_name, quantity, buy_price):
    init_position_strategy_table()
    with sqlite3.connect(POSITION_DB) as con:
        con.execute("""
            INSERT OR REPLACE INTO position_strategy
            (code, code_name, strategy_name, quantity, buy_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(code).zfill(6), code_name, strategy_name, int(quantity or 0),
            int(buy_price or 0), _now_text()
        ))

def get_position_strategy(code):
    init_position_strategy_table()
    with sqlite3.connect(POSITION_DB) as con:
        row = con.execute("""
            SELECT strategy_name FROM position_strategy WHERE code = ?
        """, (str(code).zfill(6),)).fetchone()
    return None if row is None else row[0]

def get_position_detail(code):
    init_position_strategy_table()
    with sqlite3.connect(POSITION_DB) as con:
        row = con.execute("""
            SELECT code, code_name, strategy_name, quantity, buy_price, created_at
            FROM position_strategy WHERE code = ?
        """, (str(code).zfill(6),)).fetchone()
    if row is None:
        return None
    return {
        "code": row[0], "code_name": row[1], "strategy_name": row[2],
        "quantity": row[3], "buy_price": row[4], "created_at": row[5],
    }

def get_all_position_details():
    """
    strategy_position.db의 전체 전략 포지션 정보를 코드 기준 딕셔너리로 반환한다.

    quantity=0, buy_price=0인 매수 예약 행도 포함한다.
    실제 잔고가 생겼는데 체결 반영이 누락된 경우를 찾기 위함이다.
    """
    init_position_strategy_table()

    with sqlite3.connect(POSITION_DB) as con:
        rows = con.execute("""
            SELECT code, code_name, strategy_name, quantity, buy_price, created_at
            FROM position_strategy
        """).fetchall()

    return {
        str(row[0]).zfill(6): {
            "code": str(row[0]).zfill(6),
            "code_name": row[1],
            "strategy_name": row[2],
            "quantity": int(row[3] or 0),
            "buy_price": float(row[4] or 0),
            "created_at": row[5],
        }
        for row in rows
    }

def delete_position_strategy(code):
    init_position_strategy_table()
    with sqlite3.connect(POSITION_DB) as con:
        con.execute("DELETE FROM position_strategy WHERE code = ?", (str(code).zfill(6),))

def update_position_from_buy_fill(
    code,
    code_name,
    strategy_name,
    fill_quantity,
    fill_price,
):
    """
    실제 매수 체결 수량과 체결가를 기준으로
    position_strategy의 보유수량과 평균매입가를 갱신한다.
    """
    fill_quantity = int(fill_quantity or 0)
    fill_price = float(fill_price or 0)

    if fill_quantity <= 0 or fill_price <= 0:
        return

    init_position_strategy_table()

    normalized_code = str(code).zfill(6)
    filled_at = _now_text()

    with sqlite3.connect(POSITION_DB) as con:
        row = con.execute("""
            SELECT strategy_name, quantity, buy_price, created_at
            FROM position_strategy
            WHERE code = ?
        """, (normalized_code,)).fetchone()

        # 주문 직후 예약 저장이 없었더라도,
        # 전략명이 확인되면 실제 체결 기준으로 새 포지션을 만든다.
        if row is None:
            if not strategy_name:
                print(
                    f"[DB] 매수 체결의 전략명을 확인할 수 없어 "
                    f"포지션 저장을 생략합니다: {normalized_code}"
                )
                return

            con.execute("""
                INSERT INTO position_strategy
                (code, code_name, strategy_name, quantity, buy_price, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                normalized_code,
                code_name,
                strategy_name,
                fill_quantity,
                fill_price,
                filled_at,
            ))
            return

        stored_strategy, old_quantity, old_buy_price, created_at = row

        old_quantity = int(old_quantity or 0)
        old_buy_price = float(old_buy_price or 0)

        total_quantity = old_quantity + fill_quantity

        average_buy_price = (
            old_quantity * old_buy_price
            + fill_quantity * fill_price
        ) / total_quantity

        con.execute("""
            UPDATE position_strategy
            SET code_name = ?,
                strategy_name = ?,
                quantity = ?,
                buy_price = ?,
                created_at = ?
            WHERE code = ?
        """, (
            code_name,
            strategy_name or stored_strategy,
            total_quantity,
            average_buy_price,
            filled_at if old_quantity == 0 else created_at,
            normalized_code,
        ))

def check_table_exists(db_name, table_name):
    with sqlite3.connect(f"{db_name}.db") as con:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' and name=?", (table_name,)
        ).fetchone()
    return row is not None

def insert_df_to_db(db_name, table_name, df, option="replace"):
    with sqlite3.connect(f"{db_name}.db") as con:
        df.to_sql(table_name, con, if_exists=option)

def execute_sql(db_name, sql, params=None):
    con = sqlite3.connect(f"{db_name}.db")
    return con.execute(sql, params or {})

def init_sent_news_table():
    with sqlite3.connect(SENT_NEWS_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS sent_news (
                link TEXT PRIMARY KEY, title TEXT, source TEXT, sent_at TEXT
            )
        """)

def is_news_sent(link):
    init_sent_news_table()
    with sqlite3.connect(SENT_NEWS_DB) as con:
        row = con.execute("SELECT 1 FROM sent_news WHERE link = ? LIMIT 1", (link,)).fetchone()
    return row is not None

def save_sent_news(link, title, source="naver_economy"):
    init_sent_news_table()
    with sqlite3.connect(SENT_NEWS_DB) as con:
        con.execute("""
            INSERT OR IGNORE INTO sent_news (link, title, source, sent_at)
            VALUES (?, ?, ?, ?)
        """, (link, title, source, _now_text()))

def _migrate_old_order_log_if_needed(con):
    columns = con.execute("PRAGMA table_info(order_log)").fetchall()
    if not columns:
        return
    column_names = {row[1] for row in columns}
    if "event_id" in column_names:
        return
    legacy_name = f"order_log_snapshot_legacy_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    con.execute(f"ALTER TABLE order_log RENAME TO {legacy_name}")
    print(f"[DB] 기존 order_log를 {legacy_name}로 보존하고 이벤트 이력 테이블을 생성합니다.")

def _add_column_if_missing(con, table_name, column_name, column_type):
    columns = {
        row[1]
        for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()
    }

    if column_name not in columns:
        con.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
        )
        print(f"[DB] {table_name}.{column_name} 컬럼을 추가했습니다.")

def init_monitoring_tables():
    with sqlite3.connect(MONITORING_DB) as con:
        _migrate_old_order_log_if_needed(con)
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
            row[1]
            for row in con.execute("PRAGMA table_info(real_trades)").fetchall()
        }

        if "sell_order_no" not in real_trade_columns:
            con.execute("""
                ALTER TABLE real_trades
                ADD COLUMN sell_order_no TEXT
            """)

        _add_column_if_missing(
            con,
            "real_trades",
            "gross_realized_pnl",
            "INTEGER",
        )
        _add_column_if_missing(
            con,
            "real_trades",
            "fee",
            "INTEGER",
        )
        _add_column_if_missing(
            con,
            "real_trades",
            "tax",
            "INTEGER",
        )
        _add_column_if_missing(
            con,
            "real_trades",
            "net_realized_pnl",
            "INTEGER",
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
            con,
            "daily_equity",
            "gross_realized_pnl",
            "INTEGER",
        )
        _add_column_if_missing(
            con,
            "daily_equity",
            "net_realized_pnl",
            "INTEGER",
        )
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
        )
        _add_column_if_missing(
            con,
            "strategy_daily_summary",
            "fee",
            "INTEGER",
        )
        _add_column_if_missing(
            con,
            "strategy_daily_summary",
            "tax",
            "INTEGER",
        )
        _add_column_if_missing(
            con,
            "strategy_daily_summary",
            "net_realized_pnl",
            "INTEGER",
        )

def save_order_event(
    event_key, code, code_name, order_type, strategy_name="", order_quantity=0,
    order_price=0, remaining_quantity=0, filled_quantity=0, filled_price=0,
    order_status="", event_type="", order_no="", original_order_no="",
    request_id="", message="",
):
    init_monitoring_tables()
    with sqlite3.connect(MONITORING_DB) as con:
        con.execute("""
            INSERT OR IGNORE INTO order_log (
                event_key, request_id, order_no, original_order_no, code, code_name,
                order_type, strategy_name, order_quantity, order_price,
                remaining_quantity, filled_quantity, filled_price, order_status,
                event_type, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(event_key), str(request_id or ""), str(order_no or ""),
            str(original_order_no or ""), str(code).zfill(6), code_name,
            order_type, strategy_name, int(order_quantity or 0), int(order_price or 0),
            int(remaining_quantity or 0), int(filled_quantity or 0), int(filled_price or 0),
            order_status, event_type, message, _now_text(),
        ))

def save_order_log(order_no, code, code_name, order_type, strategy_name,
                   order_quantity, order_price, remaining_quantity, order_status,
                   filled_quantity=0, filled_price=0, fill_no=""):
    """키움 체잔 응답을 주문 이벤트 이력으로 보존한다."""
    normalized_status = str(order_status or "").strip()
    if "거부" in normalized_status or "실패" in normalized_status:
        event_type = "FAILED"
    elif "취소" in normalized_status or "취소" in str(order_type):
        event_type = "CANCELLED"
    elif int(filled_quantity or 0) > 0:
        event_type = "FILLED" if int(remaining_quantity or 0) == 0 else "PARTIAL_FILLED"
    else:
        event_type = "ACCEPTED"
    event_key = "|".join([
        "BROKER", str(order_no or ""), str(fill_no or ""), event_type,
        str(remaining_quantity or 0), str(filled_quantity or 0), normalized_status,
    ])
    save_order_event(
        event_key=event_key, order_no=order_no, code=code, code_name=code_name,
        order_type=order_type, strategy_name=strategy_name,
        order_quantity=order_quantity, order_price=order_price,
        remaining_quantity=remaining_quantity, filled_quantity=filled_quantity,
        filled_price=filled_price, order_status=normalized_status, event_type=event_type,
    )

def save_trade_fill(
    fill_no,
    order_no,
    code,
    code_name,
    order_type,
    strategy_name,
    quantity,
    price,
    buy_price=None,
    buy_date=None,
    fee=None,
    tax=None,
):
    """
    체결번호 기준으로 체결을 저장한다.

    - realized_pnl / gross_realized_pnl:
      수수료·세금 전 손익
    - fee / tax:
      키움에서 확인된 당일 비용 스냅샷
    - net_realized_pnl:
      수수료·세금 정보가 확인된 경우에만 계산

    주의:
    키움 비용 FID는 당일 누적값일 수 있으므로,
    동일 매도 주문의 분할체결 시 fee/tax를 합산하지 않고
    가장 최근 수신값으로 교체한다.
    """
    if not fill_no or int(quantity or 0) <= 0 or int(price or 0) <= 0:
        return

    init_monitoring_tables()

    now = _now_text()
    gross_realized_pnl = None
    return_pct = None
    buy_price_value = None

    if buy_price is not None and float(buy_price) > 0:
        buy_price_value = float(buy_price)

    if order_type == "매도" and buy_price_value is not None:
        gross_realized_pnl = round(
            (int(price) - buy_price_value) * int(quantity)
        )

        return_pct = (
            (int(price) - buy_price_value)
            / buy_price_value
            * 100
        )

    received_fee = None if fee is None else int(fee or 0)
    received_tax = None if tax is None else int(tax or 0)

    if (
        gross_realized_pnl is not None
        and received_fee is not None
        and received_tax is not None
    ):
        net_realized_pnl = (
            gross_realized_pnl
            - received_fee
            - received_tax
        )
    else:
        net_realized_pnl = None

    with sqlite3.connect(MONITORING_DB) as con:
        cursor = con.execute("""
            INSERT OR IGNORE INTO trade_fill_log (
                fill_no,
                order_no,
                code,
                code_name,
                order_type,
                strategy_name,
                quantity,
                price,
                buy_price,
                realized_pnl,
                filled_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(fill_no),
            str(order_no or ""),
            str(code).zfill(6),
            code_name,
            order_type,
            strategy_name,
            int(quantity),
            int(price),
            buy_price_value,
            gross_realized_pnl,
            now,
        ))

        is_new_fill = cursor.rowcount == 1

        if is_new_fill and order_type == "매도":
            normalized_order_no = str(order_no or "").strip()

            if normalized_order_no:
                trade_id = f"SELL_ORDER_{normalized_order_no}"
            else:
                trade_id = f"SELL_FILL_{fill_no}"

            existing_trade = con.execute("""
                SELECT
                    quantity,
                    sell_price,
                    realized_pnl,
                    gross_realized_pnl,
                    fee,
                    tax
                FROM real_trades
                WHERE trade_id = ?
            """, (trade_id,)).fetchone()

            new_quantity = int(quantity)
            new_sell_price = float(price)

            if existing_trade is None:
                con.execute("""
                    INSERT INTO real_trades (
                        trade_id,
                        code,
                        code_name,
                        strategy_name,
                        sell_order_no,
                        quantity,
                        buy_date,
                        buy_price,
                        sell_date,
                        sell_price,
                        realized_pnl,
                        gross_realized_pnl,
                        fee,
                        tax,
                        net_realized_pnl,
                        return_pct,
                        sell_fill_no,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    str(code).zfill(6),
                    code_name,
                    strategy_name,
                    normalized_order_no,
                    new_quantity,
                    buy_date,
                    buy_price_value,
                    now,
                    new_sell_price,
                    gross_realized_pnl,
                    gross_realized_pnl,
                    received_fee,
                    received_tax,
                    net_realized_pnl,
                    return_pct,
                    str(fill_no),
                    now,
                ))

            else:
                old_quantity = int(existing_trade[0] or 0)
                old_sell_price = float(existing_trade[1] or 0)
                old_gross_realized_pnl = existing_trade[3]
                old_fee = existing_trade[4]
                old_tax = existing_trade[5]

                total_quantity = old_quantity + new_quantity

                average_sell_price = (
                    old_quantity * old_sell_price
                    + new_quantity * new_sell_price
                ) / total_quantity

                if (
                    old_gross_realized_pnl is None
                    or gross_realized_pnl is None
                ):
                    total_gross_realized_pnl = None
                else:
                    total_gross_realized_pnl = (
                        int(old_gross_realized_pnl)
                        + int(gross_realized_pnl)
                    )

                # 비용 FID는 누적 스냅샷일 수 있으므로 더하지 않고
                # 새로 수신한 값이 있으면 최신값으로 교체한다.
                final_fee = (
                    received_fee
                    if received_fee is not None
                    else old_fee
                )
                final_tax = (
                    received_tax
                    if received_tax is not None
                    else old_tax
                )

                if (
                    total_gross_realized_pnl is not None
                    and final_fee is not None
                    and final_tax is not None
                ):
                    total_net_realized_pnl = (
                        int(total_gross_realized_pnl)
                        - int(final_fee)
                        - int(final_tax)
                    )
                else:
                    total_net_realized_pnl = None

                if buy_price_value is not None and buy_price_value > 0:
                    aggregated_return_pct = (
                        (average_sell_price - buy_price_value)
                        / buy_price_value
                        * 100
                    )
                else:
                    aggregated_return_pct = None

                con.execute("""
                    UPDATE real_trades
                    SET quantity = ?,
                        sell_date = ?,
                        sell_price = ?,
                        realized_pnl = ?,
                        gross_realized_pnl = ?,
                        fee = ?,
                        tax = ?,
                        net_realized_pnl = ?,
                        return_pct = ?
                    WHERE trade_id = ?
                """, (
                    total_quantity,
                    now,
                    average_sell_price,
                    total_gross_realized_pnl,
                    total_gross_realized_pnl,
                    final_fee,
                    final_tax,
                    total_net_realized_pnl,
                    aggregated_return_pct,
                    trade_id,
                ))

    if is_new_fill and order_type == "매수":
        update_position_from_buy_fill(
            code=code,
            code_name=code_name,
            strategy_name=strategy_name,
            fill_quantity=quantity,
            fill_price=price,
        )

    if is_new_fill and order_type == "매도":
        reduce_position_from_sell_fill(
            code=code,
            fill_quantity=quantity,
        )

def update_latest_sell_trade_costs(code, fee=None, tax=None):
    """
    잔고 체잔 이벤트로 비용 정보가 늦게 도착했을 때,
    오늘 해당 종목의 가장 최근 매도 거래에 비용을 반영한다.

    주의:
    동일 종목을 같은 날 여러 차례 완전히 매매한 경우에는
    당일 누적 비용을 개별 거래에 정확히 배분할 수 없다.
    """
    if fee is None or tax is None:
        return

    init_monitoring_tables()

    today = datetime.now().strftime("%Y%m%d")
    normalized_code = str(code).zfill(6)
    fee_value = int(fee or 0)
    tax_value = int(tax or 0)

    with sqlite3.connect(MONITORING_DB) as con:
        row = con.execute("""
            SELECT trade_id, gross_realized_pnl
            FROM real_trades
            WHERE code = ?
              AND sell_date LIKE ?
              AND gross_realized_pnl IS NOT NULL
            ORDER BY sell_date DESC
            LIMIT 1
        """, (
            normalized_code,
            f"{today}%",
        )).fetchone()

        if row is None:
            return

        trade_id = row[0]
        gross_realized_pnl = int(row[1])

        net_realized_pnl = (
            gross_realized_pnl
            - fee_value
            - tax_value
        )

        con.execute("""
            UPDATE real_trades
            SET fee = ?,
                tax = ?,
                net_realized_pnl = ?
            WHERE trade_id = ?
        """, (
            fee_value,
            tax_value,
            net_realized_pnl,
            trade_id,
        ))

def get_filled_position_codes():
    """
    실제 매수 체결 이력이 있는 전략 포지션 코드 목록을 반환한다.

    - quantity > 0: 현재 보유 중인 포지션
    - buy_price > 0: 전량 매도 체결 후 삭제 대기 중인 포지션
    - quantity = 0, buy_price = 0: 아직 체결되지 않은 매수 예약 행이므로 제외
    """
    init_position_strategy_table()

    with sqlite3.connect(POSITION_DB) as con:
        rows = con.execute("""
            SELECT code
            FROM position_strategy
            WHERE quantity > 0 OR buy_price > 0
        """).fetchall()

    return [row[0] for row in rows]

def get_strategy_position_counts():
    init_position_strategy_table()

    with sqlite3.connect(POSITION_DB) as con:
        rows = con.execute("""
            SELECT strategy_name, COUNT(*)
            FROM position_strategy
            WHERE quantity > 0
            GROUP BY strategy_name
        """).fetchall()

    return {strategy_name: count for strategy_name, count in rows}

def get_today_order_count():
    today = datetime.now().strftime("%Y%m%d")
    init_monitoring_tables()
    with sqlite3.connect(MONITORING_DB) as con:
        row = con.execute("""
            SELECT COUNT(*) FROM order_log
            WHERE created_at LIKE ? AND event_type = 'REQUESTED'
              AND order_type IN ('매수', '매도')
        """, (f"{today}%",)).fetchone()
    return int(row[0] or 0)

def get_today_realized_pnl():
    """
    오늘의 총손익과 순손익을 반환한다.

    반환값:
    - gross_pnl: 수수료·세금 전 손익
    - net_pnl: 비용이 확인된 거래만 합산한 순손익
    - missing_buy_price_count: 매입가가 없어 총손익도 계산 못한 거래 수
    - missing_cost_count: 총손익은 있으나 비용이 없어 순손익 계산 못한 거래 수
    """
    today = datetime.now().strftime("%Y%m%d")
    init_monitoring_tables()

    with sqlite3.connect(MONITORING_DB) as con:
        row = con.execute("""
            SELECT
                COALESCE(SUM(gross_realized_pnl), 0),
                COALESCE(SUM(net_realized_pnl), 0),
                COALESCE(SUM(
                    CASE WHEN gross_realized_pnl IS NULL THEN 1 ELSE 0 END
                ), 0),
                COALESCE(SUM(
                    CASE
                        WHEN gross_realized_pnl IS NOT NULL
                         AND net_realized_pnl IS NULL
                        THEN 1 ELSE 0
                    END
                ), 0)
            FROM real_trades
            WHERE sell_date LIKE ?
        """, (f"{today}%",)).fetchone()

    return (
        int(row[0] or 0),
        int(row[1] or 0),
        int(row[2] or 0),
        int(row[3] or 0),
    )

def save_daily_equity(deposit, balance):
    init_monitoring_tables()
    today = datetime.now().strftime("%Y%m%d")

    evaluation_amount = 0

    for info in balance.values():
        quantity = int(info.get("보유수량", 0) or 0)
        current_price = int(
            info.get("현재가", 0)
            or info.get("매입가", 0)
            or 0
        )
        evaluation_amount += quantity * current_price

    total_assets = int(deposit or 0) + evaluation_amount

    (
        gross_realized_pnl,
        net_realized_pnl,
        _,
        _,
    ) = get_today_realized_pnl()

    with sqlite3.connect(MONITORING_DB) as con:
        con.execute("""
            INSERT INTO daily_equity (
                date,
                deposit,
                evaluation_amount,
                total_assets,
                realized_pnl,
                gross_realized_pnl,
                net_realized_pnl,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                deposit = excluded.deposit,
                evaluation_amount = excluded.evaluation_amount,
                total_assets = excluded.total_assets,
                realized_pnl = excluded.realized_pnl,
                gross_realized_pnl = excluded.gross_realized_pnl,
                net_realized_pnl = excluded.net_realized_pnl,
                updated_at = excluded.updated_at
        """, (
            today,
            int(deposit or 0),
            evaluation_amount,
            total_assets,
            gross_realized_pnl,
            gross_realized_pnl,
            net_realized_pnl,
            _now_text(),
        ))

    return evaluation_amount, total_assets

def save_strategy_daily_summaries(strategy_names):
    init_monitoring_tables()
    init_position_strategy_table()

    today = datetime.now().strftime("%Y%m%d")
    counts = get_strategy_position_counts()

    with sqlite3.connect(MONITORING_DB) as con:
        for strategy_name in strategy_names:
            row = con.execute("""
                SELECT
                    COALESCE(SUM(gross_realized_pnl), 0),
                    COALESCE(SUM(fee), 0),
                    COALESCE(SUM(tax), 0),
                    COALESCE(SUM(net_realized_pnl), 0),
                    COALESCE(SUM(
                        CASE
                            WHEN buy_price IS NOT NULL
                            THEN buy_price * quantity
                            ELSE 0
                        END
                    ), 0),
                    COUNT(*),
                    COALESCE(SUM(
                        CASE WHEN net_realized_pnl > 0 THEN 1 ELSE 0 END
                    ), 0)
                FROM real_trades
                WHERE sell_date LIKE ?
                  AND strategy_name = ?
            """, (
                f"{today}%",
                strategy_name,
            )).fetchone()

            (
                gross_realized_pnl,
                fee,
                tax,
                net_realized_pnl,
                invested_amount,
                trade_count,
                winning_trades,
            ) = [int(v or 0) for v in row]

            daily_return_pct = (
                net_realized_pnl / invested_amount * 100
                if invested_amount
                else 0.0
            )

            win_rate_pct = (
                winning_trades / trade_count * 100
                if trade_count
                else 0.0
            )

            con.execute("""
                INSERT INTO strategy_daily_summary (
                    date,
                    strategy_name,
                    realized_pnl,
                    gross_realized_pnl,
                    fee,
                    tax,
                    net_realized_pnl,
                    invested_amount,
                    daily_return_pct,
                    trade_count,
                    winning_trades,
                    win_rate_pct,
                    holding_count,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, strategy_name) DO UPDATE SET
                    realized_pnl = excluded.realized_pnl,
                    gross_realized_pnl = excluded.gross_realized_pnl,
                    fee = excluded.fee,
                    tax = excluded.tax,
                    net_realized_pnl = excluded.net_realized_pnl,
                    invested_amount = excluded.invested_amount,
                    daily_return_pct = excluded.daily_return_pct,
                    trade_count = excluded.trade_count,
                    winning_trades = excluded.winning_trades,
                    win_rate_pct = excluded.win_rate_pct,
                    holding_count = excluded.holding_count,
                    updated_at = excluded.updated_at
            """, (
                today,
                strategy_name,
                gross_realized_pnl,
                gross_realized_pnl,
                fee,
                tax,
                net_realized_pnl,
                invested_amount,
                daily_return_pct,
                trade_count,
                winning_trades,
                win_rate_pct,
                counts.get(strategy_name, 0),
                _now_text(),
            ))

def get_last_sent_news_at():
    init_sent_news_table()
    with sqlite3.connect(SENT_NEWS_DB) as con:
        row = con.execute("SELECT sent_at FROM sent_news ORDER BY sent_at DESC LIMIT 1").fetchone()
    return None if row is None else row[0]

def reduce_position_from_sell_fill(code, fill_quantity):
    """
    실제 매도 체결 수량만큼 position_strategy 수량을 감소시킨다.
    전량 매도 후 행 삭제는 StrategyManager가 실제 잔고 확인 후 처리한다.
    """
    fill_quantity = int(fill_quantity or 0)

    if fill_quantity <= 0:
        return

    init_position_strategy_table()

    normalized_code = str(code).zfill(6)

    with sqlite3.connect(POSITION_DB) as con:
        row = con.execute("""
            SELECT quantity
            FROM position_strategy
            WHERE code = ?
        """, (normalized_code,)).fetchone()

        if row is None:
            return

        old_quantity = int(row[0] or 0)
        new_quantity = max(0, old_quantity - fill_quantity)

        con.execute("""
            UPDATE position_strategy
            SET quantity = ?
            WHERE code = ?
        """, (
            new_quantity,
            normalized_code,
        ))