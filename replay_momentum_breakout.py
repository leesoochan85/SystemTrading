"""Replay the momentum paper engine from recorded executions; no Qt/login/orders.

CSV must contain individual executions, not one-minute OHLC bars. Both local
receipt and exchange trade time are retained so stale/out-of-order rejection is
reproduced. Daily history is always cut off before each simulated decision day.
"""
from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import argparse
import csv
import json
import os
import sqlite3
import sys

from strategy.MomentumContractionStrategy import MomentumContractionStrategy
from util.momentum_breakout import load_config
from util.paper_strategy_engine import MomentumPaperEngine
from util.paper_trading import MOMENTUM
from util.paper_tick_recorder import COLUMNS


def parse_timestamp(value):
    if len(value) != 14 or not value.isdigit():
        raise ValueError("timestamps must be YYYYMMDDHHMMSS in Korea time")
    return datetime.strptime(value, "%Y%m%d%H%M%S")


def read_ticks(path):
    """Stream in recorded receipt order; never sort or manufacture executions."""
    previous_received = None
    previous_registered = None
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or any(c not in reader.fieldnames for c in COLUMNS):
            raise ValueError("CSV requires columns: " + ",".join(COLUMNS))
        for line, row in enumerate(reader, 2):
            try:
                received = parse_timestamp(row["received_at"])
                registered = parse_timestamp(row["registered_at"])
                if registered > received or (previous_received and received < previous_received):
                    raise ValueError("receipt order/registration time is invalid")
                if previous_registered and registered < previous_registered:
                    raise ValueError("registration times must not move backwards")
                code = row["code"]
                if len(code) != 6 or not code.isdigit():
                    raise ValueError("code must be six digits")
                price, low = int(row["price"]), int(row["day_low"])
                cumulative, quantity = int(row["cumulative_volume"]), int(row["trade_volume"])
                if min(price, low, cumulative) < 0:
                    raise ValueError("negative price/day-low/cumulative volume")
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"CSV line {line}: {exc}") from exc
            previous_received, previous_registered = received, registered
            # Missing/malformed exchange time and zero volume are deliberately
            # passed through so production feed guards can reject them.
            yield received, registered, code, {"체결시간": row["trade_time"],
                "현재가": price, "저가": low, "누적거래량": cumulative, "거래량": quantity}


def replay(ticks_path, market_db_path, paper_db_path, config_path=None):
    ticks_path, market_db_path, paper_db_path = [Path(p).resolve() for p in
        (ticks_path, market_db_path, paper_db_path)]
    if not market_db_path.is_file():
        raise ValueError("market DB does not exist")
    if paper_db_path.exists() or paper_db_path in (market_db_path, ticks_path):
        raise ValueError("paper DB must be a new separate file; existing files are never overwritten")
    if config_path:
        load_config(config_path)
    # Validate the entire input before creating the output DB, without loading
    # a full day's ticks into memory on a 32-bit process.
    first, count = None, 0
    for event in read_ticks(ticks_path):
        first = first or event
        count += 1
    if not first:
        raise ValueError("CSV has no executions")
    current = [first[1]]
    strategy = MomentumContractionStrategy(SimpleNamespace(), config_path=config_path,
        market_db_path=market_db_path, clock=lambda: current[0])
    strategy.check_and_get_universe()
    if not strategy.universe:
        raise ValueError("market DB has no active company stocks")
    strategy.check_and_get_price_data()
    strategy.is_init_success = True
    strategy.on_realtime_registered()
    paper_db_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation prevents an accidental overwrite/race with another run.
    fd = os.open(str(paper_db_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    engine = MomentumPaperEngine([strategy], paper_db_path, record_ticks=False)
    current[0] = first[0]
    strategy.check_and_get_price_data()  # Persist first-day candidate diagnostics.
    registration, unknown = first[1], 0
    for received, registered, code, tick in read_ticks(ticks_path):
        if received.date() != current[0].date():
            engine.flush_snapshot_if_due(force=True)
        if registered != registration:
            current[0] = registered
            strategy.on_realtime_registered()
            registration = registered
        current[0] = received
        if code not in strategy.universe:
            unknown += 1
            continue
        engine.on_realtime_tick(code, tick)
        # Replay snapshots each observed event; this does not claim production's
        # five-second MDD is identical to an event-by-event replay.
        engine.flush_snapshot_if_due(force=True)
    engine.flush_snapshot_if_due(force=True)
    broker = engine.broker
    with closing(sqlite3.connect(paper_db_path)) as con:
        con.row_factory = sqlite3.Row
        trades = [dict(r) for r in con.execute("SELECT * FROM paper_trade WHERE strategy_name=? ORDER BY trade_id", (MOMENTUM,))]
    account = broker.accounts[MOMENTUM]
    return {"scope": "MOMENTUM_ONLY_TICK_REPLAY", "paper_only": True,
        "input_tick_count": count, "unknown_code_tick_count": unknown,
        "paper_db": str(paper_db_path), "initial_cash": account["initial_cash"],
        "cash": account["cash"], "equity": broker.equity(MOMENTUM),
        "return_pct": (broker.equity(MOMENTUM) / account["initial_cash"] - 1) * 100,
        "total_fees": account["total_fees"], "realized_pnl": account["realized_pnl"],
        "observed_max_drawdown_pct": account["max_drawdown_pct"],
        "positions": [p for (name, _), p in broker.positions.items() if name == MOMENTUM],
        "trades": trades, "last_candidate_stats": strategy.stats,
        "last_runtime": strategy.runtime,
        "limitations": ["Individual recorded executions required; OHLC bars cannot establish intrabar order.",
            "Historical stock master membership and corporate-action adjustments are supplied by the user DB.",
            "Model fills do not reproduce order-book liquidity, partial fills, or exchange tick sizes.",
            "Event-by-event replay MDD differs from production five-second sampling; this is not proof of future returns."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", required=True, type=Path)
    parser.add_argument("--market-db", required=True, type=Path)
    parser.add_argument("--paper-db", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        with redirect_stdout(sys.stderr):
            result = replay(args.ticks, args.market_db, args.paper_db, args.config)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
