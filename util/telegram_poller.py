"""Nonblocking command reception. Call poll() only from the Qt main thread."""
import time
from concurrent.futures import ThreadPoolExecutor


class TelegramCommandPoller:
    def __init__(self, fetch_updates, *, clock=time.monotonic):
        self.fetch_updates = fetch_updates
        self.clock = clock
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-receive")
        self.future = None
        self.next_poll_at = 0.0
        self.failures = 0
        self.last_error_log_at = None
        self.suppressed_errors = 0
        self.closed = False

    def poll(self, offset):
        """Return completed updates; never wait on a running HTTP request."""
        if self.closed:
            return []
        now = self.clock()
        if self.future is not None:
            if not self.future.done():
                return []
            future, self.future = self.future, None
            try:
                updates = future.result()
            except Exception as exc:
                self.failures += 1
                delay = (10, 20, 40, 60)[min(self.failures - 1, 3)]
                self.next_poll_at = now + delay
                if self.last_error_log_at is None or now - self.last_error_log_at >= 60:
                    # Exception strings may include the entire bot credential URL.
                    print(
                        f"[Telegram receive error] {type(exc).__name__}; "
                        f"retry in {delay}s; suppressed={self.suppressed_errors}"
                    )
                    self.last_error_log_at = now
                    self.suppressed_errors = 0
                else:
                    self.suppressed_errors += 1
                return []
            if self.failures:
                print(f"[Telegram receive recovered] failures={self.failures}; "
                      f"suppressed={self.suppressed_errors}")
            self.failures = 0
            self.last_error_log_at = None
            self.suppressed_errors = 0
            self.next_poll_at = now + 10
            return updates
        if now >= self.next_poll_at:
            self.future = self.executor.submit(
                self.fetch_updates, offset=offset,
                request_timeout=(3, 5), raise_errors=True,
            )
        return []

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
