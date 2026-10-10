"""Notification privacy tests with fake credentials and no network access."""
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch


def load_notifier():
    # Other strategy tests stub util.notifier globally. Load the actual file
    # under a private name, without importing credentials or using the network.
    constants = types.ModuleType("util.const")
    constants.Telegram_bot_TOKEN = ""
    constants.Telegram_chat_ID = ""
    requests = types.ModuleType("requests")
    requests.post = Mock()
    requests.get = Mock()
    filename = Path(__file__).resolve().parents[1] / "util" / "notifier.py"
    spec = importlib.util.spec_from_file_location("notifier_privacy_under_test", filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"util.const": constants, "requests": requests}):
        spec.loader.exec_module(module)
    return module


class NotifierPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.notifier = load_notifier()

    def test_traceback_keeps_filename_line_function_and_error(self):
        message = (
            "[전략 초기화 실패]\nTraceback (most recent call last):\n"
            '  File "C:\\Users\\sample\\OneDrive\\바탕 화면\\SystemTrading\\'
            'strategy\\StrategyManager.py", line 791, in initialize_strategies\n'
            "    strategy.check_and_get_price_data()\n"
            '  File "/srv/private/app/strategy/MomentumContractionStrategy.py", '
            "line 120, in check_and_get_price_data\n"
            "    raise RuntimeError(\"일봉 부족\")\n"
            "RuntimeError: 일봉 부족"
        )
        outgoing = self.notifier.sanitize_telegram_message(message)
        self.assertIn('File "StrategyManager.py", line 791, in initialize_strategies', outgoing)
        self.assertIn('File "MomentumContractionStrategy.py", line 120', outgoing)
        self.assertIn("RuntimeError: 일봉 부족", outgoing)
        for private_part in ("C:\\Users", "sample", "OneDrive", "바탕 화면", "/srv/private"):
            self.assertNotIn(private_part, outgoing)

    def test_relative_and_virtual_traceback_files(self):
        outgoing = self.notifier.sanitize_telegram_message(
            'File "strategy/StrategyManager.py", line 1, in start\n'
            'File "<string>", line 2, in <module>'
        )
        self.assertIn('File "StrategyManager.py", line 1', outgoing)
        self.assertIn('File "<string>", line 2', outgoing)

    def test_common_filesystem_paths_are_hidden(self):
        cases = [
            r'DB: "C:\Users\sample\바탕 화면\market_history.db"',
            "DB: 'C:/Users/sample/My Documents/market_history.db'",
            r"DB: C:\Users\sample\바탕 화면\market_history.db",
            "DB: C:/Users/sample/My Documents/market_history.db",
            r'경로: "\\server\private share\market.db"',
            r"경로: \\server\private\market.db",
            "경로: //server/private/market.db",
            'DB: "/home/sample/My Documents/market.db"',
            "DB: /srv/private/market.db",
            "DB: /home/sample/My Documents/market.db",
            "DB: ~/private/market.db",
            "DB: ../private/market.db",
            "DB: ./private/market.db",
            "DB: file:///home/sample/market.db",
            "DB: sqlite:///home/sample/market.db",
            "FileNotFoundError: [Errno 2] No such file: '/home/sample/private.db'",
        ]
        for message in cases:
            with self.subTest(message=message):
                outgoing = self.notifier.sanitize_telegram_message(message)
                self.assertIn("[경로 숨김]", outgoing)
                for private_part in ("sample", "private", "market", "Documents", "바탕 화면"):
                    self.assertNotIn(private_part, outgoing)

    def test_trade_messages_commands_ratios_and_web_links_are_unchanged(self):
        messages = [
            "[매수] 삼성전자(005930) 10주 / 가격 70,000원",
            "손익 -3.5% / MA 5/20/60 / ADR > 3.5%",
            "Telegram /status 명령대기",
            "https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/",
            "참고: https://example.com/docs/path?value=1, 다음 안내",
            "RuntimeError: 최소 65거래일을 구축하세요.",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(self.notifier.sanitize_telegram_message(message), message)

    def test_send_filters_outgoing_copy_but_console_and_input_keep_full_log(self):
        message = '실패\nFile "C:\\Users\\sample\\app\\main.py", line 3, in start'
        original = message
        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        self.notifier.requests.post.return_value = response
        console = io.StringIO()
        with redirect_stdout(console):
            print(message)  # Same print + send_message sequence as the app.
            result = self.notifier.send_message(message, token="TEST", chat_id="TEST")
        self.assertEqual(message, original)
        self.assertIn(original, console.getvalue())
        call = self.notifier.requests.post.call_args
        self.assertEqual(call.kwargs["data"]["text"], '실패\nFile "main.py", line 3, in start')
        self.assertEqual(call.kwargs["data"]["chat_id"], "TEST")
        self.assertEqual(call.kwargs["timeout"], 10)
        self.assertTrue(result["ok"])

    def test_skipped_notification_keeps_original_console_text(self):
        message = r"DB: C:\Users\sample\market.db"
        console = io.StringIO()
        with redirect_stdout(console):
            self.assertIsNone(self.notifier.send_message(message, token="", chat_id=""))
        self.assertIn(message, console.getvalue())
        self.notifier.requests.post.assert_not_called()

    def test_non_string_messages_and_repeated_sanitization(self):
        self.assertEqual(self.notifier.sanitize_telegram_message(123), "123")
        message = self.notifier.sanitize_telegram_message("DB: /home/sample/market.db")
        self.assertEqual(self.notifier.sanitize_telegram_message(message), message)


if __name__ == "__main__":
    unittest.main()
