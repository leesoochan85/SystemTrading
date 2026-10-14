"""Same-date collection checkpoints. No credentials or API payloads are stored."""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
import time

COLLECTION_VERSION = "ttm-quality-v1"


def _connect(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.execute("""CREATE TABLE IF NOT EXISTS value_collection_checkpoint (
        code TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        completed_at REAL NOT NULL, outcome TEXT NOT NULL
    )""")
    con.commit()
    return con


def fingerprint(quote_date, company, cap, shares, generation):
    value = [COLLECTION_VERSION, quote_date.isoformat(), company,
             str(cap), str(shares), generation]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_completed(db_path):
    with closing(_connect(db_path)) as con:
        return {code: (key, stamp, outcome) for code, key, stamp, outcome in
                con.execute("SELECT * FROM value_collection_checkpoint")}


def checkpoint_matches(entry, key):
    # Missing-report results are retried after six hours even on the same date.
    return bool(entry and entry[0] == key and 0 <= time.time() - entry[1] < 6 * 3600)


def complete(db_path, code, key, outcome):
    with closing(_connect(db_path)) as con, con:
        con.execute("""INSERT INTO value_collection_checkpoint VALUES (?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET fingerprint=excluded.fingerprint,
            completed_at=excluded.completed_at, outcome=excluded.outcome""",
            (code, key, time.time(), outcome))
