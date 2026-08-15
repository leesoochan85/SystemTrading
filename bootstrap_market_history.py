"""64bit Python 전용 과거 일봉 수집기.

예시:
    C:\\Users\\leesoochan\\anaconda3\\envs\\marketdata\\python.exe bootstrap_market_history.py

Kiwoom용 Python39-32로 실행하지 않는다.
"""

from dotenv import load_dotenv

load_dotenv()

import argparse
import platform
import struct

from util.market_history import (
    DEFAULT_LOOKBACK_CALENDAR_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_CALENDAR_DAYS,
    bootstrap_market_history,
    get_db_path,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_CALENDAR_DAYS,
        help="달력일 기준 과거 조회 범위",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=DEFAULT_INCREMENTAL_OVERLAP_CALENDAR_DAYS,
        help="기존 데이터가 충분한 종목의 증분 보충 시 마지막 저장일 이전 재조회 달력일 수",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="pykrx 종목별 요청 간 대기시간(초). 전체시장 수집은 서버 부하 방지를 위해 기본 1초",
    )
    args = parser.parse_args()

    bitness = struct.calcsize("P") * 8
    print(f"[수집기 Python] {platform.python_version()} / {bitness}bit")
    print(f"[공유 DB] {get_db_path()}")

    if bitness != 64:
        raise RuntimeError(
            "bootstrap_market_history.py는 64bit Python 전용입니다. "
            "Kiwoom용 Python39-32가 아닌 marketdata 64bit Python으로 실행하세요."
        )

    result = bootstrap_market_history(
        lookback_calendar_days=args.lookback_days,
        request_sleep=args.sleep,
        incremental_overlap_calendar_days=args.overlap_days,
    )
    print("[초기 일봉 구축 완료]", result)


if __name__ == "__main__":
    main()
