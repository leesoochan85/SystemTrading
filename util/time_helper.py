from datetime import datetime

def check_transaction_open(): #장 중인지 확인
    now = datetime.now()
    start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start_time <= now <= end_time

def check_transaction_closed():
    now = datetime.now()
    end_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return end_time < now

def check_adjacent_transaction_closed_for_buying():
    now = datetime.now()
    cutoff = now.replace(hour=15, minute=15, second=0, microsecond=0)
    end_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return cutoff <= now <= end_time