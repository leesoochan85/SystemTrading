"""Stable experiment identities shared by the tick engine and read-only API."""
from pathlib import Path

PERIODS = (
    {"id": "1m", "label": "1개월", "sessions": 21, "mode": "return_21", "field": "return21_pct"},
    {"id": "3m", "label": "3개월", "sessions": 63, "mode": "return_63", "field": "return63_pct"},
    {"id": "6m", "label": "6개월", "sessions": 126, "mode": "return_126", "field": "return126_pct"},
)


def period_db_path(base_path, period_id):
    if period_id not in {p["id"] for p in PERIODS}:
        raise ValueError("unknown momentum period")
    base = Path(base_path)
    return base.with_name(f"{base.stem}_momentum_{period_id}_v1.db")
