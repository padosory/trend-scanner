"""한국 금융 뉴스 RSS 수집.

feedparser 패키지 필요. 피드 목록은 아래 RSS_FEEDS에 정의.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    ("연합인포맥스", "https://news.einfomax.co.kr/rss/allnews.xml"),
    ("한국경제", "https://rss.hankyung.com/feed/news-all.xml"),
    ("머니투데이", "https://rss.mt.co.kr/rss/news.xml"),
    ("이데일리", "https://rss.edaily.co.kr/edaily/stock.xml"),
]


@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    published: str


def fetch(max_per_feed: int = 5) -> list[NewsItem]:
    """RSS 피드에서 최신 뉴스를 수집해 NewsItem 목록으로 반환한다."""
    import feedparser

    items: list[NewsItem] = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                items.append(
                    NewsItem(
                        source=source,
                        title=entry.get("title", "").strip(),
                        link=entry.get("link", ""),
                        published=entry.get("published", ""),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RSS 수집 실패 (%s): %s", source, exc)

    logger.info("뉴스 %d건 수집 (%d개 피드)", len(items), len(RSS_FEEDS))
    return items
