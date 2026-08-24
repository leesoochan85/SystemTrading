from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .database import (
    DatabaseNotReadyError,
    get_all_position_counts,
    get_db_status,
    get_latest_account_equity,
    get_latest_strategy_daily_summary,
    get_signal_summary,
    get_strategy_performance,
    get_strategy_positions,
    get_position_snapshot_status,
    get_latest_strategy_equity,
    get_strategy_equity_history,
    get_strategy_signals,
    get_strategy_trades,
    get_virtual_positions,
    get_virtual_trades,
    get_virtual_performance,
    get_virtual_daily_performance,
    get_virtual_strategy_index_history,
    get_virtual_event_summary,
    get_virtual_events,
)
from .strategies import (
    get_all_strategy_configs,
    get_strategy_config,
)


app = FastAPI(
    title="SystemTrading Monitoring API",
    version="0.1.0",
    description=(
        "Kiwoom 자동매매 프로세스가 저장한 SQLite DB를 "
        "읽기 전용으로 조회하는 웹 모니터링 API"
    ),
)

# React/Vite 개발 서버와 분리해서 실행할 것을 전제로 한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(DatabaseNotReadyError)
async def database_not_ready_handler(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "db_status": get_db_status(),
        },
    )


def _require_strategy(strategy_name: str) -> dict:
    config = get_strategy_config(strategy_name)

    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"지원하지 않는 전략입니다: {strategy_name}",
        )

    return config


@app.get("/")
def root():
    return {
        "service": "SystemTrading Monitoring API",
        "status": "ok",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    status = get_db_status()

    return {
        "status": (
            "ok"
            if (
                status["monitoring_db"]["exists"]
                and status["position_db"]["exists"]
            )
            else "degraded"
        ),
        "server_time": datetime.now().isoformat(
            timespec="seconds"
        ),
        **status,
    }


@app.get("/api/dashboard")
def dashboard():
    today = datetime.now().strftime("%Y%m%d")
    strategies = []

    for config in get_all_strategy_configs():
        strategy_name = config["strategy_name"]
        strategies.append(
            {
                **config,
                "virtual_performance": get_virtual_performance(strategy_name),
                "today_virtual_events": get_virtual_event_summary(
                    strategy_name, event_date=today
                ),
            }
        )

    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "strategies": strategies,
        "metric_note": (
            "승률/평균수익률/최고/최저는 가상 BUY 후 100% 청산이 완료된 "
            "거래를 기준으로 계산합니다. 부분청산만 진행된 포지션은 완료 거래에 포함하지 않습니다."
        ),
    }


@app.get("/api/strategies")
def strategies():
    today = datetime.now().strftime("%Y%m%d")
    result = []

    for config in get_all_strategy_configs():
        strategy_name = config["strategy_name"]
        result.append(
            {
                **config,
                "virtual_performance": get_virtual_performance(strategy_name),
                "today_virtual_events": get_virtual_event_summary(
                    strategy_name, event_date=today
                ),
            }
        )

    return {"items": result}


@app.get("/api/strategies/{strategy_name}")
def strategy_detail(strategy_name: str):
    config = _require_strategy(strategy_name)
    today = datetime.now().strftime("%Y%m%d")

    return {
        **config,
        "virtual_performance": get_virtual_performance(strategy_name),
        "virtual_positions": get_virtual_positions(strategy_name),
        "today_virtual_events": get_virtual_event_summary(
            strategy_name, event_date=today
        ),
    }


@app.get("/api/strategies/{strategy_name}/positions")
def strategy_positions(strategy_name: str):
    _require_strategy(strategy_name)

    items = get_strategy_positions(
        strategy_name
    )
    snapshot_status = get_position_snapshot_status()

    return {
        "strategy_name": strategy_name,
        "items": items,
        "live_price_available": any(
            bool(item.get("live_price_available"))
            for item in items
        ),
        "snapshot_status": snapshot_status,
        "note": (
            "position_snapshot이 있으면 5초 주기의 최신 보유현황을 사용하고, "
            "아직 생성되지 않았다면 strategy_position.db로 자동 fallback합니다."
        ),
    }


