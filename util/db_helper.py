import json
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


def _json_dumps_safe(value):
    """전략 조건 스냅샷을 한글이 깨지지 않는 JSON 문자열로 변환한다."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def build_strategy_signal_key(
    strategy_name,
    code,
    signal_type,
    reason_code,
    signal_date=None,
):
    signal_date = signal_date or datetime.now().strftime("%Y%m%d")
    return "|".join([
        str(strategy_name),
        str(code).strip().zfill(6),
        str(signal_date),
        str(signal_type).upper(),
        str(reason_code).upper(),
    ])


def save_strategy_signal(
    strategy_name,
    code,
    code_name,
    signal_type,
    reason_code,
    signal_reason="",
    current_price=None,
    condition_data=None,
    order_attempted=False,
    order_result=None,
    blocked_reason=None,
    detected_at=None,
):
    """전략 조건 충족을 monitoring.db에 저장한다.

    동일한 전략/종목/날짜/신호종류/사유코드는 한 행으로 유지한다.
    반복 틱에서는 hit_count와 last_detected_at을 갱신한다.
    주문 결과가 새로 들어오면 기존 행의 주문 상태도 함께 갱신한다.
    """
    init_monitoring_tables()

    now_text = str(detected_at or _now_text())
    signal_date = now_text[:8] if len(now_text) >= 8 else datetime.now().strftime("%Y%m%d")
    normalized_code = str(code).strip().zfill(6)
    signal_type = str(signal_type).upper()
    reason_code = str(reason_code).upper()
    signal_key = build_strategy_signal_key(
        strategy_name=strategy_name,
        code=normalized_code,
        signal_type=signal_type,
        reason_code=reason_code,
        signal_date=signal_date,
    )

    condition_json = _json_dumps_safe(condition_data)
    attempted = 1 if order_attempted else 0

    with sqlite3.connect(MONITORING_DB) as con:
        con.execute("""
            INSERT INTO strategy_signal_log (
                signal_key, signal_date, strategy_name, code, code_name,
                signal_type, reason_code, signal_reason, current_price,
                condition_data, order_attempted, order_result, blocked_reason,
                first_detected_at, last_detected_at, hit_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(signal_key) DO UPDATE SET
                code_name = excluded.code_name,
                signal_reason = excluded.signal_reason,
                current_price = excluded.current_price,
                condition_data = excluded.condition_data,
                order_attempted = CASE
                    WHEN excluded.order_attempted = 1 THEN 1
                    ELSE strategy_signal_log.order_attempted
                END,
                order_result = CASE
                    WHEN excluded.order_result IS NOT NULL THEN excluded.order_result
                    ELSE strategy_signal_log.order_result
                END,
                blocked_reason = CASE
                    WHEN excluded.order_result = 'ORDER_SENT' THEN NULL
                    WHEN excluded.blocked_reason IS NOT NULL THEN excluded.blocked_reason
                    ELSE strategy_signal_log.blocked_reason
                END,
                last_detected_at = excluded.last_detected_at,
                hit_count = strategy_signal_log.hit_count + 1
        """, (
            signal_key,
            signal_date,
            str(strategy_name),
            normalized_code,
            str(code_name or normalized_code),
            signal_type,
            reason_code,
            str(signal_reason or ""),
            None if current_price is None else float(current_price),
            condition_json,
            attempted,
            order_result,
            blocked_reason,
            now_text,
            now_text,
        ))

    return signal_key


def update_strategy_signal_order_result(
    signal_key,
    order_attempted,
    order_result,
    blocked_reason=None,
):
    """이미 저장된 전략 신호에 실제 주문 시도/결과를 연결한다."""
    if not signal_key:
        return False

    init_monitoring_tables()
    with sqlite3.connect(MONITORING_DB) as con:
        cursor = con.execute("""
            UPDATE strategy_signal_log
            SET order_attempted = ?,
                order_result = ?,
                blocked_reason = ?,
                last_detected_at = ?
            WHERE signal_key = ?
        """, (
            1 if order_attempted else 0,
            str(order_result or ""),
            blocked_reason,
            _now_text(),
            str(signal_key),
        ))
    return cursor.rowcount > 0


def get_strategy_signals(
    strategy_name=None,
    signal_type=None,
    signal_date=None,
    limit=500,
):
    """향후 FastAPI에서 바로 사용할 수 있는 전략 신호 조회 함수."""
    init_monitoring_tables()

    where = []
    params = []
    if strategy_name:
        where.append("strategy_name = ?")
        params.append(str(strategy_name))
    if signal_type:
        where.append("signal_type = ?")
        params.append(str(signal_type).upper())
    if signal_date:
        where.append("signal_date = ?")
        params.append(str(signal_date))

    sql = "SELECT * FROM strategy_signal_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_detected_at DESC LIMIT ?"
    params.append(max(1, int(limit)))

    with sqlite3.connect(MONITORING_DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        raw = item.get("condition_data")
        if raw:
            try:
                item["condition_data"] = json.loads(raw)
            except Exception:
                pass
        result.append(item)
    return result

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

# ---------------------------------------------------------------------------
# PullbackTrendStrategy 실시간 실행 상태 저장
# ---------------------------------------------------------------------------



def save_strategy_equity_snapshots(
    strategy_names,
    snapshot_at=None,
):
    """전략별 실제 거래 기반 성과 스냅샷을 저장한다.

    계산 기준
    ---------
    open_cost_basis:
        현재 보유주식 매입원금(position_snapshot.purchase_amount)

    market_value:
        현재 보유주식 평가금액(position_snapshot.evaluation_amount)

    unrealized_pnl:
        현재 평가금액 - 현재 매입원금

    cumulative_net_realized_pnl:
        real_trades의 전체 누적 순실현손익.
        과거 데이터에 net 값이 없으면 gross/realized_pnl을 순서대로 fallback한다.

    lifetime_sold_cost_basis:
        지금까지 매도 완료된 수량의 매입원금 합계.

    deployed_capital:
        lifetime_sold_cost_basis + 현재 보유 매입원금.
        즉 지금까지 전략이 실제로 주식에 배치한 누적 매입원금이다.

    return_on_deployed_capital_pct:
        (누적 순실현손익 + 현재 미실현손익) / deployed_capital * 100

    전략별 현금이 분리되어 있지 않으므로 이 값은 독립 계좌 NAV 수익률이 아니다.
    """
    init_monitoring_tables()

    normalized_names = [
        str(name)
        for name in dict.fromkeys(strategy_names or [])
        if str(name).strip()
    ]

    if not normalized_names:
        return []

    now_text = str(
        snapshot_at
        or _now_text()
    )
    snapshot_date = (
        now_text[:8]
        if len(now_text) >= 8
        else datetime.now().strftime("%Y%m%d")
    )

    saved = []

    with sqlite3.connect(
        MONITORING_DB,
        timeout=15,
    ) as con:
        con.row_factory = sqlite3.Row
        con.execute(
            "PRAGMA busy_timeout = 15000"
        )
        con.execute(
            "PRAGMA journal_mode = WAL"
        )
        con.execute(
            "PRAGMA synchronous = NORMAL"
        )

        for strategy_name in normalized_names:
            open_row = con.execute("""
                SELECT
                    COUNT(*) AS holding_count,
                    COALESCE(
                        SUM(purchase_amount),
                        0
                    ) AS open_cost_basis,
                    COALESCE(
                        SUM(evaluation_amount),
                        0
                    ) AS market_value,
                    COALESCE(
                        SUM(unrealized_pnl),
                        0
                    ) AS unrealized_pnl
                FROM position_snapshot
                WHERE strategy_name = ?
                  AND quantity > 0
            """, (
                strategy_name,
            )).fetchone()

            realized_row = con.execute("""
                SELECT
                    COALESCE(
                        SUM(
                            COALESCE(
                                gross_realized_pnl,
                                realized_pnl,
                                0
                            )
                        ),
                        0
                    ) AS gross_realized_pnl,

                    COALESCE(
                        SUM(
                            COALESCE(
                                fee,
                                0
                            )
                        ),
                        0
                    ) AS fee,

                    COALESCE(
                        SUM(
                            COALESCE(
                                tax,
                                0
                            )
                        ),
                        0
                    ) AS tax,

                    COALESCE(
                        SUM(
                            COALESCE(
                                net_realized_pnl,
                                gross_realized_pnl,
                                realized_pnl,
                                0
                            )
                        ),
                        0
                    ) AS net_realized_pnl,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN buy_price IS NOT NULL
                                THEN buy_price * quantity
                                ELSE 0
                            END
                        ),
                        0
                    ) AS sold_cost_basis
                FROM real_trades
                WHERE strategy_name = ?
            """, (
                strategy_name,
            )).fetchone()

            holding_count = int(
                open_row["holding_count"]
                or 0
            )
            open_cost_basis = float(
                open_row["open_cost_basis"]
                or 0
            )
            market_value = float(
                open_row["market_value"]
                or 0
            )
            unrealized_pnl = float(
                open_row["unrealized_pnl"]
                or 0
            )

            cumulative_gross_realized_pnl = float(
                realized_row["gross_realized_pnl"]
                or 0
            )
            cumulative_fee = float(
                realized_row["fee"]
                or 0
            )
            cumulative_tax = float(
                realized_row["tax"]
                or 0
            )
            cumulative_net_realized_pnl = float(
                realized_row["net_realized_pnl"]
                or 0
            )
            lifetime_sold_cost_basis = float(
                realized_row["sold_cost_basis"]
                or 0
            )

            total_pnl = (
                cumulative_net_realized_pnl
                + unrealized_pnl
            )

            deployed_capital = (
                lifetime_sold_cost_basis
                + open_cost_basis
            )

            return_pct = (
                total_pnl
                / deployed_capital
                * 100.0
                if deployed_capital > 0
                else 0.0
            )

            con.execute("""
                INSERT INTO strategy_equity_snapshot (
                    snapshot_at,
                    snapshot_date,
                    strategy_name,
                    holding_count,

                    open_cost_basis,
                    market_value,
                    unrealized_pnl,

                    cumulative_gross_realized_pnl,
                    cumulative_fee,
                    cumulative_tax,
                    cumulative_net_realized_pnl,

                    total_pnl,

                    lifetime_sold_cost_basis,
                    deployed_capital,
                    return_on_deployed_capital_pct,

                    return_method
                )
                VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?,
                    ?, ?, ?,
                    'DEPLOYED_CAPITAL_ROI'
                )
                ON CONFLICT(
                    strategy_name,
                    snapshot_at
                ) DO UPDATE SET
                    snapshot_date = excluded.snapshot_date,
                    holding_count = excluded.holding_count,

                    open_cost_basis = excluded.open_cost_basis,
                    market_value = excluded.market_value,
                    unrealized_pnl = excluded.unrealized_pnl,

                    cumulative_gross_realized_pnl =
                        excluded.cumulative_gross_realized_pnl,
                    cumulative_fee = excluded.cumulative_fee,
                    cumulative_tax = excluded.cumulative_tax,
                    cumulative_net_realized_pnl =
                        excluded.cumulative_net_realized_pnl,

                    total_pnl = excluded.total_pnl,

                    lifetime_sold_cost_basis =
                        excluded.lifetime_sold_cost_basis,
                    deployed_capital = excluded.deployed_capital,
                    return_on_deployed_capital_pct =
                        excluded.return_on_deployed_capital_pct,
                    return_method = excluded.return_method
            """, (
                now_text,
                snapshot_date,
                strategy_name,
                holding_count,

                open_cost_basis,
                market_value,
                unrealized_pnl,

                cumulative_gross_realized_pnl,
                cumulative_fee,
                cumulative_tax,
                cumulative_net_realized_pnl,

                total_pnl,

                lifetime_sold_cost_basis,
                deployed_capital,
                return_pct,
            ))

            saved.append({
                "snapshot_at": now_text,
                "snapshot_date": snapshot_date,
                "strategy_name": strategy_name,
                "holding_count": holding_count,
                "open_cost_basis": open_cost_basis,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "cumulative_gross_realized_pnl": (
                    cumulative_gross_realized_pnl
                ),
                "cumulative_fee": cumulative_fee,
                "cumulative_tax": cumulative_tax,
                "cumulative_net_realized_pnl": (
                    cumulative_net_realized_pnl
                ),
                "total_pnl": total_pnl,
                "lifetime_sold_cost_basis": (
                    lifetime_sold_cost_basis
                ),
                "deployed_capital": deployed_capital,
                "return_on_deployed_capital_pct": (
                    return_pct
                ),
                "return_method": "DEPLOYED_CAPITAL_ROI",
            })

    return saved


def get_latest_strategy_equity_snapshot(
    strategy_name,
):
    init_monitoring_tables()

    with sqlite3.connect(MONITORING_DB) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("""
            SELECT *
            FROM strategy_equity_snapshot
            WHERE strategy_name = ?
            ORDER BY snapshot_at DESC
            LIMIT 1
        """, (
            str(strategy_name),
        )).fetchone()

    return dict(row) if row else None


def get_strategy_equity_snapshots(
    strategy_name,
    date_from=None,
    date_to=None,
    limit=2000,
):
    init_monitoring_tables()

    where = [
        "strategy_name = ?"
    ]
    params = [
        str(strategy_name)
    ]

    if date_from:
        where.append(
            "snapshot_date >= ?"
        )
        params.append(
            str(date_from)
        )

    if date_to:
        where.append(
            "snapshot_date <= ?"
        )
        params.append(
            str(date_to)
        )

    params.append(
        max(
            1,
            min(
                int(limit or 2000),
                10000,
            ),
        )
    )

    with sqlite3.connect(MONITORING_DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""
            SELECT *
            FROM strategy_equity_snapshot
            WHERE {' AND '.join(where)}
            ORDER BY snapshot_at ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]

def save_position_snapshot(balance, realtime_price_map=None):
    """현재 실제 보유종목을 monitoring.db.position_snapshot에 저장한다.

    - balance: Kiwoom.balance
    - realtime_price_map: Kiwoom.universe_realtime_transaction_info

    정상 잔고 조회가 확인된 상태에서 StrategyManager가 호출한다.
    현재 보유종목 전체를 한 트랜잭션에서 DELETE + INSERT 하므로
    전량매도된 종목은 다음 스냅샷에서 자동으로 사라진다.
    """
    init_monitoring_tables()
    init_position_strategy_table()

    if balance is None:
        return 0

    realtime_price_map = realtime_price_map or {}
    position_details = get_all_position_details()
    now_text = _now_text()
    rows = []

    for raw_code, raw_info in dict(balance).items():
        code = str(raw_code).strip().upper().zfill(6)
        info = raw_info or {}

        quantity = int(info.get("보유수량", 0) or 0)
        if quantity <= 0:
            continue

        available_quantity = int(
            info.get("매매가능수량", 0)
            or info.get("주문가능수량", 0)
            or quantity
        )

        buy_price = float(
            info.get("매입가")
            or info.get("매입단가")
            or 0
        )

        rt = realtime_price_map.get(code, {}) or {}
        current_price = float(
            rt.get("현재가")
            or info.get("현재가")
            or buy_price
            or 0
        )

        purchase_amount = float(
            info.get("매입금액")
            or info.get("총매입가")
            or (buy_price * quantity)
            or 0
        )

        evaluation_amount = float(
            max(0, current_price)
            * max(0, quantity)
        )

        unrealized_pnl = (
            evaluation_amount
            - purchase_amount
        )

        if purchase_amount > 0:
            return_pct = (
                unrealized_pnl
                / purchase_amount
                * 100.0
            )
        else:
            return_pct = float(
                info.get("수익률", 0)
                or info.get("손익율", 0)
                or 0
            )

        position = position_details.get(code, {})
        strategy_name = (
            position.get("strategy_name")
            or get_position_strategy(code)
        )

        code_name = str(
            info.get("종목명")
            or position.get("code_name")
            or code
        ).strip()

        rows.append((
            code,
            code_name,
            strategy_name,
            quantity,
            max(0, available_quantity),
            buy_price,
            current_price,
            purchase_amount,
            evaluation_amount,
            unrealized_pnl,
            return_pct,
            now_text,
        ))

    # FastAPI가 동시에 읽더라도 커밋 전의 일관된 이전 스냅샷 또는
    # 커밋 후의 새 스냅샷만 보도록 하나의 트랜잭션으로 교체한다.
    with sqlite3.connect(
        MONITORING_DB,
        timeout=15,
    ) as con:
        con.execute("PRAGMA busy_timeout = 15000")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")

        con.execute(
            "DELETE FROM position_snapshot"
        )

        if rows:
            con.executemany("""
                INSERT INTO position_snapshot (
                    code,
                    code_name,
                    strategy_name,
                    quantity,
                    available_quantity,
                    buy_price,
                    current_price,
                    purchase_amount,
                    evaluation_amount,
                    unrealized_pnl,
                    return_pct,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)

    return len(rows)


