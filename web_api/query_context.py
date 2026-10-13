"""Read-only SQLite access shared by monitoring query modules."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Any
import json
import sqlite3


class DatabaseNotReadyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReadOnlyDatabase:
    path: Path
    busy_timeout_ms: int = 5000

    @contextmanager
    def connect(self):
        path = Path(self.path).resolve()
        if not path.exists():
            raise DatabaseNotReadyError(f"DB 파일을 찾을 수 없습니다: {path}")
        con = sqlite3.connect(str(path), timeout=self.busy_timeout_ms / 1000)
        con.row_factory = sqlite3.Row
        try:
            con.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            con.execute("PRAGMA query_only = ON")
            yield con
        finally:
            con.close()

    def table_exists(self, table_name):
        if not Path(self.path).exists():
            return False
        with self.connect() as con:
            row = con.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name=? LIMIT 1""", (table_name,)).fetchone()
        return row is not None


@dataclass(frozen=True)
class VirtualQueryContext:
    monitoring: ReadOnlyDatabase
    paper: ReadOnlyDatabase
    clock: Callable[[], datetime] = datetime.now


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def safe_json_loads(value):
    if value in (None, ""):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value
