from datetime import datetime

def check_transaction_open(): #장 중인지 확인
    now = datetime.now()
    start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end_time = now.replace(hour=20, minute=00, second=0, microsecond=0)
    return start_time <= now <= end_time

def check_transaction_closed():
    now = datetime.now()
    end_time = now.replace(hour=20, minute=00, second=0, microsecond=0)
    return end_time < now

# def check_adjacent_transaction_closed_for_buying(): #매수 시 장 마감 10분 전인지 확인
#     now = datetime.now()
#     base_time = now.replace(hour=15, minute=0, second=0, microsecond=0)
#     end_time = now.replace(hour=15, minute=20, second=0, microsecond=0)


#     return base_time <= now < end_time