"""Independent, cash-backed paper accounts. Never calls Kiwoom or SendOrder.

Entries/exits use the observed tick plus configured slippage, not an ideal
breakout/stop price. Cash, integer lots, fees, partial state and fills commit
together. Market marks are batched by the enclosing engine every five seconds.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import math
import sqlite3

from util.position_sizing import stop_risk_quantity


BASELINE = "HighBreakoutBaselinePaper"
FILTERED = "HighBreakoutFilteredPaper"
MOMENTUM = "MomentumContractionStrategy"
PROFILES = (BASELINE, FILTERED, MOMENTUM)


class PaperBroker:
    def __init__(self, db_path, config, now=None):
        self.db_path = Path(db_path).resolve()
        self.config = config
        self.costs = config["account"]
        self.positions = {}
        self.accounts = {}
        self.entered_today = set()
        self.guard_date = None
        self._init(now or datetime.now())

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(str(self.db_path), timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 15000")
        try:
            yield con
        finally:
            con.close()

    def _init(self, now):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS paper_account (
                    strategy_name TEXT PRIMARY KEY, initial_cash REAL NOT NULL,
                    cash REAL NOT NULL, total_fees REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0, peak_equity REAL NOT NULL,
                    max_drawdown_pct REAL NOT NULL DEFAULT 0,
                    config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_position (
                    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL, code TEXT NOT NULL, code_name TEXT,
                    initial_quantity INTEGER NOT NULL, quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL, entry_at TEXT NOT NULL,
                    current_price REAL NOT NULL, state_json TEXT NOT NULL,
                    buy_reason TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(strategy_name, code)
                );
                CREATE TABLE IF NOT EXISTS paper_trade (
                    trade_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL,
                    strategy_name TEXT NOT NULL, code TEXT NOT NULL, code_name TEXT,
                    event_type TEXT NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL,
                    fee REAL NOT NULL, net_pnl REAL NOT NULL, reason_code TEXT NOT NULL,
                    entry_price REAL NOT NULL, entry_at TEXT NOT NULL, event_at TEXT NOT NULL,
                    condition_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trade_profile_time ON paper_trade(strategy_name,event_at);
                CREATE TABLE IF NOT EXISTS paper_equity (
                    strategy_name TEXT NOT NULL, snapshot_date TEXT NOT NULL,
                    equity REAL NOT NULL, cash REAL NOT NULL, position_value REAL NOT NULL,
                    open_position_count INTEGER NOT NULL, cumulative_return_pct REAL NOT NULL,
                    daily_return_pct REAL NOT NULL, peak_equity REAL NOT NULL,
                    drawdown_pct REAL NOT NULL, total_fees REAL NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_name,snapshot_date)
                );
                CREATE TABLE IF NOT EXISTS paper_watchlist (
                    snapshot_date TEXT NOT NULL, code TEXT NOT NULL, code_name TEXT,
                    eligible INTEGER NOT NULL, momentum_rank INTEGER NOT NULL,
                    metric_json TEXT NOT NULL, current_price REAL, opening_high REAL,
                    day_low REAL, last_reason TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY(snapshot_date,code)
                );
                CREATE TABLE IF NOT EXISTS paper_run_status (
                    snapshot_date TEXT PRIMARY KEY, stats_json TEXT NOT NULL,
                    settings_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            columns = {r[1] for r in con.execute("PRAGMA table_info(paper_account)")}
            if "max_drawdown_pct" not in columns:
                con.execute("ALTER TABLE paper_account ADD COLUMN max_drawdown_pct REAL NOT NULL DEFAULT 0")
                con.execute("""UPDATE paper_account SET max_drawdown_pct=COALESCE(
                    (SELECT MIN(drawdown_pct) FROM paper_equity WHERE paper_equity.strategy_name=paper_account.strategy_name),0)""")
            config_text = json.dumps(self.config, sort_keys=True, ensure_ascii=False)
            stamp = now.strftime("%Y%m%d%H%M%S")
            with con:
                for profile in PROFILES:
                    con.execute("""INSERT OR IGNORE INTO paper_account
                        (strategy_name,initial_cash,cash,total_fees,realized_pnl,peak_equity,config_json,created_at,updated_at)
                        VALUES (?,?,?,0,0,?,?,?,?)""", (profile, self.costs["initial_cash"], self.costs["initial_cash"],
                            self.costs["initial_cash"], config_text, stamp, stamp))
                    row = con.execute("SELECT * FROM paper_account WHERE strategy_name=?", (profile,)).fetchone()
                    if json.loads(row["config_json"]) != self.config:
                        raise ValueError("가상 계좌 설정이 이전 실행과 다릅니다. 별도 실험 폴더/DB를 사용하세요.")
                    self.accounts[profile] = dict(row)
            for row in con.execute("SELECT * FROM paper_position"):
                position = dict(row)
                position["state"] = json.loads(position.pop("state_json"))
                self.positions[(position["strategy_name"], position["code"])] = position
        self._refresh_guard(now)

    def _refresh_guard(self, now):
        day = now.strftime("%Y%m%d")
        if self.guard_date == day:
            return
        with self._connect() as con:
            self.entered_today = {(r[0], r[1]) for r in con.execute(
                "SELECT DISTINCT strategy_name,code FROM paper_trade WHERE event_type='BUY' AND substr(event_at,1,8)=?", (day,))}
        self.guard_date = day

    def equity(self, profile):
        return self.accounts[profile]["cash"] + sum(p["quantity"] * p["current_price"] for (name, _), p in self.positions.items() if name == profile)

    def mark(self, code, price, now):
        for (_, position_code), position in self.positions.items():
            if position_code == code:
                position["current_price"] = float(price)
                position["updated_at"] = now.strftime("%Y%m%d%H%M%S")

    def buy_price(self, observed):
        return float(math.ceil(observed * (1 + self.costs["slippage_bps"] / 10000)))

    def sell_price(self, observed):
        return float(max(1, math.floor(observed * (1 - self.costs["slippage_bps"] / 10000))))

    def buy(self, profile, code, code_name, observed, stop, state, reason, data, now, max_stop_distance=None):
        self._refresh_guard(now)
        code = str(code).zfill(6)
        key = (profile, code)
        if key in self.positions:
            return None, "ALREADY_HELD"
        if key in self.entered_today:
            return None, "SAME_DAY_REENTRY"
        count = sum(name == profile for name, _ in self.positions)
        if count >= self.costs["max_positions"]:
            return None, "POSITION_LIMIT"
        price = self.buy_price(observed)
        if not math.isfinite(stop) or stop <= 0 or price <= stop:
            return None, "INVALID_STOP"
        if max_stop_distance is not None and price - stop > max_stop_distance:
            return None, "STOP_TOO_WIDE_AFTER_SLIPPAGE"
        assets = self.equity(profile)
        account = self.accounts[profile]
        unit_cost = price * (1 + self.costs["buy_fee_rate"])
        risk_qty = stop_risk_quantity(assets, price, self.sell_price(stop), self.costs["risk_ratio"],
                                     self.costs["buy_fee_rate"], self.costs["sell_fee_rate"] + self.costs["sell_tax_rate"])
        qty = min(risk_qty, math.floor(min(account["cash"], assets * self.costs["max_position_ratio"]) / unit_cost))
        if qty < 1:
            return None, "INSUFFICIENT_CASH_OR_RISK"
        fee = qty * price * self.costs["buy_fee_rate"]
        amount = qty * price + fee
        stamp = now.strftime("%Y%m%d%H%M%S")
        filled_state = dict(state, stop_price=float(stop), entry_price=price)
        # ATR entry state must use the modeled fill, not the pre-slippage tick.
        if profile != MOMENTUM:
            filled_state["initial_stop"] = float(stop)
        with self._connect() as con, con:
            con.execute("BEGIN IMMEDIATE")
            stored = con.execute("SELECT cash FROM paper_account WHERE strategy_name=?", (profile,)).fetchone()
            stored_count = con.execute("SELECT COUNT(*) FROM paper_position WHERE strategy_name=?", (profile,)).fetchone()[0]
            if abs(stored[0] - account["cash"]) > 0.01 or stored_count != count:
                return None, "ACCOUNT_CHANGED_RESTART_REQUIRED"
            duplicate = con.execute("SELECT 1 FROM paper_trade WHERE strategy_name=? AND code=? AND event_type='BUY' AND substr(event_at,1,8)=? LIMIT 1", (profile, code, stamp[:8])).fetchone()
            if duplicate:
                return None, "SAME_DAY_REENTRY"
            cursor = con.execute("""INSERT INTO paper_position
                (strategy_name,code,code_name,initial_quantity,quantity,entry_price,entry_at,current_price,state_json,buy_reason,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (profile, code, code_name, qty, qty, price, stamp, observed,
                    json.dumps(filled_state, ensure_ascii=False), reason, stamp))
            position_id = cursor.lastrowid
            con.execute("""INSERT INTO paper_trade
                (position_id,strategy_name,code,code_name,event_type,quantity,price,fee,net_pnl,reason_code,entry_price,entry_at,event_at,condition_json)
                VALUES (?,?,?,?,'BUY',?,?,?,0,?,?,?,?,?)""", (position_id, profile, code, code_name, qty, price, fee, reason, price, stamp, stamp, json.dumps(data, ensure_ascii=False)))
            con.execute("UPDATE paper_account SET cash=cash-?,total_fees=total_fees+?,updated_at=? WHERE strategy_name=?", (amount, fee, stamp, profile))
        position = dict(position_id=position_id, strategy_name=profile, code=code, code_name=code_name,
                        initial_quantity=qty, quantity=qty, entry_price=price, entry_at=stamp,
                        current_price=float(observed), state=filled_state, buy_reason=reason, updated_at=stamp)
        self.positions[key] = position
        self.entered_today.add(key)
        account["cash"] -= amount
        account["total_fees"] += fee
        return position, None

    def update_state(self, position, state):
        if position["state"] == state:
            return
        if not math.isfinite(state.get("stop_price", 0)) or state.get("stop_price", 0) < position["state"]["stop_price"]:
            raise ValueError("paper stop cannot move down")
        with self._connect() as con, con:
            con.execute("UPDATE paper_position SET state_json=? WHERE position_id=?", (json.dumps(state, ensure_ascii=False), position["position_id"]))
        position["state"] = dict(state)

    def sell(self, position, observed, quantity, reason, now, partial=False):
        qty = min(int(quantity), position["quantity"])
        if qty < 1:
            return False
        profile, code = position["strategy_name"], position["code"]
        price = self.sell_price(observed)
        fee = qty * price * (self.costs["sell_fee_rate"] + self.costs["sell_tax_rate"])
        proceeds = qty * price - fee
        net_pnl = proceeds - qty * position["entry_price"] * (1 + self.costs["buy_fee_rate"])
        stamp = now.strftime("%Y%m%d%H%M%S")
        remaining = position["quantity"] - qty
        state = dict(position["state"])
        if partial and remaining:
            denominator = (1 - self.costs["sell_fee_rate"] - self.costs["sell_tax_rate"]) * (1 - self.costs["slippage_bps"] / 10000)
            break_even = math.ceil(position["entry_price"] * (1 + self.costs["buy_fee_rate"]) / denominator)
            state.update(partial_done=True, stop_price=max(state["stop_price"], break_even))
        account = self.accounts[profile]
        with self._connect() as con, con:
            con.execute("BEGIN IMMEDIATE")
            stored = con.execute("SELECT quantity FROM paper_position WHERE position_id=?", (position["position_id"],)).fetchone()
            cash = con.execute("SELECT cash FROM paper_account WHERE strategy_name=?", (profile,)).fetchone()[0]
            if not stored or stored[0] != position["quantity"] or abs(cash - account["cash"]) > 0.01:
                raise RuntimeError("paper account changed in another writer; restart before trading")
            con.execute("""INSERT INTO paper_trade
                (position_id,strategy_name,code,code_name,event_type,quantity,price,fee,net_pnl,reason_code,entry_price,entry_at,event_at,condition_json)
                VALUES (?,?,?,?,'SELL',?,?,?,?,?,?,?,?,?)""", (position["position_id"], profile, code, position["code_name"], qty, price, fee,
                    net_pnl, reason, position["entry_price"], position["entry_at"], stamp, json.dumps(state, ensure_ascii=False)))
            if remaining:
                con.execute("UPDATE paper_position SET quantity=?,current_price=?,state_json=?,updated_at=? WHERE position_id=?", (remaining, observed, json.dumps(state, ensure_ascii=False), stamp, position["position_id"]))
            else:
                con.execute("DELETE FROM paper_position WHERE position_id=?", (position["position_id"],))
            con.execute("UPDATE paper_account SET cash=cash+?,total_fees=total_fees+?,realized_pnl=realized_pnl+?,updated_at=? WHERE strategy_name=?", (proceeds, fee, net_pnl, stamp, profile))
        account["cash"] += proceeds
        account["total_fees"] += fee
        account["realized_pnl"] += net_pnl
        if remaining:
            position.update(quantity=remaining, current_price=float(observed), state=state, updated_at=stamp)
        else:
            self.positions.pop((profile, code), None)
        return True

    def save_watchlist(self, day, metrics, universe, stats, now):
        stamp = now.strftime("%Y%m%d%H%M%S")
        with self._connect() as con, con:
            con.execute("DELETE FROM paper_watchlist WHERE snapshot_date=?", (day,))
            con.executemany("""INSERT INTO paper_watchlist
                (snapshot_date,code,code_name,eligible,momentum_rank,metric_json,last_reason,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""", [(day, code, universe.get(code, {}).get("code_name", code), int(m["eligible"]), m["momentum_rank"],
                    json.dumps(m, ensure_ascii=False), m["rejection_reason"] or "WAIT_OPENING_RANGE", stamp) for code, m in metrics.items()])
            con.execute("INSERT OR REPLACE INTO paper_run_status VALUES (?,?,?,?)", (day, json.dumps(stats, ensure_ascii=False), json.dumps(self.config, ensure_ascii=False), stamp))

    def snapshot(self, now, watch_runtime=None):
        day, stamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%d%H%M%S")
        with self._connect() as con, con:
            con.executemany("UPDATE paper_position SET current_price=?,updated_at=? WHERE position_id=?", [(p["current_price"], p["updated_at"], p["position_id"]) for p in self.positions.values()])
            for profile, account in self.accounts.items():
                nav = self.equity(profile)
                account["peak_equity"] = max(account["peak_equity"], nav)
                drawdown = (nav / account["peak_equity"] - 1) * 100
                account["max_drawdown_pct"] = min(account["max_drawdown_pct"], drawdown)
                previous = con.execute("SELECT equity FROM paper_equity WHERE strategy_name=? AND snapshot_date<? ORDER BY snapshot_date DESC LIMIT 1", (profile, day)).fetchone()
                prior_nav = previous[0] if previous else account["initial_cash"]
                count = sum(name == profile for name, _ in self.positions)
                con.execute("INSERT OR REPLACE INTO paper_equity VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (profile, day, nav, account["cash"], nav - account["cash"], count,
                    (nav / account["initial_cash"] - 1) * 100, (nav / prior_nav - 1) * 100,
                    account["peak_equity"], drawdown, account["total_fees"], stamp))
                con.execute("UPDATE paper_account SET peak_equity=?,max_drawdown_pct=?,updated_at=? WHERE strategy_name=?", (account["peak_equity"], account["max_drawdown_pct"], stamp, profile))
            if watch_runtime:
                con.executemany("""UPDATE paper_watchlist SET current_price=?,opening_high=?,day_low=?,last_reason=?,updated_at=?
                    WHERE snapshot_date=? AND code=?""", [(v.get("price"), v.get("opening_high"), v.get("day_low"), v.get("reason"), stamp, day, code) for code, v in watch_runtime.items()])
        return len(self.positions)
