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
    position_counts = get_all_position_counts()
    account_equity = get_latest_account_equity()
    today = datetime.now().strftime("%Y%m%d")

    strategies = []

    for config in get_all_strategy_configs():
        strategy_name = config["strategy_name"]

        strategies.append(
            {
                **config,
                "holding_count": int(
                    position_counts.get(
                        strategy_name,
                        0,
                    )
                ),
                "performance": get_strategy_performance(
                    strategy_name
                ),
                "equity_snapshot": get_latest_strategy_equity(
                    strategy_name
                ),
                "latest_daily_summary": (
                    get_latest_strategy_daily_summary(
                        strategy_name
                    )
                ),
                "today_signals": get_signal_summary(
                    strategy_name,
                    signal_date=today,
                ),
            }
        )

    return {
        "as_of": datetime.now().isoformat(
            timespec="seconds"
        ),
        "account_equity": account_equity,
        "strategies": strategies,
        "return_note": (
            "performance.realized_return_pct는 매도 완료 거래의 "
            "매입원금 합계 대비 순실현손익률이며 전략 NAV 누적수익률이 아닙니다."
        ),
    }


@app.get("/api/strategies")
def strategies():
    position_counts = get_all_position_counts()

    result = []

    for config in get_all_strategy_configs():
        strategy_name = config["strategy_name"]

        result.append(
            {
                **config,
                "holding_count": int(
                    position_counts.get(
                        strategy_name,
                        0,
                    )
                ),
                "performance": get_strategy_performance(
                    strategy_name
                ),
                "equity_snapshot": get_latest_strategy_equity(
                    strategy_name
                ),
            }
        )

    return {"items": result}


@app.get("/api/strategies/{strategy_name}")
def strategy_detail(strategy_name: str):
    config = _require_strategy(strategy_name)

    return {
        **config,
        "performance": get_strategy_performance(
            strategy_name
        ),
        "equity_snapshot": get_latest_strategy_equity(
            strategy_name
        ),
        "latest_daily_summary": (
            get_latest_strategy_daily_summary(
                strategy_name
            )
        ),
        "positions": get_strategy_positions(
            strategy_name
        ),
        "signal_summary": get_signal_summary(
            strategy_name
        ),
        "position_snapshot": get_position_snapshot_status(),
        "position_price_note": (
            "position_snapshot이 저장된 경우 Kiwoom 실시간 현재가와 "
            "평균매입가 기준 평가금액/평가손익/수익률을 제공합니다."
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

