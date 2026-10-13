"""Performance repository: SQL persistence with an explicit RepositoryContext."""

import json
import sqlite3

from util import db_schema
from util import order_repository
from util import position_repository


def save_daily_equity(deposit, balance, *, context):
    db_schema.init_monitoring_tables(context=context)
    today = context.clock().strftime("%Y%m%d")

    evaluation_amount = 0

    for info in balance.values():
        quantity = int(info.get("보유수량", 0) or 0)
        current_price = int(info.get("현재가", 0) or info.get("매입가", 0) or 0)
        evaluation_amount += quantity * current_price

    total_assets = int(deposit or 0) + evaluation_amount

    (
        gross_realized_pnl,
        net_realized_pnl,
        _,
        _,
    ) = order_repository.get_today_realized_pnl(context=context)

    with context.connect(context.monitoring_db) as con:
        con.execute(
            """
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
        """,
            (
                today,
                int(deposit or 0),
                evaluation_amount,
                total_assets,
                gross_realized_pnl,
                gross_realized_pnl,
                net_realized_pnl,
                context.now_text(),
            ),
        )

    return evaluation_amount, total_assets


def save_strategy_daily_summaries(strategy_names, *, context):
    db_schema.init_monitoring_tables(context=context)
    db_schema.init_position_strategy_table(context=context)

    today = context.clock().strftime("%Y%m%d")
    counts = position_repository.get_strategy_position_counts(context=context)

    with context.connect(context.monitoring_db) as con:
        for strategy_name in strategy_names:
            row = con.execute(
                """
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
            """,
                (
                    f"{today}%",
                    strategy_name,
                ),
            ).fetchone()

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
                net_realized_pnl / invested_amount * 100 if invested_amount else 0.0
            )

            win_rate_pct = winning_trades / trade_count * 100 if trade_count else 0.0

            con.execute(
                """
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
            """,
                (
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
                    context.now_text(),
                ),
            )


def save_strategy_equity_snapshots(strategy_names, snapshot_at=None, *, context):
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
    db_schema.init_monitoring_tables(context=context)

    normalized_names = [
        str(name) for name in dict.fromkeys(strategy_names or []) if str(name).strip()
    ]

    if not normalized_names:
        return []

    now_text = str(snapshot_at or context.now_text())
    snapshot_date = (
        now_text[:8] if len(now_text) >= 8 else context.clock().strftime("%Y%m%d")
    )

    saved = []

    with context.connect(
        context.monitoring_db,
        timeout=15,
    ) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 15000")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")

        for strategy_name in normalized_names:
            open_row = con.execute(
                """
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
            """,
                (strategy_name,),
            ).fetchone()

            realized_row = con.execute(
                """
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
            """,
                (strategy_name,),
            ).fetchone()

            holding_count = int(open_row["holding_count"] or 0)
            open_cost_basis = float(open_row["open_cost_basis"] or 0)
            market_value = float(open_row["market_value"] or 0)
            unrealized_pnl = float(open_row["unrealized_pnl"] or 0)

            cumulative_gross_realized_pnl = float(
                realized_row["gross_realized_pnl"] or 0
            )
            cumulative_fee = float(realized_row["fee"] or 0)
            cumulative_tax = float(realized_row["tax"] or 0)
            cumulative_net_realized_pnl = float(realized_row["net_realized_pnl"] or 0)
            lifetime_sold_cost_basis = float(realized_row["sold_cost_basis"] or 0)

            total_pnl = cumulative_net_realized_pnl + unrealized_pnl

            deployed_capital = lifetime_sold_cost_basis + open_cost_basis

            return_pct = (
                total_pnl / deployed_capital * 100.0 if deployed_capital > 0 else 0.0
            )

            con.execute(
                """
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
            """,
                (
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
                ),
            )

            saved.append(
                {
                    "snapshot_at": now_text,
                    "snapshot_date": snapshot_date,
                    "strategy_name": strategy_name,
                    "holding_count": holding_count,
                    "open_cost_basis": open_cost_basis,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "cumulative_gross_realized_pnl": (cumulative_gross_realized_pnl),
                    "cumulative_fee": cumulative_fee,
                    "cumulative_tax": cumulative_tax,
                    "cumulative_net_realized_pnl": (cumulative_net_realized_pnl),
                    "total_pnl": total_pnl,
                    "lifetime_sold_cost_basis": (lifetime_sold_cost_basis),
                    "deployed_capital": deployed_capital,
                    "return_on_deployed_capital_pct": (return_pct),
                    "return_method": "DEPLOYED_CAPITAL_ROI",
                }
            )

    return saved


def save_position_snapshot(balance, realtime_price_map=None, *, context):
    """현재 실제 보유종목을 monitoring.db.position_snapshot에 저장한다.

    - balance: Kiwoom.balance
    - realtime_price_map: Kiwoom.universe_realtime_transaction_info

    정상 잔고 조회가 확인된 상태에서 StrategyManager가 호출한다.
    현재 보유종목 전체를 한 트랜잭션에서 DELETE + INSERT 하므로
    전량매도된 종목은 다음 스냅샷에서 자동으로 사라진다.
    """
    db_schema.init_monitoring_tables(context=context)
    db_schema.init_position_strategy_table(context=context)

    if balance is None:
        return 0

    realtime_price_map = realtime_price_map or {}
    position_details = position_repository.get_all_position_details(context=context)
    now_text = context.now_text()
    rows = []

    for raw_code, raw_info in dict(balance).items():
        code = str(raw_code).strip().upper().zfill(6)
        info = raw_info or {}

        quantity = int(info.get("보유수량", 0) or 0)
        if quantity <= 0:
            continue

        available_quantity = int(
            info.get("매매가능수량", 0) or info.get("주문가능수량", 0) or quantity
        )

        buy_price = float(info.get("매입가") or info.get("매입단가") or 0)

        rt = realtime_price_map.get(code, {}) or {}
        current_price = float(rt.get("현재가") or info.get("현재가") or buy_price or 0)

        purchase_amount = float(
            info.get("매입금액") or info.get("총매입가") or (buy_price * quantity) or 0
        )

        evaluation_amount = float(max(0, current_price) * max(0, quantity))

        unrealized_pnl = evaluation_amount - purchase_amount

        if purchase_amount > 0:
            return_pct = unrealized_pnl / purchase_amount * 100.0
        else:
            return_pct = float(info.get("수익률", 0) or info.get("손익율", 0) or 0)

        position = position_details.get(code, {})
        strategy_name = position.get(
            "strategy_name"
        ) or position_repository.get_position_strategy(code, context=context)

        code_name = str(info.get("종목명") or position.get("code_name") or code).strip()

        rows.append(
            (
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
            )
        )

    # FastAPI가 동시에 읽더라도 커밋 전의 일관된 이전 스냅샷 또는
    # 커밋 후의 새 스냅샷만 보도록 하나의 트랜잭션으로 교체한다.
    with context.connect(
        context.monitoring_db,
        timeout=15,
    ) as con:
        con.execute("PRAGMA busy_timeout = 15000")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")

        con.execute("DELETE FROM position_snapshot")

        if rows:
            con.executemany(
                """
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
            """,
                rows,
            )

    return len(rows)
