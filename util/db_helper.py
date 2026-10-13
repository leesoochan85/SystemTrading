"""Backward-compatible database API; implementation lives in focused repositories.

MONITORING_DB and POSITION_DB remain configurable on this module. Each call
captures their current values and shares one context across nested operations.
"""

import json
import sqlite3
from datetime import datetime

from util.db_context import RepositoryContext
from util import (
    db_schema,
    position_repository,
    order_repository,
    performance_repository,
)

MONITORING_DB = "monitoring.db"
POSITION_DB = "strategy_position.db"


def _now_text():
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _context():
    return RepositoryContext(
        monitoring_db=MONITORING_DB,
        position_db=POSITION_DB,
        clock=lambda: datetime.now(),
        timestamp=_now_text,
    )


def init_position_strategy_table():
    return db_schema.init_position_strategy_table(context=_context())


def save_position_strategy(code, code_name, strategy_name, quantity, buy_price):
    return position_repository.save_position_strategy(
        code, code_name, strategy_name, quantity, buy_price, context=_context()
    )


def get_position_strategy(code):
    return position_repository.get_position_strategy(code, context=_context())


def get_position_detail(code):
    return position_repository.get_position_detail(code, context=_context())


def get_all_position_details():
    """strategy_position.db의 전체 전략 포지션 정보를 코드 기준 딕셔너리로 반환한다."""
    return position_repository.get_all_position_details(context=_context())


def delete_position_strategy(code):
    return position_repository.delete_position_strategy(code, context=_context())


def update_position_from_buy_fill(
    code, code_name, strategy_name, fill_quantity, fill_price
):
    """실제 매수 체결 수량과 체결가를 기준으로"""
    return position_repository.update_position_from_buy_fill(
        code, code_name, strategy_name, fill_quantity, fill_price, context=_context()
    )


def _migrate_old_order_log_if_needed(con):
    return db_schema._migrate_old_order_log_if_needed(con, context=_context())


def _add_column_if_missing(con, table_name, column_name, column_type):
    return db_schema._add_column_if_missing(
        con, table_name, column_name, column_type, context=_context()
    )


def init_monitoring_tables():
    return db_schema.init_monitoring_tables(context=_context())


def _json_dumps_safe(value):
    """전략 조건 스냅샷을 한글이 깨지지 않는 JSON 문자열로 변환한다."""
    return order_repository._json_dumps_safe(value, context=_context())


def build_strategy_signal_key(
    strategy_name, code, signal_type, reason_code, signal_date=None
):
    return order_repository.build_strategy_signal_key(
        strategy_name, code, signal_type, reason_code, signal_date, context=_context()
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
):
    """전략 조건 충족을 monitoring.db에 저장한다."""
    return order_repository.save_strategy_signal(
        strategy_name,
        code,
        code_name,
        signal_type,
        reason_code,
        signal_reason,
        current_price,
        condition_data,
        order_attempted,
        order_result,
        blocked_reason,
        detected_at,
        context=_context(),
    )


def update_strategy_signal_order_result(
    signal_key, order_attempted, order_result, blocked_reason=None
):
    """이미 저장된 전략 신호에 실제 주문 시도/결과를 연결한다."""
    return order_repository.update_strategy_signal_order_result(
        signal_key, order_attempted, order_result, blocked_reason, context=_context()
    )


def get_strategy_signals(
    strategy_name=None, signal_type=None, signal_date=None, limit=500
):
    """향후 FastAPI에서 바로 사용할 수 있는 전략 신호 조회 함수."""
    return order_repository.get_strategy_signals(
        strategy_name, signal_type, signal_date, limit, context=_context()
    )


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
):
    return order_repository.save_order_event(
        event_key,
        code,
        code_name,
        order_type,
        strategy_name,
        order_quantity,
        order_price,
        remaining_quantity,
        filled_quantity,
        filled_price,
        order_status,
        event_type,
        order_no,
        original_order_no,
        request_id,
        message,
        context=_context(),
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
):
    """키움 체잔 응답을 주문 이벤트 이력으로 보존한다."""
    return order_repository.save_order_log(
        order_no,
        code,
        code_name,
        order_type,
        strategy_name,
        order_quantity,
        order_price,
        remaining_quantity,
        order_status,
        filled_quantity,
        filled_price,
        fill_no,
        context=_context(),
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
    """체결번호 기준으로 체결을 저장한다."""
    return order_repository.save_trade_fill(
        fill_no,
        order_no,
        code,
        code_name,
        order_type,
        strategy_name,
        quantity,
        price,
        buy_price,
        buy_date,
        fee,
        tax,
        context=_context(),
    )


def update_latest_sell_trade_costs(code, fee=None, tax=None):
    """잔고 체잔 이벤트로 비용 정보가 늦게 도착했을 때,"""
    return order_repository.update_latest_sell_trade_costs(
        code, fee, tax, context=_context()
    )


def get_filled_position_codes():
    """실제 매수 체결 이력이 있는 전략 포지션 코드 목록을 반환한다."""
    return position_repository.get_filled_position_codes(context=_context())


def get_strategy_position_counts():
    return position_repository.get_strategy_position_counts(context=_context())


def get_today_order_count():
    return order_repository.get_today_order_count(context=_context())


def get_today_realized_pnl():
    """오늘의 총손익과 순손익을 반환한다."""
    return order_repository.get_today_realized_pnl(context=_context())


def save_daily_equity(deposit, balance):
    return performance_repository.save_daily_equity(
        deposit, balance, context=_context()
    )


def save_strategy_daily_summaries(strategy_names):
    return performance_repository.save_strategy_daily_summaries(
        strategy_names, context=_context()
    )


def reduce_position_from_sell_fill(code, fill_quantity):
    """실제 매도 체결 수량만큼 position_strategy 수량을 감소시킨다."""
    return position_repository.reduce_position_from_sell_fill(
        code, fill_quantity, context=_context()
    )


def save_strategy_equity_snapshots(strategy_names, snapshot_at=None):
    """전략별 실제 거래 기반 성과 스냅샷을 저장한다."""
    return performance_repository.save_strategy_equity_snapshots(
        strategy_names, snapshot_at, context=_context()
    )


def save_position_snapshot(balance, realtime_price_map=None):
    """현재 실제 보유종목을 monitoring.db.position_snapshot에 저장한다."""
    return performance_repository.save_position_snapshot(
        balance, realtime_price_map, context=_context()
    )
