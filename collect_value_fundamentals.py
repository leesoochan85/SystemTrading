"""64bit financial collector. Reads DART_API_KEY from environment/.env."""
from datetime import datetime, timedelta
from pathlib import Path
import argparse
import json
import os
import re
import struct
import sys
import time

from util.value_quality_data import get_db_path, get_fundamental_status


def main():
    started = time.monotonic()
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", help="comma-separated six-digit stock codes")
    parser.add_argument("--limit", type=int, default=20, help="0=all; first run defaults to 20")
    parser.add_argument("--stats", action="store_true", help="inspect existing financial DB without API")
    parser.add_argument("--check-market", action="store_true", help="test KRX market cap without DART calls or DB writes")
    parser.add_argument("--force", action="store_true", help="bypass report cache and same-date checkpoints")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    codes = args.codes.split(",") if args.codes else None
    if codes:
        codes = list(dict.fromkeys(c.strip() for c in codes))
        if any(not re.fullmatch(r"\d{6}", c) for c in codes):
            parser.error("--codes requires six-digit codes")
    path = get_db_path()
    print(f"[ValueCollect DB] {path}")
    if args.stats:
        print(json.dumps(get_fundamental_status(path), ensure_ascii=False, indent=2))
        return 0
    if struct.calcsize("P") * 8 != 64:
        parser.error("수집은 64bit marketdata 환경에서 실행하세요. --stats는 32bit도 가능합니다.")
    if sys.version_info < (3, 10):
        parser.error("최신 pykrx 수집에는 Python 3.10 이상 64bit가 필요합니다. python setup_value_data_env.py를 실행하세요.")
    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env", interpolate=False)
    except ImportError:
        pass  # OS environment works without python-dotenv.
    from util.value_fundamental_collector import DartClient, DartFailure, DataUnavailable, market_caps, run_collection
    try:
        if args.check_market:
            quote_date, caps = market_caps(datetime.now().date() - timedelta(days=1))
            print(f"[KRX check OK] date={quote_date} rows={len(caps)}")
            print(caps[["시가총액", "상장주식수"]].head())
            return 0
        # Validate credentials before any schema or data write.
        client = DartClient(os.getenv("DART_API_KEY", ""), root / "data/value_cache",
                            force_refresh=args.force)
        quote_date, caps = market_caps(datetime.now().date() - timedelta(days=1))
        companies = client.companies()
        client.prepare_filings(quote_date)
        result = run_collection(client, path, quote_date, caps, companies,
                                codes=codes, limit=args.limit, resume=not args.force)
    except (DartFailure, DataUnavailable) as exc:
        print(f"[ValueCollect stopped] {exc}")
        return 2
    reports = root / "data/value_reports"
    result["total_elapsed_seconds"] = round(time.monotonic() - started, 2)
    reports.mkdir(parents=True, exist_ok=True)
    output = reports / ("collection_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("[ValueCollect status]", json.dumps(get_fundamental_status(path), ensure_ascii=False))
    return 2 if result.get("stopped") else (0 if result["saved"] or result["resumed_saved"] else 3)


if __name__ == "__main__":
    raise SystemExit(main())
