"""Optional C-candidate/holding execution capture, for offline paper replay."""
from pathlib import Path
import csv
import time


COLUMNS = ("received_at", "registered_at", "code", "trade_time", "price",
           "day_low", "cumulative_volume", "trade_volume")


class PaperTickRecorder:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if self.path.suffix.lower() != ".csv":
            raise ValueError("tick recording path must end in .csv")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        has_content = self.path.exists() and self.path.stat().st_size > 0
        if has_content:
            with self.path.open(encoding="utf-8-sig", newline="") as existing:
                if next(csv.reader(existing), None) != list(COLUMNS):
                    raise ValueError("existing tick CSV header differs; use a new path")
        self.file = self.path.open("a", encoding="utf-8", newline="")
        self.writer = csv.writer(self.file)
        self.last_flush = time.monotonic()
        if not has_content:
            self.writer.writerow(COLUMNS)
            self.file.flush()

    def record(self, code, tick, received_at, registered_at):
        if registered_at is None:
            return  # Cannot claim that subscription was active before this point.
        self.writer.writerow((received_at.strftime("%Y%m%d%H%M%S"),
            registered_at.strftime("%Y%m%d%H%M%S"), str(code).zfill(6),
            tick.get("체결시간", ""), tick.get("현재가", 0), tick.get("저가", 0),
            tick.get("누적거래량", 0), tick.get("거래량", 0)))
        if time.monotonic() - self.last_flush >= 1:
            self.flush()

    def flush(self):
        self.file.flush()
        self.last_flush = time.monotonic()

    def close(self):
        self.file.close()
