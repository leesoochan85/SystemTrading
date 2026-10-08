"""Broker execution adapter for momentum; A/B/C remain independent paper accounts."""
from __future__ import annotations
import json
import math
import sqlite3
import time
from pathlib import Path
from contextlib import closing
from datetime import datetime, timedelta
from strategy.MomentumContractionStrategy import MomentumContractionStrategy
from util.momentum_breakout import momentum_exit
from util.db_helper import get_position_strategy, save_position_strategy, save_strategy_signal, update_strategy_signal_order_result
from util.time_helper import check_transaction_open, get_market_open_time
from util.notifier import send_message


class LiveMomentumStore:
    """Account-scoped state and idempotent fill journal, committed together."""
    def __init__(self, path, account):
        self.path = Path(path)
        self.account = str(account)
        if not self.account:
            raise ValueError("모멘텀 실거래 계좌 식별자 없음")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as con, con:
            con.executescript('''CREATE TABLE IF NOT EXISTS momentum_live_state (
                account TEXT NOT NULL, code TEXT NOT NULL, state_json TEXT NOT NULL,
                PRIMARY KEY(account,code));
                CREATE TABLE IF NOT EXISTS momentum_live_fill (
                account TEXT NOT NULL, fill_key TEXT NOT NULL, PRIMARY KEY(account,fill_key));''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def load(self):
        with closing(self.connect()) as con:
            return {code: json.loads(raw) for code, raw in con.execute(
                "SELECT code,state_json FROM momentum_live_state WHERE account=?", (self.account,))}

    def save(self, code, state):
        with closing(self.connect()) as con, con:
            con.execute("INSERT OR REPLACE INTO momentum_live_state VALUES (?,?,?)",
                        (self.account, code, json.dumps(state, allow_nan=False)))

    def apply_fill(self, code, fill_key, update):
        with closing(self.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            inserted = con.execute("INSERT OR IGNORE INTO momentum_live_fill VALUES (?,?)", (self.account, fill_key))
            if not inserted.rowcount:
                return None
            row = con.execute("SELECT state_json FROM momentum_live_state WHERE account=? AND code=?", (self.account, code)).fetchone()
            if row is None:
                raise RuntimeError("실체결에 대응하는 모멘텀 손절 상태 없음")
            state = update(json.loads(row[0]))
            con.execute("UPDATE momentum_live_state SET state_json=? WHERE account=? AND code=?",
                        (json.dumps(state, allow_nan=False), self.account, code))
            return state


class LiveMomentumContractionStrategy(MomentumContractionStrategy):
    paper_only = False
    legacy_virtual = False  # C is evaluated by PaperComparisonEngine, not the old index engine.

    def __init__(self, kiwoom, config_path=None, market_db_path=None, clock=None, live_db_path=None, allow_new_buys=False):
        args = {} if clock is None else {"clock": clock}
        super().__init__(kiwoom, config_path, market_db_path, **args)
        self.allow_new_buys = bool(allow_new_buys)
        self.store = LiveMomentumStore(live_db_path or self.market_history_db_path.parent / "momentum_live.db", kiwoom.account_number)
        self.live_states = self.store.load()
        self.order_guard = None
        self.buy_order_handler = None
        self.buy_rejection_reason_handler = None
        self.listener_registered = False
        self.last_warning = {}

    def check_and_get_universe(self, shared_universe_df=None):
        super().check_and_get_universe(shared_universe_df)
        self._reconcile_recorded_fills()

    def _reconcile_recorded_fills(self):
        # Recover a crash between the common fill journal commit and live-state commit.
        from util.db_helper import MONITORING_DB
        if not Path(MONITORING_DB).exists():
            return
        with closing(sqlite3.connect(Path(MONITORING_DB).resolve().as_uri() + "?mode=ro", uri=True)) as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_fill_log'").fetchone():
                return
            for code, state in list(self.live_states.items()):
                if not state.get("pending") and state.get("quantity", 0) == 0:
                    continue
                rows = con.execute("SELECT fill_no,order_no,order_type,quantity,price,filled_at FROM trade_fill_log WHERE code=? AND strategy_name=? AND filled_at>=? ORDER BY rowid",
                    (code, self.strategy_name, state["entry_at"])).fetchall()
                for fill, order, kind, qty, price, at in rows:
                    self.on_order_event({"code": code, "strategy_name": self.strategy_name, "order_no": order,
                        "order_type": kind, "fill_no": fill, "fill_quantity": qty, "fill_price": price,
                        "at": at, "remaining": -1, "rqname": "", "status": "체결"})
                # Successful broker order query plus matching fill journal can resolve a completed order.
                state = self.live_states[code]
                info = self.kiwoom.order.get(code, {})
                if getattr(self.kiwoom, "last_order_query_success", False) and not info and state.get("pending") and state.get("order_no"):
                    self._persist(code, dict(state, pending="", order_no=""))

    def set_order_guard(self, callback): self.order_guard = callback
    def set_buy_order_handler(self, callback): self.buy_order_handler = callback
    def set_buy_rejection_reason_handler(self, callback): self.buy_rejection_reason_handler = callback

    def set_universe_real_time(self, register_market=True, codes_override=None, force_refresh=False):
        if not self.listener_registered:
            self.kiwoom.add_realtime_listener(self.on_realtime_tick)
            self.kiwoom.add_order_listener(self.on_order_event)
            self.listener_registered = True
        return super().set_universe_real_time(register_market, codes_override, force_refresh)

    def get_realtime_candidate_codes(self):
        codes = set(super().get_realtime_candidate_codes())
        codes.update(code for code, state in self.live_states.items() if state.get("quantity", 0) > 0 or state.get("pending"))
        codes.update(code for code in self.kiwoom.balance if get_position_strategy(code) == self.strategy_name)
        return sorted(codes)

    def _persist(self, code, state):
        self.store.save(code, state)
        self.live_states[code] = state

    def _warn(self, code, message):
        if time.monotonic() - self.last_warning.get((code, message), -1000) >= 60:
            print(f"[Momentum LIVE] {code}: {message}")
            self.last_warning[(code, message)] = time.monotonic()

    def on_realtime_tick(self, raw_code, tick):
        now = self.clock()
        if not self.is_init_success or not check_transaction_open(now):
            return
        code = str(raw_code).strip().zfill(6)
        if not self.metrics.get(code, {}).get("eligible") and code not in self.live_states and code not in self.kiwoom.balance:
            return
        try:
            price = float(tick.get("현재가") or 0)
            raw = str(tick.get("체결시간") or "")
            tick_at = datetime.strptime(now.strftime("%Y%m%d") + raw, "%Y%m%d%H%M%S") if len(raw) == 6 else None
            if not math.isfinite(price) or price <= 0 or tick_at is None or tick_at < datetime.combine(now.date(), get_market_open_time(now)) or tick_at > now + timedelta(seconds=5) or now - tick_at > timedelta(seconds=30):
                return
            # Accumulate opening observations even when orders are blocked or a paper C position exists.
            paper_runtime = self.runtime.get(code)
            signal = self.get_buy_signal(code, tick, now)
            if paper_runtime is not None:
                self.runtime[code] = paper_runtime
            owner = get_position_strategy(code)
            balance = self.kiwoom.balance.get(code, {})
            qty = int(balance.get("보유수량") or 0)
            state = self.live_states.get(code)
            if qty > 0:
                if owner == self.strategy_name:
                    self._manage_live(code, tick, now, state, qty)
                return
            if not self.allow_new_buys or not signal or not self.order_guard or not self.order_guard() or not self.buy_order_handler:
                return
            # Pending/filled state survives restart, preventing same-day repeat entries.
            if state and (state.get("pending") or state.get("quantity", 0) > 0 or state.get("buy_date") == now.strftime("%Y%m%d")):
                return
            if int(self.kiwoom.order.get(code, {}).get("미체결수량") or 0) > 0:
                return
            self._buy_live(code, tick, signal, now)
        except Exception as exc:
            self._warn(code, f"실거래 처리 보류: {exc}")

    def _signal_log(self, code, event, reason, price, data):
        return save_strategy_signal(strategy_name=self.strategy_name, code=code,
            code_name=self.universe.get(code, {}).get("code_name", code), signal_type=event,
            reason_code=reason, signal_reason=reason, current_price=price, condition_data=data)

    def _buy_live(self, code, tick, signal, now):
        # Limit buy at current observed price: cannot fill above the planned ATR risk distance.
        price = int(tick["현재가"])
        stop = float(signal["stop_price"])
        key = self._signal_log(code, "BUY", signal["reason_code"], price, signal["condition_data"])
        state = {"stop_price": stop, "initial_stop": stop, "entry_at": now.strftime("%Y%m%d%H%M%S"),
                 "entry_price": 0.0, "initial_quantity": 0, "quantity": 0, "partial_done": False,
                 "partial_filled": 0, "pending": "BUY", "buy_date": now.strftime("%Y%m%d"), "order_no": ""}
        self._persist(code, state)  # Stop exists before an order can be sent.
        try:
            order = self.buy_order_handler(strategy_name=self.strategy_name, code=code,
                code_name=self.universe.get(code, {}).get("code_name", code), price=price,
                rqname="momentum_live_buy", screen_no="2301", stop_price=stop,
                risk_ratio=self.config["account"]["risk_ratio"],
                buy_fee_rate=self.config["account"]["buy_fee_rate"],
                sell_cost_rate=self.config["account"]["sell_fee_rate"] + self.config["account"]["sell_tax_rate"])
        except Exception:
            # Ambiguous exceptions may occur after the order was sent: keep pending until broker reconciliation.
            raise
        if order is None:
            state = self.live_states[code]
            state["pending"] = ""
            state["buy_date"] = ""  # No broker order accepted; another valid signal may retry.
            self._persist(code, state)
            reason = self.buy_rejection_reason_handler(code) if self.buy_rejection_reason_handler else "Manager 거절"
            update_strategy_signal_order_result(key, False, "BLOCKED", reason)
            return
        if self.live_states[code]["quantity"] == 0:
            save_position_strategy(code, self.universe.get(code, {}).get("code_name", code), self.strategy_name, 0, 0)
        update_strategy_signal_order_result(key, True, "ORDER_SENT", None)
        print(f"[Momentum LIVE] 매수 주문 전송 {code} {order['quantity']}주; 체결 대기")

    def _manage_live(self, code, tick, now, state, broker_qty):
        if state and (state.get("quantity", 0) != broker_qty or state.get("initial_quantity", 0) <= 0):
            self._reconcile_recorded_fills()
            state = self.live_states.get(code)
        if not state or state.get("initial_quantity", 0) <= 0:
            self._warn(code, "실보유는 있으나 체결 기반 손절 상태 없음: 신규 주문 보류, 상태 확인 필요")
            return
        if state["quantity"] != broker_qty:
            self._warn(code, "브로커 잔고와 실체결 상태 불일치: 잔고/체결 동기화 대기")
            return
        order = self.kiwoom.order.get(code, {})
        if state.get("pending") in ("BUY", "SELL", "PARTIAL") and float(tick["현재가"]) <= state["stop_price"]:
            number = state.get("order_no") or str(order.get("주문번호") or "")
            remaining = int(order.get("미체결수량") or 0)
            if number and remaining > 0:
                buying = state["pending"] == "BUY"
                result = self.kiwoom.send_order("momentum_live_cancel_buy" if buying else "momentum_live_cancel_sell",
                    "2303", 3 if buying else 4, code, remaining, 0, "00", number, strategy_name=self.strategy_name)
                if result == 0:
                    self._persist(code, dict(state, pending="BUY_CANCEL" if buying else "SELL_CANCEL", order_no=number))
            return
        if state.get("pending") or int(order.get("미체결수량") or 0) > 0:
            return
        if not self.order_guard or not self.order_guard():
            return
        price = float(tick["현재가"])
        context = {} if price <= state["stop_price"] else self.exit_context({"code": code, "entry_at": state["entry_at"]}, now)
        position = {"code": code, "quantity": broker_qty, "initial_quantity": state["initial_quantity"],
                    "entry_price": state["entry_price"], "entry_at": state["entry_at"], "state": state}
        signal = momentum_exit(position, price, context, self.rules, self.config["account"])
        if not signal:
            return
        if signal.get("partial"):
            target = max(1, int(state["initial_quantity"] * self.rules["partial_ratio"]))
            quantity = min(broker_qty, target - state.get("partial_filled", 0))
            if quantity <= 0:
                return
        else:
            quantity = broker_qty
        available = int(self.kiwoom.balance[code].get("매매가능수량") or 0)
        if available < quantity:
            return
        key = self._signal_log(code, "SELL", signal["reason_code"], price, {"quantity": quantity, "state": state})
        state = dict(state, pending="PARTIAL" if signal.get("partial") else "SELL", order_no="", partial_target=max(1, int(state["initial_quantity"] * self.rules["partial_ratio"])))
        self._persist(code, state)
        result = self.kiwoom.send_order("momentum_live_partial" if signal.get("partial") else "momentum_live_sell",
            "2302", 2, code, quantity, 0, "03", strategy_name=self.strategy_name)
        if result != 0:
            state["pending"] = ""
            self._persist(code, state)
            update_strategy_signal_order_result(key, True, "ORDER_FAILED", str(result))
            return
        self.kiwoom.order[code] = {"주문구분": "매도", "미체결수량": quantity,
            "strategy_name": self.strategy_name, "order_time": time.time()}
        update_strategy_signal_order_result(key, True, "ORDER_SENT", None)

    def on_order_event(self, event):
        code = event["code"]
        state = self.live_states.get(code)
        if not state or event.get("strategy_name") != self.strategy_name:
            return
        kind = event.get("order_type", "")
        number = event.get("order_no", "")
        intent = event.get("rqname", "")
        match = bool(state.get("pending")) and (state.get("order_no") == number or
            (not state.get("order_no") and intent.startswith("momentum_live_")))
        if match and number and not state.get("order_no"):
            state = dict(state, order_no=number)
            if state.get("pending") == "PARTIAL":
                state["partial_order_no"] = number
            self._persist(code, state)
        qty, price = int(event.get("fill_quantity") or 0), float(event.get("fill_price") or 0)
        if event.get("fill_no") and qty > 0 and price > 0 and kind in ("매수", "매도"):
            partial_match = kind == "매도" and bool(state.get("partial_order_no")) and state["partial_order_no"] == number
            def update(s):
                if kind == "매수":
                    previous = s["initial_quantity"]
                    s["entry_price"] = (s["entry_price"] * previous + price * qty) / (previous + qty)
                    s["initial_quantity"] += qty
                    s["quantity"] += qty
                    if previous == 0:
                        s["entry_at"] = event["at"]
                else:
                    s["quantity"] = max(0, s["quantity"] - qty)
                    if partial_match:
                        s["partial_filled"] += qty
                        if s["partial_filled"] >= s["partial_target"]:
                            s["partial_done"] = True
                            costs = self.config["account"]
                            breakeven = s["entry_price"] * (1 + costs["buy_fee_rate"]) / (1 - costs["sell_fee_rate"] - costs["sell_tax_rate"])
                            s["stop_price"] = max(s["stop_price"], breakeven)
                return s
            filled = self.store.apply_fill(code, event["at"][:8] + ":" + str(event["fill_no"]), update)
            if filled is not None:
                self.live_states[code] = state = filled
        cancelled = "취소" in kind or "거부" in str(event.get("status", ""))
        cancel_match = str(event.get("original_order_no") or "").lstrip("0") == str(state.get("order_no") or "").lstrip("0") and bool(state.get("order_no"))
        if ((match and event.get("remaining") == 0) or (cancelled and (match or cancel_match))):
            state = dict(state, pending="", order_no="")
            self._persist(code, state)
