"""Position repository: SQL persistence with an explicit RepositoryContext."""

import json
import sqlite3

from util import db_schema


def save_position_strategy(
    code, code_name, strategy_name, quantity, buy_price, *, context
):
    db_schema.init_position_strategy_table(context=context)
    with context.connect(context.position_db) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO position_strategy
            (code, code_name, strategy_name, quantity, buy_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                str(code).zfill(6),
                code_name,
                strategy_name,
                int(quantity or 0),
                int(buy_price or 0),
                context.now_text(),
            ),
        )


def get_position_strategy(code, *, context):
    db_schema.init_position_strategy_table(context=context)
    with context.connect(context.position_db) as con:
        row = con.execute(
            """
            SELECT strategy_name FROM position_strategy WHERE code = ?
        """,
            (str(code).zfill(6),),
        ).fetchone()
    return None if row is None else row[0]


def get_position_detail(code, *, context):
    db_schema.init_position_strategy_table(context=context)
    with context.connect(context.position_db) as con:
        row = con.execute(
            """
            SELECT code, code_name, strategy_name, quantity, buy_price, created_at
            FROM position_strategy WHERE code = ?
        """,
            (str(code).zfill(6),),
        ).fetchone()
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


def get_all_position_details(*, context):
    """
    strategy_position.db의 전체 전략 포지션 정보를 코드 기준 딕셔너리로 반환한다.

    quantity=0, buy_price=0인 매수 예약 행도 포함한다.
    실제 잔고가 생겼는데 체결 반영이 누락된 경우를 찾기 위함이다.
    """
    db_schema.init_position_strategy_table(context=context)

    with context.connect(context.position_db) as con:
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


def delete_position_strategy(code, *, context):
    db_schema.init_position_strategy_table(context=context)
    with context.connect(context.position_db) as con:
        con.execute(
            "DELETE FROM position_strategy WHERE code = ?", (str(code).zfill(6),)
        )


def update_position_from_buy_fill(
    code, code_name, strategy_name, fill_quantity, fill_price, *, context
):
    """
    실제 매수 체결 수량과 체결가를 기준으로
    position_strategy의 보유수량과 평균매입가를 갱신한다.
    """
    fill_quantity = int(fill_quantity or 0)
    fill_price = float(fill_price or 0)

    if fill_quantity <= 0 or fill_price <= 0:
        return

    db_schema.init_position_strategy_table(context=context)

    normalized_code = str(code).zfill(6)
    filled_at = context.now_text()

    with context.connect(context.position_db) as con:
        row = con.execute(
            """
            SELECT strategy_name, quantity, buy_price, created_at
            FROM position_strategy
            WHERE code = ?
        """,
            (normalized_code,),
        ).fetchone()

        # 주문 직후 예약 저장이 없었더라도,
        # 전략명이 확인되면 실제 체결 기준으로 새 포지션을 만든다.
        if row is None:
            if not strategy_name:
                print(
                    f"[DB] 매수 체결의 전략명을 확인할 수 없어 "
                    f"포지션 저장을 생략합니다: {normalized_code}"
                )
                return

            con.execute(
                """
                INSERT INTO position_strategy
                (code, code_name, strategy_name, quantity, buy_price, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    normalized_code,
                    code_name,
                    strategy_name,
                    fill_quantity,
                    fill_price,
                    filled_at,
                ),
            )
            return

        stored_strategy, old_quantity, old_buy_price, created_at = row

        old_quantity = int(old_quantity or 0)
        old_buy_price = float(old_buy_price or 0)

        total_quantity = old_quantity + fill_quantity

        average_buy_price = (
            old_quantity * old_buy_price + fill_quantity * fill_price
        ) / total_quantity

        con.execute(
            """
            UPDATE position_strategy
            SET code_name = ?,
                strategy_name = ?,
                quantity = ?,
                buy_price = ?,
                created_at = ?
            WHERE code = ?
        """,
            (
                code_name,
                strategy_name or stored_strategy,
                total_quantity,
                average_buy_price,
                filled_at if old_quantity == 0 else created_at,
                normalized_code,
            ),
        )


def get_filled_position_codes(*, context):
    """
    실제 매수 체결 이력이 있는 전략 포지션 코드 목록을 반환한다.

    - quantity > 0: 현재 보유 중인 포지션
    - buy_price > 0: 전량 매도 체결 후 삭제 대기 중인 포지션
    - quantity = 0, buy_price = 0: 아직 체결되지 않은 매수 예약 행이므로 제외
    """
    db_schema.init_position_strategy_table(context=context)

    with context.connect(context.position_db) as con:
        rows = con.execute("""
            SELECT code
            FROM position_strategy
            WHERE quantity > 0 OR buy_price > 0
        """).fetchall()

    return [row[0] for row in rows]


def get_strategy_position_counts(*, context):
    db_schema.init_position_strategy_table(context=context)

    with context.connect(context.position_db) as con:
        rows = con.execute("""
            SELECT strategy_name, COUNT(*)
            FROM position_strategy
            WHERE quantity > 0
            GROUP BY strategy_name
        """).fetchall()

    return {strategy_name: count for strategy_name, count in rows}


def reduce_position_from_sell_fill(code, fill_quantity, *, context):
    """
    실제 매도 체결 수량만큼 position_strategy 수량을 감소시킨다.
    전량 매도 후 행 삭제는 StrategyManager가 실제 잔고 확인 후 처리한다.
    """
    fill_quantity = int(fill_quantity or 0)

    if fill_quantity <= 0:
        return

    db_schema.init_position_strategy_table(context=context)

    normalized_code = str(code).zfill(6)

    with context.connect(context.position_db) as con:
        row = con.execute(
            """
            SELECT quantity
            FROM position_strategy
            WHERE code = ?
        """,
            (normalized_code,),
        ).fetchone()

        if row is None:
            return

        old_quantity = int(row[0] or 0)
        new_quantity = max(0, old_quantity - fill_quantity)

        con.execute(
            """
            UPDATE position_strategy
            SET quantity = ?
            WHERE code = ?
        """,
            (
                new_quantity,
                normalized_code,
            ),
        )
