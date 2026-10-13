"""Reception tests without credentials, Telegram requests, or Kiwoom."""
import ast
import importlib.util
import io
import threading
from concurrent.futures import Future
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_notifier_privacy import load_notifier

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("poller_under_test", ROOT / "util/telegram_poller.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TelegramPollerTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.fetch = Mock()
        self.poller = module.TelegramCommandPoller(self.fetch, clock=lambda: self.now)
        self.poller.executor.shutdown()
        self.poller.executor = Mock()
        self.pending = Future()
        self.poller.executor.submit.return_value = self.pending
        self.output = io.StringIO()
        self.console = redirect_stdout(self.output)
        self.console.__enter__()
        self.addCleanup(self.console.__exit__, None, None, None)
        self.addCleanup(self.poller.close)

    def test_pending_request_never_waits_or_submits_overlap(self):
        self.assertEqual(self.poller.poll(42), [])
        for _ in range(100):
            self.assertEqual(self.poller.poll(42), [])
        self.poller.executor.submit.assert_called_once_with(
            self.fetch, offset=42, request_timeout=(3, 5), raise_errors=True)

    def test_completed_updates_and_next_offset_are_preserved(self):
        self.poller.poll(None)
        updates = [{"update_id": 42, "message": {"text": "/status"}}]
        self.pending.set_result(updates)
        self.assertEqual(self.poller.poll(None), updates)
        self.now = 109.9
        self.poller.poll(43)
        self.assertEqual(self.poller.executor.submit.call_count, 1)
        self.now = 110
        self.poller.poll(43)
        self.assertEqual(self.poller.executor.submit.call_args.kwargs["offset"], 43)

    def test_backoff_throttle_recovery_and_no_credential_logging(self):
        secret = "FAKE_SECRET_URL"
        for delay in (10, 20, 40, 60, 60):
            future = Future()
            self.poller.executor.submit.return_value = future
            self.poller.poll(7)
            future.set_exception(TimeoutError(secret))
            self.assertEqual(self.poller.poll(7), [])
            self.assertEqual(self.poller.next_poll_at, self.now + delay)
            self.now += delay
        self.assertNotIn(secret, self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("receive error"), 3)
        future = Future()
        self.poller.executor.submit.return_value = future
        self.poller.poll(7)
        future.set_result([])
        self.poller.poll(7)
        self.assertEqual(self.poller.failures, 0)
        self.assertEqual(self.poller.next_poll_at, self.now + 10)
        self.assertIn("receive recovered", self.output.getvalue())

    def test_close_does_not_wait_and_prevents_new_requests(self):
        self.poller.close()
        self.poller.executor.shutdown.assert_called_with(wait=False, cancel_futures=True)
        self.assertEqual(self.poller.poll(None), [])
        self.poller.executor.submit.assert_not_called()

    def test_manager_processes_status_on_caller_and_filters_other_chats(self):
        tree = ast.parse((ROOT / "strategy/StrategyManager.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "StrategyManager")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "poll_telegram_commands")
        send = Mock()
        namespace = {"send_message": send, "Telegram_chat_ID": "1"}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "manager", "exec"), namespace)
        manager = SimpleNamespace(telegram_update_offset=None,
            telegram_command_poller=Mock(), build_status_message=Mock(return_value="STATUS"))
        manager.telegram_command_poller.poll.return_value = [
            {"update_id": 7, "message": {"chat": {"id": 2}, "text": "/status"}},
            {"update_id": 8, "message": {"chat": {"id": 1}, "text": "/status"}}]
        namespace["poll_telegram_commands"](manager)
        self.assertEqual(manager.telegram_update_offset, 9)
        send.assert_called_once_with("STATUS", chat_id="1")


class TelegramReceptionTest(unittest.TestCase):
    def setUp(self):
        self.notifier = load_notifier()

    def test_request_timeout_and_offset(self):
        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": [{"update_id": 12}]}
        self.notifier.requests.get.return_value = response
        self.assertEqual(self.notifier.get_updates(12, token="FAKE"), [{"update_id": 12}])
        kwargs = self.notifier.requests.get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], (3, 5))
        self.assertEqual(kwargs["params"]["offset"], 12)
        self.assertEqual(kwargs["params"]["timeout"], 0)

    def test_timeout_propagates_for_worker_and_default_log_is_safe(self):
        self.notifier.requests.get.side_effect = TimeoutError("FAKE_SECRET_URL")
        with self.assertRaises(TimeoutError):
            self.notifier.get_updates(token="FAKE", raise_errors=True)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.notifier.get_updates(token="FAKE"), [])
        self.assertNotIn("FAKE_SECRET_URL", output.getvalue())

    def test_api_failure_is_not_mistaken_for_successful_empty_poll(self):
        response = Mock(ok=False, status_code=401)
        response.json.return_value = {"ok": False, "description": "FAKE_SECRET"}
        self.notifier.requests.get.return_value = response
        with self.assertRaises(self.notifier.TelegramAPIError) as caught:
            self.notifier.get_updates(token="FAKE", raise_errors=True)
        self.assertEqual(str(caught.exception), "HTTP 401")

    def test_outgoing_telegram_url_token_is_redacted(self):
        message = "https://api.telegram.org/bot123456:FAKE_TOKEN/getUpdates"
        sanitized = self.notifier.sanitize_telegram_message(message)
        self.assertNotIn("FAKE_TOKEN", sanitized)
        self.assertIn("[토큰 숨김]", sanitized)


class RealWorkerTest(unittest.TestCase):
    def test_http_runs_on_worker_and_main_poll_stays_available(self):
        started, release = threading.Event(), threading.Event()
        caller_id = threading.get_ident()
        worker_ids = []
        def fetch(**kwargs):
            worker_ids.append(threading.get_ident())
            started.set()
            if not release.wait(2):
                raise RuntimeError("Test release timed out")
            return [{"update_id": 1}]
        poller = module.TelegramCommandPoller(fetch)
        try:
            self.assertEqual(poller.poll(None), [])
            self.assertTrue(started.wait(2))
            self.assertEqual(poller.poll(None), [])
            self.assertNotEqual(worker_ids[0], caller_id)
            release.set()
            poller.future.result(timeout=2)
            self.assertEqual(poller.poll(None), [{"update_id": 1}])
        finally:
            release.set()
            poller.close()


if __name__ == "__main__":
    unittest.main()
