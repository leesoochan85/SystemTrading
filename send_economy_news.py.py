from util.news_helper import (
    fetch_naver_economy_news,
    format_naver_economy_news_message,
)
from util.notifier import send_message


def send_naver_economy_news(max_items=10):
    news_items = fetch_naver_economy_news(max_items=max_items)
    message = format_naver_economy_news_message(news_items)
    return send_message(message)


if __name__ == "__main__":
    send_naver_economy_news(max_items=10)