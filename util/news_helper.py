import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin


NAVER_ECONOMY_SECTION_URL = "https://news.naver.com/section/101"


def fetch_naver_economy_news(max_items=10):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://news.naver.com/",
    }

    response = requests.get(
        NAVER_ECONOMY_SECTION_URL,
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    news_items = []
    seen_links = set()

    selectors = [
        "a.sa_text_title",
        "a.cluster_text_headline",
        "a.sh_text_headline",
        "a[href*='/article/']",
    ]

    for selector in selectors:
        for a_tag in soup.select(selector):
            title = a_tag.get_text(" ", strip=True)
            link = a_tag.get("href", "")

            if not title or not link:
                continue

            link = urljoin("https://news.naver.com", link)

            if "/article/" not in link:
                continue

            if link in seen_links:
                continue

            seen_links.add(link)
            news_items.append({
                "title": title,
                "link": link,
            })

            if len(news_items) >= max_items:
                return news_items

    return news_items


def format_naver_economy_news_message(news_items): #뉴스를 한 번에 여러 개 보내는 경우, 이미 보낸 뉴스는 제외하는 로직 추가
    if not news_items:
        return "[네이버 경제 뉴스]\n가져온 뉴스가 없습니다. selector 또는 네이버 페이지 구조를 확인해야 합니다."

    lines = ["[네이버 경제 뉴스]"]

    for idx, item in enumerate(news_items, start=1):
        lines.append("")
        lines.append(f"{idx}. {item['title']}")
        lines.append(item["link"])

    return "\n".join(lines)