def get_position_snapshots(strategy_name=None):
    """웹/API 점검용 최신 보유 스냅샷 조회."""
    init_monitoring_tables()

    sql = """
        SELECT
            code,
            code_name,
            strategy_name,
            quantity,
            available_quantity,
            buy_price,
            current_price,
            purchase_amount,
            evaluation_amount,
            unrealized_pnl,
            return_pct,
            updated_at
        FROM position_snapshot
    """
    params = []

    if strategy_name:
        sql += " WHERE strategy_name = ?"
        params.append(str(strategy_name))

    sql += " ORDER BY strategy_name, code"

    with sqlite3.connect(MONITORING_DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            sql,
            params,
        ).fetchall()

    return [dict(row) for row in rows]

def init_pullback_runtime_table():
    """눌림목 전략의 전고점 부분익절/다음날 거래량 비교 상태를 저장한다."""
    with sqlite3.connect(MONITORING_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pullback_runtime_state (
                code TEXT PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                previous_high REAL,
                high_touch_date TEXT,
                high_touch_volume INTEGER NOT NULL DEFAULT 0,
                partial_exit_requested INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)


def save_pullback_runtime_state(
    code,
    strategy_name,
    previous_high=None,
    high_touch_date=None,
    high_touch_volume=None,
    partial_exit_requested=None,
):
    """
    눌림목 전략 실행 상태를 저장한다.

    None으로 전달된 값은 기존 값이 있으면 보존한다.
    따라서 전고점 터치 당일에는 high_touch_volume만 계속 갱신할 수 있다.
    """
    init_pullback_runtime_table()

    normalized_code = str(code).strip().upper().zfill(6)
    now_text = _now_text()

    with sqlite3.connect(MONITORING_DB) as con:
        row = con.execute("""
            SELECT
                strategy_name,
                previous_high,
                high_touch_date,
                high_touch_volume,
                partial_exit_requested
            FROM pullback_runtime_state
            WHERE code = ?
        """, (normalized_code,)).fetchone()

        if row is None:
            con.execute("""
                INSERT INTO pullback_runtime_state (
                    code,
                    strategy_name,
                    previous_high,
                    high_touch_date,
                    high_touch_volume,
                    partial_exit_requested,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                normalized_code,
                str(strategy_name),
                None if previous_high is None else float(previous_high),
                None if high_touch_date is None else str(high_touch_date),
                int(high_touch_volume or 0),
                1 if bool(partial_exit_requested) else 0,
                now_text,
            ))
            return

        (
            stored_strategy_name,
            stored_previous_high,
            stored_high_touch_date,
            stored_high_touch_volume,
            stored_partial_exit_requested,
        ) = row

        new_strategy_name = (
            str(strategy_name)
            if strategy_name not in (None, "")
            else stored_strategy_name
        )
        new_previous_high = (
            float(previous_high)
            if previous_high is not None
            else stored_previous_high
        )

        # high_touch_date는 신규 진입 시 명시적으로 None을 넣어 초기화해야 하는 경우가 있다.
        # save_position 직후에는 기존 행을 먼저 delete한 뒤 저장하므로 None은 정상적으로 NULL 저장된다.
        # 기존 행 업데이트에서 None은 "값 유지"로 취급한다.
        new_high_touch_date = (
            str(high_touch_date)
            if high_touch_date is not None
            else stored_high_touch_date
        )
        new_high_touch_volume = (
            int(high_touch_volume)
            if high_touch_volume is not None
            else int(stored_high_touch_volume or 0)
        )
        new_partial_exit_requested = (
            1 if bool(partial_exit_requested) else 0
            if partial_exit_requested is not None
            else int(stored_partial_exit_requested or 0)
        )

        con.execute("""
            UPDATE pullback_runtime_state
            SET
                strategy_name = ?,
                previous_high = ?,
                high_touch_date = ?,
                high_touch_volume = ?,
                partial_exit_requested = ?,
                updated_at = ?
            WHERE code = ?
        """, (
            new_strategy_name,
            new_previous_high,
            new_high_touch_date,
            new_high_touch_volume,
            new_partial_exit_requested,
            now_text,
            normalized_code,
        ))


def get_pullback_runtime_state(code):
    """종목의 눌림목 런타임 상태를 dict로 반환한다."""
    init_pullback_runtime_table()

    normalized_code = str(code).strip().upper().zfill(6)

    with sqlite3.connect(MONITORING_DB) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("""
            SELECT
                code,
                strategy_name,
                previous_high,
                high_touch_date,
                high_touch_volume,
                partial_exit_requested,
                updated_at
            FROM pullback_runtime_state
            WHERE code = ?
        """, (normalized_code,)).fetchone()

    if row is None:
        return None

    result = dict(row)
    result["high_touch_volume"] = int(result.get("high_touch_volume") or 0)
    result["partial_exit_requested"] = bool(
        result.get("partial_exit_requested")
    )
    return result


def delete_pullback_runtime_state(code):
    """전량매도 완료 또는 신규 진입 초기화 시 눌림목 런타임 상태를 삭제한다."""
    init_pullback_runtime_table()

    normalized_code = str(code).strip().upper().zfill(6)

    with sqlite3.connect(MONITORING_DB) as con:
        con.execute(
            "DELETE FROM pullback_runtime_state WHERE code = ?",
            (normalized_code,),
        )


# ---------------------------------------------------------------------------
# ORBStrategy 실시간 실행 상태 저장
# ---------------------------------------------------------------------------

def init_orb_runtime_table():
    """프로그램 재시작 후에도 당일 ORB 손절/목표/청산 상태를 복구할 수 있게 한다."""
    with sqlite3.connect(MONITORING_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS orb_runtime_state (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                code_name TEXT,
                strategy_name TEXT NOT NULL,
                status TEXT NOT NULL,
                skip_reason TEXT,
                first_open INTEGER,
                first_high INTEGER,
                first_low INTEGER,
                first_close INTEGER,
                signal_price INTEGER,
                planned_quantity INTEGER,
                account_equity REAL,
                available_cash INTEGER,
                sizing_mode TEXT,
                risk_budget REAL,
                entry_price REAL,
                quantity INTEGER,
                stop_price REAL,
                target_price REAL,
                exit_reason TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            )
        """)


def save_orb_runtime_state(
    trade_date,
    code,
    code_name,
    strategy_name,
    status,
    skip_reason=None,
    first_bar=None,
    signal_price=None,
    planned_quantity=None,
    account_equity=None,
    available_cash=None,
    sizing_mode=None,
    risk_budget=None,
    entry_price=None,
    quantity=None,
    stop_price=None,
    target_price=None,
    exit_reason=None,
):
    """ORB 당일 실행 상태를 upsert 한다. 미지정 항목은 기존 값이 있으면 보존한다."""
    init_orb_runtime_table()
    normalized_code = str(code).zfill(6)
    first_bar = first_bar or {}

    values = {
        "trade_date": str(trade_date),
        "code": normalized_code,
        "code_name": code_name,
        "strategy_name": strategy_name,
        "status": status,
        "skip_reason": skip_reason,
        "first_open": first_bar.get("open"),
        "first_high": first_bar.get("high"),
        "first_low": first_bar.get("low"),
        "first_close": first_bar.get("close"),
        "signal_price": signal_price,
        "planned_quantity": planned_quantity,
        "account_equity": account_equity,
        "available_cash": available_cash,
        "sizing_mode": sizing_mode,
        "risk_budget": risk_budget,
        "entry_price": entry_price,
        "quantity": quantity,
        "stop_price": stop_price,
        "target_price": target_price,
        "exit_reason": exit_reason,
        "updated_at": _now_text(),
    }

    with sqlite3.connect(MONITORING_DB) as con:
        existing = con.execute("""
            SELECT code_name, strategy_name, status, skip_reason,
                   first_open, first_high, first_low, first_close,
                   signal_price, planned_quantity, account_equity,
                   available_cash, sizing_mode, risk_budget, entry_price,
                   quantity, stop_price, target_price, exit_reason
            FROM orb_runtime_state
            WHERE trade_date = ? AND code = ?
        """, (values["trade_date"], normalized_code)).fetchone()

        if existing is not None:
            columns = [
                "code_name", "strategy_name", "status", "skip_reason",
                "first_open", "first_high", "first_low", "first_close",
                "signal_price", "planned_quantity", "account_equity",
                "available_cash", "sizing_mode", "risk_budget", "entry_price",
                "quantity", "stop_price", "target_price", "exit_reason",
            ]
            prior = dict(zip(columns, existing))
            for column in columns:
                if column in ("status", "strategy_name"):
                    continue
                if values[column] is None:
                    values[column] = prior[column]

        con.execute("""
            INSERT INTO orb_runtime_state (
                trade_date, code, code_name, strategy_name, status, skip_reason,
                first_open, first_high, first_low, first_close,
                signal_price, planned_quantity, account_equity, available_cash,
                sizing_mode, risk_budget, entry_price, quantity, stop_price,
                target_price, exit_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, code) DO UPDATE SET
                code_name = excluded.code_name,
                strategy_name = excluded.strategy_name,
                status = excluded.status,
                skip_reason = excluded.skip_reason,
                first_open = excluded.first_open,
                first_high = excluded.first_high,
                first_low = excluded.first_low,
                first_close = excluded.first_close,
                signal_price = excluded.signal_price,
                planned_quantity = excluded.planned_quantity,
                account_equity = excluded.account_equity,
                available_cash = excluded.available_cash,
                sizing_mode = excluded.sizing_mode,
                risk_budget = excluded.risk_budget,
                entry_price = excluded.entry_price,
                quantity = excluded.quantity,
                stop_price = excluded.stop_price,
                target_price = excluded.target_price,
                exit_reason = excluded.exit_reason,
                updated_at = excluded.updated_at
        """, (
            values["trade_date"], values["code"], values["code_name"],
            values["strategy_name"], values["status"], values["skip_reason"],
            values["first_open"], values["first_high"], values["first_low"],
            values["first_close"], values["signal_price"],
            values["planned_quantity"], values["account_equity"],
            values["available_cash"], values["sizing_mode"],
            values["risk_budget"], values["entry_price"], values["quantity"],
            values["stop_price"], values["target_price"], values["exit_reason"],
            values["updated_at"],
        ))


def get_orb_runtime_state(trade_date, code):
    init_orb_runtime_table()
    normalized_code = str(code).zfill(6)
    with sqlite3.connect(MONITORING_DB) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("""
            SELECT * FROM orb_runtime_state
            WHERE trade_date = ? AND code = ?
        """, (str(trade_date), normalized_code)).fetchone()
    return None if row is None else dict(row)
