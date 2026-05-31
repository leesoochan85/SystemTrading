from datetime import date, datetime, time


REGULAR_OPEN_TIME = time(9, 0)
REGULAR_CLOSE_TIME = time(15, 30)
BUY_CUTOFF_TIME = time(15, 15)


# 2026년 프로그램 운용 기간 중 장을 열지 않아야 하는 날짜.
#
# 주의:
# - 2026-06-03, 2026-07-17은 한국거래소 휴장 발표가 확인된 날짜다.
# - 그 외 공휴일·대체공휴일·연말 휴장은 실제 운용 전
#   한국거래소 공지와 다시 대조하는 것이 안전하다.
# - 2027년으로 넘어가기 전에 반드시 갱신해야 한다.
MARKET_HOLIDAYS = {
    date(2026, 6, 3),    # 지방선거일: KRX 휴장 발표 확인
    date(2026, 7, 17),   # 제헌절: KRX 휴장 발표 확인
    date(2026, 8, 17),   # 광복절 대체공휴일
    date(2026, 9, 24),   # 추석 연휴
    date(2026, 9, 25),   # 추석
    date(2026, 9, 28),   # 추석 대체공휴일
    date(2026, 10, 5),   # 개천절 대체공휴일
    date(2026, 10, 9),   # 한글날
    date(2026, 12, 25),  # 성탄절
    date(2026, 12, 31),  # 연말 휴장 예상일: KRX 연말 공지 후 재확인 필요
}


# 수능일·연초 개장일처럼 정규장 시작 시간이 달라지는 날을 추가한다.
# 예: date(2027, 1, 4): time(10, 0)
SPECIAL_MARKET_OPEN_TIMES = {}


def is_market_business_day(now=None):
    """
    오늘이 전략 매매를 수행할 수 있는 거래일인지 확인한다.
    """
    now = now or datetime.now()

    # 월요일=0, ..., 토요일=5, 일요일=6
    if now.weekday() >= 5:
        return False

    if now.date() in MARKET_HOLIDAYS:
        return False

    return True


def get_market_open_time(now=None):
    """
    특정 날짜에 개장 시간이 별도로 지정되어 있으면 해당 시간을 반환한다.
    """
    now = now or datetime.now()

    return SPECIAL_MARKET_OPEN_TIMES.get(
        now.date(),
        REGULAR_OPEN_TIME,
    )


def check_transaction_open(now=None):
    """
    현재 시점이 실제 전략 주문 검사를 수행할 정규장 시간인지 확인한다.
    """
    now = now or datetime.now()

    if not is_market_business_day(now):
        return False

    open_time = get_market_open_time(now)

    return open_time <= now.time() <= REGULAR_CLOSE_TIME


def check_transaction_closed(now=None):
    """
    거래일의 정규장이 종료된 상태인지 확인한다.

    휴장일에는 장 종료로 판단하지 않는다.
    휴장일에 일봉 갱신이나 장 마감 처리 로직이 잘못 실행되는 것을 막기 위함이다.
    """
    now = now or datetime.now()

    if not is_market_business_day(now):
        return False

    return now.time() > REGULAR_CLOSE_TIME


def check_adjacent_transaction_closed_for_buying(now=None):
    """
    신규 매수 제한 시간인지 확인한다.

    - 휴장일: 신규 매수 금지
    - 거래일 15:15~15:30: 신규 매수 금지
    """
    now = now or datetime.now()

    if not is_market_business_day(now):
        return True

    return BUY_CUTOFF_TIME <= now.time() <= REGULAR_CLOSE_TIME