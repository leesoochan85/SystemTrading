"""Order repository: SQL persistence with an explicit RepositoryContext."""

import json
import sqlite3

from util import db_schema
from util import position_repository


def _json_dumps_safe(value, *, context):
    """전략 조건 스냅샷을 한글이 깨지지 않는 JSON 문자열로 변환한다."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def build_strategy_signal_key(
    strategy_name, code, signal_type, reason_code, signal_date=None, *, context
):
    signal_date = signal_date or context.clock().strftime("%Y%m%d")
    return "|".join(
        [
            str(strategy_name),
            str(code).strip().zfill(6),
            str(signal_date),
            str(signal_type).upper(),
            str(reason_code).upper(),
        ]
    )


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
    *,
    context,
):
    """전략 조건 충족을 monitoring.db에 저장한다.

    동일한 전략/종목/날짜/신호종류/사유코드는 한 행으로 유지한다.
    반복 틱에서는 hit_count와 last_detected_at을 갱신한다.
    주문 결과가 새로 들어오면 기존 행의 주문 상태도 함께 갱신한다.
    """
    db_schema.init_monitoring_tables(context=context)

    now_text = str(detected_at or context.now_text())
    signal_date = (
        now_text[:8] if len(now_text) >= 8 else context.clock().strftime("%Y%m%d")
    )
    normalized_code = str(code).strip().zfill(6)
    signal_type = str(signal_type).upper()
    reason_code = str(reason_code).upper()
    signal_key = build_strategy_signal_key(
        strategy_name=strategy_name,
        code=normalized_code,
        signal_type=signal_type,
        reason_code=reason_code,
        signal_date=signal_date,
        context=context,
    )

    condition_json = _json_dumps_safe(condition_data, context=context)
    attempted = 1 if order_attempted else 0

    with context.connect(context.monitoring_db) as con:
        con.execute(
            """
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
        """,
            (
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
            ),
        )

    return signal_key


def update_strategy_signal_order_result(
    signal_key, order_attempted, order_result, blocked_reason=None, *, context
):
    """이미 저장된 전략 신호에 실제 주문 시도/결과를 연결한다."""
    if not signal_key:
        return False

    db_schema.init_monitoring_tables(context=context)
    with context.connect(context.monitoring_db) as con:
        cursor = con.execute(
            """
            UPDATE strategy_signal_log
            SET order_attempted = ?,
                order_result = ?,
                blocked_reason = ?,
                last_detected_at = ?
            WHERE signal_key = ?
        """,
            (
                1 if order_attempted else 0,
                str(order_result or ""),
                blocked_reason,
                context.now_text(),
                str(signal_key),
            ),
        )
    return cursor.rowcount > 0


def get_strategy_signals(
    strategy_name=None, signal_type=None, signal_date=None, limit=500, *, context
):
    """향후 FastAPI에서 바로 사용할 수 있는 전략 신호 조회 함수."""
    db_schema.init_monitoring_tables(context=context)

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

    with context.connect(context.monitoring_db) as con:
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
    event_key,
    code,
    code_name,
    order_type,
    strategy_name="",
    order_quantity=0,
    order_price=0,
    remaining_quantity=0,
    filled_quantity=0,
    filled_price=0,
    order_status="",
    event_type="",
    order_no="",
    original_order_no="",
    request_id="",
    message="",
    *,
    context,
):
    db_schema.init_monitoring_tables(context=context)
    with context.connect(context.monitoring_db) as con:
        con.execute(
            """
            INSERT OR IGNORE INTO order_log (
                event_key, request_id, order_no, original_order_no, code, code_name,
                order_type, strategy_name, order_quantity, order_price,
                remaining_quantity, filled_quantity, filled_price, order_status,
                event_type, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                str(event_key),
                str(request_id or ""),
                str(order_no or ""),
                str(original_order_no or ""),
                str(code).zfill(6),
                code_name,
                order_type,
                strategy_name,
                int(order_quantity or 0),
                int(order_price or 0),
                int(remaining_quantity or 0),
                int(filled_quantity or 0),
                int(filled_price or 0),
                order_status,
                event_type,
                message,
                context.now_text(),
            ),
        )


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
    filled_quantity=0,
    filled_price=0,
    fill_no="",
    *,
    context,
):
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
    event_key = "|".join(
        [
            "BROKER",
            str(order_no or ""),
            str(fill_no or ""),
            event_type,
            str(remaining_quantity or 0),
            str(filled_quantity or 0),
            normalized_status,
        ]
    )
    save_order_event(
        event_key=event_key,
        order_no=order_no,
        code=code,
        code_name=code_name,
        order_type=order_type,
        strategy_name=strategy_name,
        order_quantity=order_quantity,
        order_price=order_price,
        remaining_quantity=remaining_quantity,
        filled_quantity=filled_quantity,
        filled_price=filled_price,
        order_status=normalized_status,
        event_type=event_type,
        context=context,
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
    *,
    context,
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

    db_schema.init_monitoring_tables(context=context)

    now = context.now_text()
    gross_realized_pnl = None
    return_pct = None
    buy_price_value = None

    if buy_price is not None and float(buy_price) > 0:
        buy_price_value = float(buy_price)

    if order_type == "매도" and buy_price_value is not None:
        gross_realized_pnl = round((int(price) - buy_price_value) * int(quantity))

        return_pct = (int(price) - buy_price_value) / buy_price_value * 100

    received_fee = None if fee is None else int(fee or 0)
    received_tax = None if tax is None else int(tax or 0)

    if (
        gross_realized_pnl is not None
        and received_fee is not None
        and received_tax is not None
    ):
        net_realized_pnl = gross_realized_pnl - received_fee - received_tax
    else:
        net_realized_pnl = None

    with context.connect(context.monitoring_db) as con:
        cursor = con.execute(
            """
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
        """,
            (
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
            ),
        )

        is_new_fill = cursor.rowcount == 1

        if is_new_fill and order_type == "매도":
            normalized_order_no = str(order_no or "").strip()

            if normalized_order_no:
                trade_id = f"SELL_ORDER_{normalized_order_no}"
            else:
                trade_id = f"SELL_FILL_{fill_no}"

            existing_trade = con.execute(
                """
                SELECT
                    quantity,
                    sell_price,
                    realized_pnl,
                    gross_realized_pnl,
                    fee,
                    tax
                FROM real_trades
                WHERE trade_id = ?
            """,
                (trade_id,),
            ).fetchone()

            new_quantity = int(quantity)
            new_sell_price = float(price)

            if existing_trade is None:
                con.execute(
                    """
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
                """,
                    (
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
                    ),
                )

            else:
                old_quantity = int(existing_trade[0] or 0)
                old_sell_price = float(existing_trade[1] or 0)
                old_gross_realized_pnl = existing_trade[3]
                old_fee = existing_trade[4]
                old_tax = existing_trade[5]

                total_quantity = old_quantity + new_quantity

                average_sell_price = (
                    old_quantity * old_sell_price + new_quantity * new_sell_price
                ) / total_quantity

                if old_gross_realized_pnl is None or gross_realized_pnl is None:
                    total_gross_realized_pnl = None
                else:
                    total_gross_realized_pnl = int(old_gross_realized_pnl) + int(
                        gross_realized_pnl
                    )

                # 비용 FID는 누적 스냅샷일 수 있으므로 더하지 않고
                # 새로 수신한 값이 있으면 최신값으로 교체한다.
                final_fee = received_fee if received_fee is not None else old_fee
                final_tax = received_tax if received_tax is not None else old_tax

                if (
                    total_gross_realized_pnl is not None
                    and final_fee is not None
                    and final_tax is not None
                ):
                    total_net_realized_pnl = (
                        int(total_gross_realized_pnl) - int(final_fee) - int(final_tax)
                    )
                else:
                    total_net_realized_pnl = None

                if buy_price_value is not None and buy_price_value > 0:
                    aggregated_return_pct = (
                        (average_sell_price - buy_price_value) / buy_price_value * 100
                    )
                else:
                    aggregated_return_pct = None

                con.execute(
                    """
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
                """,
                    (
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
                    ),
                )

    if is_new_fill and order_type == "매수":
        position_repository.update_position_from_buy_fill(
            code=code,
            code_name=code_name,
            strategy_name=strategy_name,
            fill_quantity=quantity,
            fill_price=price,
            context=context,
        )

    if is_new_fill and order_type == "매도":
        position_repository.reduce_position_from_sell_fill(
            code=code, fill_quantity=quantity, context=context
        )


def update_latest_sell_trade_costs(code, fee=None, tax=None, *, context):
    """
    잔고 체잔 이벤트로 비용 정보가 늦게 도착했을 때,
    오늘 해당 종목의 가장 최근 매도 거래에 비용을 반영한다.

    주의:
    동일 종목을 같은 날 여러 차례 완전히 매매한 경우에는
    당일 누적 비용을 개별 거래에 정확히 배분할 수 없다.
    """
    if fee is None or tax is None:
        return

    db_schema.init_monitoring_tables(context=context)

    today = context.clock().strftime("%Y%m%d")
    normalized_code = str(code).zfill(6)
    fee_value = int(fee or 0)
    tax_value = int(tax or 0)

    with context.connect(context.monitoring_db) as con:
        row = con.execute(
            """
            SELECT trade_id, gross_realized_pnl
            FROM real_trades
            WHERE code = ?
              AND sell_date LIKE ?
              AND gross_realized_pnl IS NOT NULL
            ORDER BY sell_date DESC
            LIMIT 1
        """,
            (
                normalized_code,
                f"{today}%",
            ),
        ).fetchone()

        if row is None:
            return

        trade_id = row[0]
        gross_realized_pnl = int(row[1])

        net_realized_pnl = gross_realized_pnl - fee_value - tax_value

        con.execute(
            """
            UPDATE real_trades
            SET fee = ?,
                tax = ?,
                net_realized_pnl = ?
            WHERE trade_id = ?
        """,
            (
                fee_value,
                tax_value,
                net_realized_pnl,
                trade_id,
            ),
        )


def get_today_order_count(*, context):
    today = context.clock().strftime("%Y%m%d")
    db_schema.init_monitoring_tables(context=context)
    with context.connect(context.monitoring_db) as con:
        row = con.execute(
            """
            SELECT COUNT(*) FROM order_log
            WHERE created_at LIKE ? AND event_type = 'REQUESTED'
              AND order_type IN ('매수', '매도')
        """,
            (f"{today}%",),
        ).fetchone()
    return int(row[0] or 0)


def get_today_realized_pnl(*, context):
    """
    오늘의 총손익과 순손익을 반환한다.

    반환값:
    - gross_pnl: 수수료·세금 전 손익
    - net_pnl: 비용이 확인된 거래만 합산한 순손익
    - missing_buy_price_count: 매입가가 없어 총손익도 계산 못한 거래 수
    - missing_cost_count: 총손익은 있으나 비용이 없어 순손익 계산 못한 거래 수
    """
    today = context.clock().strftime("%Y%m%d")
    db_schema.init_monitoring_tables(context=context)

    with context.connect(context.monitoring_db) as con:
        row = con.execute(
            """
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
        """,
            (f"{today}%",),
        ).fetchone()

    return (
        int(row[0] or 0),
        int(row[1] or 0),
        int(row[2] or 0),
        int(row[3] or 0),
    )
