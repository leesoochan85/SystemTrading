"""Per-call database configuration shared by the persistence repositories."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Callable, Optional


@dataclass(frozen=True)
class RepositoryContext:
    monitoring_db: str = "monitoring.db"
    position_db: str = "strategy_position.db"
    clock: Callable[[], datetime] = datetime.now
    timestamp: Optional[Callable[[], str]] = None

    def now_text(self):
        if self.timestamp is not None:
            return self.timestamp()
        return self.clock().strftime("%Y%m%d%H%M%S")

    @contextmanager
    def connect(self, path, **options):
        """Keep sqlite transaction semantics and close the handle on every exit."""
        with closing(sqlite3.connect(path, **options)) as connection:
            with connection:
                yield connection
