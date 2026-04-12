import sqlite3
conn = sqlite3.connect('universe_price.db',isolation_level=None)
cur = conn.cursor()

cur.execute('''CREATE TABLE IF NOT EXISTS balance(
                code varchar(6) PRIMARY KEY,
                bid_price int(20) NOT NULL,
                quantity int(20) NOT NULL,
                created_at varchar(14) NOT NULL,
                will_clear_at varchar(14)
            )''')

# sql = "select * from balance where code= :code"
# cur.execute(sql, {"code": "007700"})
# row = cur.fetchone()
# print(row)

# cur.execute('select * from balance')
# rows = cur.fetchall()
# # print(rows)
# for row in rows:
#     # print(row)
#     code, bid_price, quantity, created_at, will_clear_at = row
#     print(code, bid_price, quantity, created_at, will_clear_at)


# sql = "insert into balance(code, bid_price, quantity, created_at,will_clear_at) values(?,?,?,?,?)"
# cur.execute(sql, ("0077000", 35000, 30, "20240317", 'today'))
# print(cur.rowcount)


# sql = "update balance set will_clear_at = :will_clear_at where bid_price = :bid_price"
# cur.execute(sql, {"will_clear_at": "next", "bid_price": 100000})
# print(cur.rowcount)

# sql = "delete from balance where will_clear_at = :will_clear_at"
# cur.execute(sql, {"will_clear_at": "next"})
# print(cur.rowcount)

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
    