@app.get("/api/strategies/{strategy_name}/trades")
def strategy_trades(
    strategy_name: str,
    limit: int = Query(
        default=200,
        ge=1,
        le=1000,
    ),
    offset: int = Query(
        default=0,
        ge=0,
    ),
):
    _require_strategy(strategy_name)

    return {
        "strategy_name": strategy_name,
        "items": get_strategy_trades(
            strategy_name,
            limit=limit,
            offset=offset,
        ),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/strategies/{strategy_name}/signals")
def strategy_signals(
    strategy_name: str,
    signal_type: Optional[Literal["BUY", "SELL"]] = None,
    signal_date: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="YYYYMMDD",
    ),
    order_result: Optional[
        Literal[
            "ORDER_SENT",
            "BLOCKED",
            "ORDER_FAILED",
        ]
    ] = None,
    limit: int = Query(
        default=200,
        ge=1,
        le=1000,
    ),
    offset: int = Query(
        default=0,
        ge=0,
    ),
):
    _require_strategy(strategy_name)

    return {
        "strategy_name": strategy_name,
        "filters": {
            "signal_type": signal_type,
            "signal_date": signal_date,
            "order_result": order_result,
        },
        "items": get_strategy_signals(
            strategy_name=strategy_name,
            signal_type=signal_type,
            signal_date=signal_date,
            order_result=order_result,
            limit=limit,
            offset=offset,
        ),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/strategies/{strategy_name}/equity")
def strategy_equity(strategy_name: str):
    _require_strategy(strategy_name)

    return {
        "strategy_name": strategy_name,
        "item": get_latest_strategy_equity(
            strategy_name
        ),
        "metric_definition": {
            "total_pnl": (
                "누적 순실현손익 + 현재 미실현손익"
            ),
            "deployed_capital": (
                "누적 매도수량의 매입원금 + 현재 보유 매입원금"
            ),
            "return_on_deployed_capital_pct": (
                "total_pnl / deployed_capital * 100"
            ),
        },
        "nav_warning": (
            "현재 계좌는 전략별 현금을 분리하지 않으므로 "
            "이 수익률은 독립 전략계좌 NAV 수익률이 아니라 "
            "실제 배치 매입원금 대비 누적 손익률입니다."
        ),
    }


@app.get("/api/strategies/{strategy_name}/equity-history")
def strategy_equity_history(
    strategy_name: str,
    date_from: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="시작일 YYYYMMDD",
    ),
    date_to: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="종료일 YYYYMMDD",
    ),
    limit: int = Query(
        default=2000,
        ge=1,
        le=10000,
    ),
):
    _require_strategy(strategy_name)

    items = get_strategy_equity_history(
        strategy_name=strategy_name,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )

    return {
        "strategy_name": strategy_name,
        "filters": {
            "date_from": date_from,
            "date_to": date_to,
        },
        "items": items,
        "limit": limit,
        "chart_fields": {
            "x": "snapshot_at",
            "return_pct": (
                "return_on_deployed_capital_pct"
            ),
            "total_pnl": "total_pnl",
            "market_value": "market_value",
        },
        "nav_warning": (
            "return_on_deployed_capital_pct는 "
            "전략별 공용현금을 임의 배분하지 않은 "
            "실제 배치자본 기준 성과지표입니다."
        ),
    }



@app.get("/api/virtual/events")
def virtual_events(
    strategy_name: str,
    limit: int = Query(default=100, ge=1, le=1000),
):
    _require_strategy(strategy_name)
    return {
        "strategy_name": strategy_name,
        "items": get_virtual_events(strategy_name, limit=limit),
        "limit": limit,
    }


@app.get("/api/virtual/positions")
def virtual_positions(strategy_name: Optional[str] = None):
    if strategy_name:
        _require_strategy(strategy_name)
    return {"items": get_virtual_positions(strategy_name)}


@app.get("/api/virtual/trades")
def virtual_trades(
    strategy_name: Optional[str] = None,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    if strategy_name:
        _require_strategy(strategy_name)
    return {
        "items": get_virtual_trades(strategy_name, limit=limit, offset=offset),
        "limit": limit,
        "offset": offset,
    }



@app.get("/api/strategies/{strategy_name}/virtual-index-history")
def strategy_virtual_index_history(
    strategy_name: str,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{8}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{8}$"),
):
    _require_strategy(strategy_name)
    result = get_virtual_strategy_index_history(
        strategy_name=strategy_name,
        date_from=date_from,
        date_to=date_to,
    )
    return {
        "strategy_name": strategy_name,
        **result,
        "warning": (
            "실제 전략별 현금계좌 NAV가 아니라 각 가상 BUY를 동일 투자금 1단위로 "
            "mark-to-market한 성과지수입니다. MDD는 일별 스냅샷 저장 시작 이후 구간을 "
            "중심으로 해석하세요."
        ),
    }


@app.get("/api/strategies/{strategy_name}/virtual-daily-performance")
def strategy_virtual_daily_performance(
    strategy_name: str,
    date_from: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="시작일 YYYYMMDD",
    ),
    date_to: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="종료일 YYYYMMDD",
    ),
):
    _require_strategy(strategy_name)

    result = get_virtual_daily_performance(
        strategy_name=strategy_name,
        date_from=date_from,
        date_to=date_to,
    )

    return {
        "strategy_name": strategy_name,
        **result,
        "metric_definition": (
            "daily_return_pct = 해당 날짜 가상 SELL leg의 "
            "weighted_return_pct 합 / 청산비중(exit_ratio) 합"
        ),
        "warning": (
            "현재 가상전략은 독립 현금/NAV를 운용하지 않으므로 "
            "이 값은 일간 NAV 수익률이 아니라 그날 청산된 "
            "가상 포지션의 비중가중 평균 실현수익률입니다. "
            "미실현 포지션은 포함하지 않습니다."
        ),
    }


@app.get("/api/strategies/{strategy_name}/virtual-performance")
def strategy_virtual_performance(strategy_name: str):
    _require_strategy(strategy_name)
    return {
        "strategy_name": strategy_name,
        "performance": get_virtual_performance(strategy_name),
        "positions": get_virtual_positions(strategy_name),
    }
