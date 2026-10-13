"""Behavior checks for startup, status presentation and web response compatibility."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from util.status_message import StatusMessageBuilder

ROOT = Path(__file__).resolve().parents[1]


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 13, 10, 5)


class ImportSafetyTest(unittest.TestCase):
    def test_importing_main_and_db_helper_has_no_startup_or_file_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import sys; import main; import util.db_helper; "
                    "assert 'PyQt5' not in sys.modules; assert 'api.Kiwoom' not in sys.modules"
                )],
                cwd=directory, env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_main_keeps_strategy_wiring_and_momentum_opt_in(self):
        import main
        app = Mock()
        app.exec_.return_value = 0
        application = Mock(return_value=app)
        broker = Mock()
        kiwoom = Mock(return_value=broker)
        value = Mock()
        momentum = Mock()
        manager = Mock()
        modules = {}
        for name, attr, value_mock in [
            ("PyQt5.QtWidgets", "QApplication", application),
            ("api.Kiwoom", "Kiwoom", kiwoom),
            ("strategy.ValueQualityStrategy", "ValueQualityStrategy", value),
            ("strategy.LiveMomentumContractionStrategy", "LiveMomentumContractionStrategy", momentum),
            ("strategy.StrategyManager", "StrategyManager", manager),
        ]:
            modules[name] = types.ModuleType(name)
            setattr(modules[name], attr, value_mock)
        for flag, enabled in [("0", False), ("1", True)]:
            with self.subTest(flag=flag), patch.dict(sys.modules, modules), \
                 patch.dict(os.environ, {"SYSTEMTRADING_MOMENTUM_LIVE": flag}), patch("builtins.print"):
                self.assertEqual(main.main(), 0)
            value.assert_called_with(broker, auto_init=False)
            momentum.assert_called_with(broker, allow_new_buys=enabled)
            manager.assert_called_with(broker, [value.return_value, momentum.return_value])
        self.assertEqual(manager.return_value.start.call_count, 2)
        self.assertEqual(app.exec_.call_count, 2)


class StatusMessageTest(unittest.TestCase):
    def setUp(self):
        self.manager = SimpleNamespace(
            last_order_sync_at=0, cash_deposit=500000, d2_estimated_deposit=490000,
            deposit=450000, MAX_POSITION_RATIO=0.1, MAX_ACCOUNT_POSITIONS=10,
            calculate_total_assets=lambda: 1000000, get_reserved_buy_amount=lambda: 20000,
            get_available_buy_cash=lambda: 430000, get_account_position_count=lambda: 1,
            get_code_name=lambda code: "테스트종목",
            strategies=[SimpleNamespace(strategy_name="ValueQualityStrategy")],
            kiwoom=SimpleNamespace(
                balance={"000001": {"종목명": "테스트종목", "보유수량": 3, "매입단가": 10000}},
                order={"000001": {"주문구분": "매수", "미체결수량": 2, "주문가격": 11000, "order_time": 880}},
                universe_realtime_transaction_info={"000001": {"현재가": 11000}},
                send_order=Mock(),
            ),
        )

    def test_status_preserves_holdings_pending_orders_and_unconfirmed_costs(self):
        with patch("util.status_message.datetime", FixedDatetime), \
             patch("util.status_message.time.time", return_value=1000), \
             patch("util.status_message.get_strategy_position_counts", return_value={"ValueQualityStrategy": 1}), \
             patch("util.status_message.get_today_order_count", return_value=4), \
             patch("util.status_message.get_today_realized_pnl", return_value=(10000, 9500, 1, 2)), \
             patch("util.status_message.get_last_sent_value_report_at", return_value="20261013090000"):
            text = StatusMessageBuilder(self.manager).build()
        self.assertEqual(text, "\n".join([
            "[자동매매 상태]", "조회 시각: 2026-10-13 10:05:00",
            "잔고/미체결 기준: 아직 동기화되지 않음", "총자산: 1,000,000원",
            "예수금: 500,000원", "D+2 추정예수금: 490,000원", "키움 주문가능금액: 450,000원",
            "Manager 매수 예약금: 20,000원", "실제 신규매수 가용현금: 430,000원",
            "종목당 최대 비중: 10% (100,000원)", "계좌 보유/매수대기: 1/10종목", "",
            "[현재 보유 종목] 1개", "- 테스트종목(000001) 3주 | 매입 10,000 / 현재 11,000 | +10.00%", "",
            "[전략별 보유 현황]", "- ValueQualityStrategy: 1개", "",
            "[미체결 주문] 1건", "- 테스트종목(000001) 매수 2주 11,000원 | 경과 2분 0초", "",
            "[오늘 요약]", "- 매도 체결 총손익(수수료·세금 전): +10,000원",
            "- 매도 체결 순손익(비용 확인 거래 합산): +9,500원", "- 주문 횟수: 4건",
            "- 마지막 저평가 리포트 전송: 2026-10-13 09:00:00",
            "- 총손익 계산 제외 매도 거래: 1건 (매입가 기록 없음)",
            "- 순손익 계산 보류 거래: 2건 (수수료·세금 수신 대기 또는 미확인)",
        ]))
        self.manager.kiwoom.send_order.assert_not_called()

    def test_empty_holdings_and_missing_records_are_visible(self):
        self.manager.kiwoom.balance = {}
        self.manager.kiwoom.order = {}
        with patch("util.status_message.get_strategy_position_counts", return_value={}), \
             patch("util.status_message.get_today_order_count", return_value=0), \
             patch("util.status_message.get_today_realized_pnl", return_value=(0, 0, 0, 0)), \
             patch("util.status_message.get_last_sent_value_report_at", return_value=None):
            text = StatusMessageBuilder(self.manager).build()
        self.assertIn("[현재 보유 종목] 0개\n- 없음", text)
        self.assertIn("[미체결 주문] 0건\n- 없음", text)
        self.assertIn("전송 기록 없음", text)
        self.assertNotIn("계산 보류 거래", text)

    def test_elapsed_broker_time_fallback_and_invalid_time(self):
        builder = StatusMessageBuilder(self.manager)
        with patch("util.status_message.datetime", FixedDatetime):
            self.assertEqual(builder._get_order_elapsed_text({"시간": "100230"}), "2분 30초")
            self.assertEqual(builder._get_order_elapsed_text({"시간": "100600"}), "0분 0초")
            self.assertEqual(builder._get_order_elapsed_text({"시간": "999999"}), "확인 불가")


class WebSummaryTest(unittest.TestCase):
    def test_dashboard_and_strategy_list_keep_the_same_response_items(self):
        from fastapi.testclient import TestClient
        from web_api.main import app
        from web_api.strategies import STRATEGIES
        with patch("web_api.main.datetime", FixedDatetime), \
             patch("web_api.main.get_virtual_performance", side_effect=lambda name: {"strategy_name": name}), \
             patch("web_api.main.get_virtual_event_summary", return_value={"buy": 1, "sell": 2}) as events:
            client = TestClient(app)
            dashboard = client.get("/api/dashboard")
            strategies = client.get("/api/strategies")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(strategies.status_code, 200)
        self.assertEqual(dashboard.json()["strategies"], strategies.json()["items"])
        self.assertEqual(dashboard.json()["as_of"], "2026-10-13T10:05:00")
        self.assertIn("metric_note", dashboard.json())
        self.assertEqual({x["strategy_name"] for x in strategies.json()["items"]}, set(STRATEGIES))
        self.assertTrue(all(c.kwargs["event_date"] == "20261013" for c in events.call_args_list))


if __name__ == "__main__":
    unittest.main()
