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
    