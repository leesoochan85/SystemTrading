"""Error throttling tests: fake time, fake credentials, no network or orders."""
import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from test_notifier_privacy import load_notifier


class NotifierThrottleTest(unittest.TestCase):
    def setUp(self):
        self.notifier = load_notifier()
        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        self.notifier.requests.post.return_value = response
        self.clock = patch.object(self.notifier.time, "monotonic", return_value=1000.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.output = io.StringIO()
        self.console = redirect_stdout(self.output)
        self.console.__enter__()
        self.addCleanup(self.console.__exit__, None, None, None)

    def error(self, message="RuntimeError: 일봉 저장 실패", **kwargs):
        options = dict(event_key="manager.step", token="FAKE", chat_id="1")
        options.update(kwargs)
        return self.notifier.send_error(message, **options)

    def test_first_error_and_boundary_with_repeat_count_and_every_console_log(self):
        self.assertTrue(self.error()["ok"])
        self.now.return_value = 1001.0
        self.assertIsNone(self.error())
        self.now.return_value = 1599.999
        self.assertIsNone(self.error())
        self.assertEqual(self.notifier.requests.post.call_count, 1)
        self.now.return_value = 1600.0
        self.error()
        self.assertEqual(self.notifier.requests.post.call_count, 2)
        self.assertIn("동일 오류 2회 추가 발생", self.notifier.requests.post.call_args.kwargs["data"]["text"])
        self.assertEqual(self.output.getvalue().count("RuntimeError: 일봉 저장 실패"), 4)
        self.now.return_value = 2200.0
        self.error()
        self.assertNotIn("[반복 오류]", self.notifier.requests.post.call_args.kwargs["data"]["text"])

    def test_different_errors_callers_symbols_bots_and_chats_are_independent(self):
        self.error()
        self.error("ValueError: 일봉 오류")
        self.error(event_key="manager.close")
        self.error(event_key="sell:005930")
        self.error(event_key="sell:000660")
        self.error(chat_id="2")
        self.error(token="OTHER_FAKE")
        self.assertEqual(self.notifier.requests.post.call_count, 7)
        self.error()
        self.assertEqual(self.notifier.requests.post.call_count, 7)

    def test_broker_code_identity_ignores_changing_return_rate_but_not_new_codes(self):
        self.error("매도 실패 손실률 -10.1% result=-308", error_identity="SendOrder:-308")
        self.error("매도 실패 손실률 -10.2% result=-308", error_identity="SendOrder:-308")
        self.assertEqual(self.notifier.requests.post.call_count, 1)
        self.error("매도 실패 result=-300", error_identity="SendOrder:-300")
        self.assertEqual(self.notifier.requests.post.call_count, 2)

    def test_network_exception_is_also_throttled_and_retried_at_boundary(self):
        self.notifier.requests.post.side_effect = RuntimeError("offline")
        self.assertIsNone(self.error())
        self.assertIsNone(self.error())
        self.assertEqual(self.notifier.requests.post.call_count, 1)
        self.now.return_value = 1600.0
        self.error()
        self.assertEqual(self.notifier.requests.post.call_count, 2)

    def test_telegram_rejected_response_is_also_throttled(self):
        self.notifier.requests.post.return_value.ok = False
        self.notifier.requests.post.return_value.json.return_value = {"ok": False}
        self.assertIsNone(self.error())
        self.assertIsNone(self.error())
        self.assertEqual(self.notifier.requests.post.call_count, 1)

    def test_normal_trade_messages_and_command_replies_are_never_throttled(self):
        for message in ("매수 체결 10주", "매도 체결 10주", "/status 응답"):
            for _ in range(2):
                self.notifier.send_message(message, token="FAKE", chat_id="1")
        self.assertEqual(self.notifier.requests.post.call_count, 6)

    def test_error_console_keeps_path_and_outgoing_copy_hides_it(self):
        message = 'Traceback\nFile "C:\\Users\\sample\\app\\main.py", line 1, in step\nRuntimeError: failed'
        self.error(message)
        self.error(message)
        self.assertEqual(self.output.getvalue().count(message), 2)
        outgoing = self.notifier.requests.post.call_args.kwargs["data"]["text"]
        self.assertIn('File "main.py"', outgoing)
        self.assertNotIn("sample", outgoing)

    def test_no_credentials_logs_without_sending_or_consuming_cooldown(self):
        self.error(token="")
        self.error(chat_id="")
        self.notifier.requests.post.assert_not_called()
        self.assertEqual(len(self.notifier._error_notifications), 0)
        self.error()
        self.assertEqual(self.notifier.requests.post.call_count, 1)

    def test_cache_is_bounded_and_active_keys_remain_suppressed(self):
        with patch.object(self.notifier, "ERROR_CACHE_MAX_ENTRIES", 3):
            for i in range(3):
                self.error(event_key=str(i))
            self.error(event_key="0")
            self.error(event_key="3")
            self.assertEqual(len(self.notifier._error_notifications), 3)
            self.error(event_key="0")
            self.assertEqual(self.notifier.requests.post.call_count, 4)
            self.error(event_key="1")  # Evicted entry starts a new interval.
            self.assertEqual(self.notifier.requests.post.call_count, 5)

    def test_concurrent_identical_errors_make_one_attempt(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.error(), range(20)))
        self.assertEqual(self.notifier.requests.post.call_count, 1)
        self.assertEqual(self.output.getvalue().count("RuntimeError: 일봉 저장 실패"), 20)


if __name__ == "__main__":
    unittest.main()
