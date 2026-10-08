"""Fake broker tests; never logs in or sends a real order."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest
from paper_fixture import qt_stubs
qt_stubs()
from strategy.LiveMomentumContractionStrategy import LiveMomentumContractionStrategy
from strategy.StrategyManager import StrategyManager
from util.position_sizing import stop_risk_quantity

class MomentumLiveTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 9, 30, 9, 6)
        self.kiwoom = SimpleNamespace(account_number="TEST_ACCOUNT", balance={}, order={}, send_order=Mock(return_value=0), add_order_listener=Mock(), add_realtime_listener=Mock())
        self.strategy = LiveMomentumContractionStrategy(self.kiwoom, market_db_path=Path(self.directory.name)/"market.db", live_db_path=Path(self.directory.name)/"live.db", clock=lambda:self.now, allow_new_buys=True)
        self.strategy.is_init_success = True
        self.strategy.universe={"000001":{"code_name":"Fixture"}}
        self.strategy.set_order_guard(lambda:True)
        self.strategy._signal_log=Mock(return_value="fixture")
        self.update=patch("strategy.LiveMomentumContractionStrategy.update_strategy_signal_order_result").start()
        self.addCleanup(patch.stopall)

    def state(self, qty=60, pending=""):
        s={"entry_at":"20260923090000","buy_date":"20260923","entry_price":10000.0,"initial_quantity":qty,"quantity":qty,"initial_stop":9500.0,"stop_price":9500.0,"partial_done":False,"partial_filled":0,"pending":pending,"order_no":"","partial_target":20}
        self.strategy._persist("000001",s)
        return s

    def event(self, kind, qty, number="ORDER1", fill="F1", remaining=0, rqname="", **extra):
        e={"code":"000001","strategy_name":"MomentumContractionStrategy","order_type":kind,"fill_quantity":qty,"fill_price":11000,"order_no":number,"fill_no":fill,"remaining":remaining,"rqname":rqname,"at":"20260930090600","status":"체결"}
        e.update(extra);self.strategy.on_order_event(e)

    def test_partial_changes_only_after_actual_fills_and_duplicate_is_ignored(self):
        self.state(pending="PARTIAL")
        self.event("매도",5,remaining=15,rqname="momentum_live_partial")
        s=self.strategy.live_states["000001"]
        self.assertFalse(s["partial_done"]);self.assertEqual(s["stop_price"],9500)
        self.event("매도",5,remaining=15,rqname="momentum_live_partial")
        self.assertEqual(self.strategy.live_states["000001"]["quantity"],55)
        self.event("매도",15,fill="F2")
        s=self.strategy.live_states["000001"]
        self.assertTrue(s["partial_done"]);self.assertEqual(s["quantity"],40)
        self.assertGreater(s["stop_price"],10000);self.assertEqual(s["pending"],"")
        restored=LiveMomentumContractionStrategy(self.kiwoom, market_db_path=Path(self.directory.name)/"market.db",live_db_path=Path(self.directory.name)/"live.db")
        self.assertEqual(restored.live_states["000001"],s)

    def test_cancelled_partial_keeps_filled_quantity_for_remaining_retry(self):
        self.state(pending="PARTIAL")
        self.event("매도",5,remaining=15,rqname="momentum_live_partial")
        self.event("매도취소",0,number="CANCEL",fill="",original_order_no="ORDER1",status="확인")
        s=self.strategy.live_states["000001"]
        self.assertEqual(s["pending"],"");self.assertEqual(s["partial_filled"],5);self.assertFalse(s["partial_done"])
        self.kiwoom.balance["000001"]={"보유수량":55,"매매가능수량":55}
        self.strategy.exit_context=Mock(return_value={"elapsed_sessions":4})
        self.strategy._manage_live("000001",{"현재가":11000},self.now,s,55)
        self.assertEqual(self.kiwoom.send_order.call_args.args[4],15)
        self.assertFalse(self.strategy.live_states["000001"]["partial_done"])

    def test_buy_uses_actual_fill_and_manual_sell_does_not_claim_partial(self):
        self.state(qty=0,pending="BUY")
        self.event("매수",10,rqname="momentum_live_buy")
        s=self.strategy.live_states["000001"]
        self.assertEqual(s["initial_quantity"],10);self.assertEqual(s["entry_price"],11000)
        self.event("매도",10,number="MANUAL",fill="F2")
        s=self.strategy.live_states["000001"]
        self.assertEqual(s["quantity"],0);self.assertFalse(s["partial_done"])

    def test_stop_does_not_need_daily_database_and_other_owner_is_ignored(self):
        s=self.state()
        self.kiwoom.balance["000001"]={"보유수량":60,"매매가능수량":60}
        self.strategy.exit_context=Mock(side_effect=AssertionError("stop must not require daily DB"))
        self.strategy._manage_live("000001",{"현재가":9400},self.now,s,60)
        self.assertEqual(self.kiwoom.send_order.call_args.args[4],60)
        self.assertEqual(self.kiwoom.send_order.call_args.args[6],"03")
        self.kiwoom.send_order.reset_mock()
        self.strategy.get_buy_signal=Mock(return_value=None)
        with patch("strategy.LiveMomentumContractionStrategy.get_position_strategy",return_value="HighBreakoutStrategy"):
            self.strategy.on_realtime_tick("000001",{"현재가":9400,"체결시간":"090600"})
        self.kiwoom.send_order.assert_not_called()

    def test_failed_sell_keeps_stop_and_can_retry(self):
        s=self.state();self.kiwoom.send_order.return_value=-1
        self.kiwoom.balance["000001"]={"保有":60,"매매가능수량":60}
        self.strategy._manage_live("000001",{"현재가":9400},self.now,s,60)
        s=self.strategy.live_states["000001"]
        self.assertEqual(s["pending"],"");self.assertEqual(s["stop_price"],9500)

    def test_stop_cancels_unfilled_buy_before_selling_partial_holding(self):
        s=self.state(qty=10,pending="BUY");s["order_no"]="BUY1"
        self.strategy._persist("000001",s)
        self.kiwoom.order["000001"]={"주문번호":"BUY1","미체결수량":20}
        self.strategy._manage_live("000001",{"현재가":9400},self.now,s,10)
        args=self.kiwoom.send_order.call_args.args
        self.assertEqual(args[2],3);self.assertEqual(args[4],20);self.assertEqual(args[7],"BUY1")

    def test_manager_allows_live_momentum_but_blocks_paper_modes_and_limits_risk(self):
        m=StrategyManager.__new__(StrategyManager);m.strategies=[self.strategy];m.kiwoom=self.kiwoom
        m.last_buy_rejections={};m.buy_request_in_progress=set();m.reserved_buy_orders={}
        m.get_event_driven_order_block_reason=lambda:None;m.has_pending_buy=lambda code:False
        m.get_account_position_count=lambda:0;m.calculate_total_assets=lambda:10000000;m.get_available_buy_cash=lambda:10000000
        result=m.request_buy_order(self.strategy.strategy_name,"000001","Fixture",10000,"momentum_live_buy","2301",stop_price=9000,risk_ratio=.005,buy_fee_rate=.00035,sell_cost_rate=.00235)
        expected=stop_risk_quantity(10000000,10000,9000,.005,.00035,.00235)
        self.assertEqual(result["quantity"],expected)
        self.assertLessEqual(result["estimated_amount"],1000000)
        self.assertIsNone(m.request_buy_order("HighBreakoutBaselinePaper","000002","Fixture",10000,"test","2301"))
        self.assertIsNone(m.request_buy_order("HighBreakoutStrategy","000002","Fixture",10000,"test","2301"))

    def test_pending_state_blocks_same_day_reentry_after_restart(self):
        self.state(qty=0,pending="BUY")
        self.strategy.get_buy_signal=Mock(return_value={"stop_price":9500})
        self.strategy.buy_order_handler=Mock()
        with patch("strategy.LiveMomentumContractionStrategy.get_position_strategy",return_value=None):
            self.strategy.on_realtime_tick("000001",{"현재가":10000,"체결시간":"090600"})
        self.strategy.buy_order_handler.assert_not_called()

    def test_crash_replay_reconciles_fill_journal_once(self):
        import sqlite3
        db=Path(self.directory.name)/"monitoring.db"
        self.state(qty=0,pending="BUY")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE trade_fill_log(fill_no,order_no,order_type,quantity,price,filled_at,code,strategy_name)")
            c.execute("INSERT INTO trade_fill_log VALUES ('F1','ORDER1','매수',10,11000,'20260930090600','000001','MomentumContractionStrategy')")
        with patch("util.db_helper.MONITORING_DB",db):
            self.strategy._reconcile_recorded_fills();self.strategy._reconcile_recorded_fills()
        self.assertEqual(self.strategy.live_states["000001"]["quantity"],10)

    def test_disabling_new_buys_does_not_disable_existing_stop_management(self):
        s=self.state()
        self.strategy.allow_new_buys=False
        self.kiwoom.balance["000001"]={"보유수량":60,"매매가능수량":60}
        self.strategy.get_buy_signal=Mock(return_value=None)
        with patch("strategy.LiveMomentumContractionStrategy.get_position_strategy",return_value=self.strategy.strategy_name):
            self.strategy.on_realtime_tick("000001",{"현재가":9400,"체결시간":"090600"})
        self.assertEqual(self.kiwoom.send_order.call_args.args[2],2)
        self.assertEqual(self.kiwoom.send_order.call_args.args[4],60)

    def test_stop_cancels_pending_partial_sell_then_exits_remaining(self):
        s=self.state(pending="PARTIAL")
        self.event("매도",5,remaining=15,rqname="momentum_live_partial")
        s=self.strategy.live_states["000001"]
        self.kiwoom.balance["000001"]={"보유수량":55,"매매가능수량":40}
        self.kiwoom.order["000001"]={"주문번호":"ORDER1","미체결수량":15}
        self.strategy._manage_live("000001",{"현재가":9400},self.now,s,55)
        self.assertEqual(self.kiwoom.send_order.call_args.args[2],4)
        self.event("매도",5,fill="F2",remaining=10)
        self.assertEqual(self.strategy.live_states["000001"]["partial_filled"],10)
        self.event("매도취소",0,number="CANCEL",fill="",original_order_no="ORDER1",status="확인")
        self.kiwoom.order={};self.kiwoom.balance["000001"]={"보유수량":50,"매매가능수량":50}
        self.strategy._manage_live("000001",{"현재가":9400},self.now,self.strategy.live_states["000001"],50)
        self.assertEqual(self.kiwoom.send_order.call_args.args[2],2)
        self.assertEqual(self.kiwoom.send_order.call_args.args[4],50)
