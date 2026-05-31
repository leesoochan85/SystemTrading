import sqlite3
from datetime import datetime
conn = sqlite3.connect('universe_price.db',isolation_level=None)
cur = conn.cursor()

cur.execute('''CREATE TABLE IF NOT EXISTS balance(
                code varchar(6) PRIMARY KEY,
                bid_price int(20) NOT NULL,
                quantity int(20) NOT NULL,
                created_at varchar(14) NOT NULL,
                will_clear_at varchar(14)
            )''')

def init_position_strategy_table():
    with sqlite3.connect("strategy_position.db") as con:
        cur = con.cursor()
        cur.execute("""
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
    with sqlite3.connect("strategy_position.db") as con:
        cur = con.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO position_strategy
            (code, code_name, strategy_name, quantity, buy_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(code).zfill(6),
            code_name,
            strategy_name,
            quantity,
            buy_price,
            datetime.now().strftime("%Y%m%d%H%M%S")
        ))

def get_position_strategy(code):
    with sqlite3.connect("strategy_position.db") as con:
        cur = con.cursor()
        cur.execute("""
            SELECT strategy_name
            FROM position_strategy
            WHERE code = ?
        """, (str(code).zfill(6),))
        row = cur.fetchone()

    if row is None:
        return None

    return row[0]

def delete_position_strategy(code):
    with sqlite3.connect("strategy_position.db") as con:
        cur = con.cursor()
        cur.execute("""
            DELETE FROM position_strategy
            WHERE code = ?
        """, (str(code).zfill(6),))

def check_table_exists(db_name, table_name):
    with sqlite3.connect('{}.db'.format(db_name)) as con:
        cur = con.cursor()
        sql = "SELECT name FROM sqlite_master WHERE type= 'table' and name=:table_name"
        cur.execute(sql, {"table_name": table_name})

        if len(cur.fetchall()) > 0:
            return True
        else:
            return False

def insert_df_to_db(db_name, table_name, df, option="replace"):
    with sqlite3.connect('{}.db'.format(db_name)) as con:
        df.to_sql(table_name, con, if_exists=option)

def execute_sql(db_name, sql, params={}):
    with sqlite3.connect('{}.db'.format(db_name)) as con:
        cur = con.cursor()
        cur.execute(sql, params)
        return cur
    
def init_sent_news_table():
    with sqlite3.connect("sent_news.db") as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sent_news (
                link TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                sent_at TEXT
            )
        """)


def is_news_sent(link):
    with sqlite3.connect("sent_news.db") as con:
        cur = con.cursor()
        cur.execute("""
            SELECT 1
            FROM sent_news
            WHERE link = ?
            LIMIT 1
        """, (link,))
        row = cur.fetchone()

    return row is not None


def save_sent_news(link, title, source="naver_economy"):
    with sqlite3.connect("sent_news.db") as con:
        cur = con.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO sent_news
            (link, title, source, sent_at)
            VALUES (?, ?, ?, ?)
        """, (
            link,
            title,
            source,
            datetime.now().strftime("%Y%m%d%H%M%S")
        ))
    
def init_monitoring_tables():
    with sqlite3.connect("monitoring.db") as con:
        cur = con.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_log (
                order_no TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                code_name TEXT,
                order_type TEXT,
                strategy_name TEXT,
                order_quantity INTEGER,
                order_price INTEGER,
                remaining_quantity INTEGER,
                order_status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cur.execute("""
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


def get_position_detail(code):
    with sqlite3.connect("strategy_position.db") as con:
        cur = con.cursor()
        cur.execute("""
            SELECT code, code_name, strategy_name, quantity, buy_price, created_at
            FROM position_strategy
            WHERE code = ?
        """, (str(code).zfill(6),))
        row = cur.fetchone()

    if row is None:
        return None

    return {
        "code": row[0],
        "code_name": row[1],
        "strategy_name": row[2],
        "quantity": row[3],
        "buy_price": row[4],
        "created_at": row[5],
    }


def save_order_log(
    order_no,
    code,
    code_name,
    order_type,
    strategy_name,
    order_quantity,
    order_price,
    remaining_quantity,
    order_status,
):
    if not order_no:
        return

    now = datetime.now().strftime("%Y%m%d%H%M%S")

    with sqlite3.connect("monitoring.db") as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO order_log (
                order_no,
                code,
                code_name,
                order_type,
                strategy_name,
                order_quantity,
                order_price,
                remaining_quantity,
                order_status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_no) DO UPDATE SET
                code_name = excluded.code_name,
                order_type = excluded.order_type,
                strategy_name = excluded.strategy_name,
                order_quantity = excluded.order_quantity,
                order_price = excluded.order_price,
                remaining_quantity = excluded.remaining_quantity,
                order_status = excluded.order_status,
                updated_at = excluded.updated_at
        """, (
            str(order_no),
            str(code).zfill(6),
            code_name,
            order_type,
            strategy_name,
            int(order_quantity or 0),
            int(order_price or 0),
            int(remaining_quantity or 0),
            order_status,
            now,
            now,
        ))


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
):
    """
    체결번호 기준으로 INSERT OR IGNORE 처리한다.
    같은 체결 이벤트가 여러 번 들어와도 손익이 중복 계산되지 않는다.
    """
    if not fill_no or int(quantity or 0) <= 0 or int(price or 0) <= 0:
        return

    realized_pnl = None

    if order_type == "매도" and buy_price is not None and int(buy_price) > 0:
        realized_pnl = (int(price) - int(buy_price)) * int(quantity)

    with sqlite3.connect("monitoring.db") as con:
        cur = con.cursor()
        cur.execute("""
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
            int(buy_price) if buy_price is not None else None,
            realized_pnl,
            datetime.now().strftime("%Y%m%d%H%M%S"),
        ))


def get_strategy_position_counts():
    with sqlite3.connect("strategy_position.db") as con:
        cur = con.cursor()
        cur.execute("""
            SELECT strategy_name, COUNT(*)
            FROM position_strategy
            GROUP BY strategy_name
        """)
        rows = cur.fetchall()

    return {strategy_name: count for strategy_name, count in rows}


def get_today_order_count():
    today = datetime.now().strftime("%Y%m%d")

    with sqlite3.connect("monitoring.db") as con:
        cur = con.cursor()
        cur.execute("""
            SELECT COUNT(*)
            FROM order_log
            WHERE created_at LIKE ?
              AND order_type IN ('매수', '매도')
        """, (f"{today}%",))
        row = cur.fetchone()

    return int(row[0] or 0)


def get_today_realized_pnl():
    today = datetime.now().strftime("%Y%m%d")

    with sqlite3.connect("monitoring.db") as con:
        cur = con.cursor()
        cur.execute("""
            SELECT
                COALESCE(SUM(realized_pnl), 0),
                SUM(
                    CASE
                        WHEN order_type = '매도' AND realized_pnl IS NULL
                        THEN 1
                        ELSE 0
                    END
                )
            FROM trade_fill_log
            WHERE filled_at LIKE ?
              AND order_type = '매도'
        """, (f"{today}%",))
        row = cur.fetchone()

    pnl = int(row[0] or 0)
    excluded_count = int(row[1] or 0)

    return pnl, excluded_count


def get_last_sent_news_at():
    with sqlite3.connect("sent_news.db") as con:
        cur = con.cursor()
        cur.execute("""
            SELECT sent_at
            FROM sent_news
            ORDER BY sent_at DESC
            LIMIT 1
        """)
        row = cur.fetchone()

    if row is None:
        return None

    return row[0]