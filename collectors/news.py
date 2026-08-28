"""금융 뉴스 수집 — 국내는 네이버 검색 API, 글로벌은 Finnhub API.

기존 RSS 방식은 언론사가 피드 경로를 바꾸면 예외 없이 0건이 되는 무음 실패가
잦아 API 기반으로 교체했다. 국내/글로벌 뉴스를 각각의 fetch 함수로 수집한다.

⚠️ 두 API 모두 과거 시점 조회를 지원하지 않는다 — 언제나 '호출 시점의 최신'
뉴스만 돌아온다. 즉 뉴스는 스캔 기준일이 아니라 실행 시각에 묶인다(정기 실행인
05:13 KST에는 '전일 마감~개장 전'이라 사실상 일치하지만, 장중 수동 실행이면
어긋난다). 그래서 수집 시각과 항목별 발행 시각을 리포트에 그대로 표시한다.

국내 뉴스는 네이버 개발자센터(developers.naver.com) 검색 API를 사용한다.
(검색 API는 NAVER API HUB(NCP)로 이관 예정이나, NCP는 무료 쿼터에도 결제수단
등록이 필요해 카드 없이 무료로 쓸 수 있는 개발자센터 키를 사용한다. 개발자센터
신규 발급은 2026-07-31까지, 발급 키는 2027-06-30까지 유효 — 그 전에 이관 필요.)

필요 환경변수:
    NAVER_CLIENT_ID, NAVER_CLIENT_SECRET  — 네이버 검색 API (developers.naver.com)
    FINNHUB_API_KEY                        — Finnhub 시장 뉴스 (finnhub.io)
"""

import datetime as dt
import html
import logging
import os
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

KST = dt.timezone(dt.timedelta(hours=9))

# 국내 시황 관련 네이버 검색 키워드 — 폭넓게 시장 분위기를 잡되 중복은 제목으로 제거
NAVER_QUERIES = ["코스피", "코스닥", "증시", "환율", "반도체"]
# 네이버 개발자센터 뉴스 검색 엔드포인트
# (NCP API HUB 이관 시 naverapihub.apigw.ntruss.com/search/v1/news + X-NCP-APIGW-* 헤더로 교체)
_NAVER_URL = "https://openapi.naver.com/v1/search/news.json"

_FINNHUB_URL = "https://finnhub.io/api/v1/news"

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    published: str       # 원본 문자열 (네이버: RFC822, Finnhub: epoch)
    published_kst: str = ""  # 표시용 KST "MM-DD HH:MM". 파싱 실패 시 빈 문자열


def _clean(text: str) -> str:
    """네이버 응답의 HTML 태그(<b> 등)와 엔티티를 제거해 순수 텍스트로 만든다."""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def now_kst_label() -> str:
    """뉴스 수집 시각 표시용 문자열 (KST). 리포트에 '언제 받은 뉴스인지' 밝히는 용도."""
    return dt.datetime.now(KST).strftime("%m-%d %H:%M")


def _kst_from_rfc822(value: str) -> str:
    """네이버 pubDate('Tue, 28 Jul 2026 12:34:00 +0900') → 'MM-DD HH:MM' (KST)."""
    try:
        return parsedate_to_datetime(value).astimezone(KST).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _kst_from_epoch(value) -> str:
    """Finnhub datetime(Unix epoch, UTC) → 'MM-DD HH:MM' (KST)."""
    try:
        return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).astimezone(KST).strftime(
            "%m-%d %H:%M"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _domain(url: str) -> str:
    """originallink 도메인에서 언론사 이름 대용 라벨을 뽑는다."""
    host = urlparse(url).netloc.replace("www.", "")
    return host.split(".")[0] if host else "뉴스"


def fetch_korea(per_query: int = 5, limit: int = 15) -> list[NewsItem]:
    """네이버 검색 API로 국내 시황 뉴스를 수집한다 (최신순, 제목 기준 중복 제거)."""
    import requests

    client_id = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.warning("NAVER_CLIENT_ID/SECRET 없음 — 국내 뉴스 스킵")
        return []

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    items: list[NewsItem] = []
    seen: set[str] = set()
    for query in NAVER_QUERIES:
        try:
            resp = requests.get(
                _NAVER_URL,
                headers=headers,
                params={"query": query, "display": per_query, "sort": "date"},
                timeout=10,
            )
            resp.raise_for_status()
            for entry in resp.json().get("items", []):
                title = _clean(entry.get("title", ""))
                if not title or title in seen:
                    continue
                seen.add(title)
                link = entry.get("originallink") or entry.get("link", "")
                pub = entry.get("pubDate", "")
                items.append(
                    NewsItem(
                        source=_domain(link),
                        title=title,
                        link=link,
                        published=pub,
                        published_kst=_kst_from_rfc822(pub),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("네이버 뉴스 수집 실패 (%s): %s", query, exc)

    items = items[:limit]
    logger.info("국내 뉴스 %d건 수집 (%d개 키워드)", len(items), len(NAVER_QUERIES))
    return items


def fetch_global(limit: int = 12) -> list[NewsItem]:
    """Finnhub API로 글로벌 시장 뉴스를 수집한다 (general 카테고리, 최신순)."""
    import requests

    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        logger.warning("FINNHUB_API_KEY 없음 — 글로벌 뉴스 스킵")
        return []

    items: list[NewsItem] = []
    try:
        resp = requests.get(
            _FINNHUB_URL,
            params={"category": "general", "token": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # datetime(Unix epoch) 최신순 정렬 후 상위 N건
        data.sort(key=lambda e: e.get("datetime", 0), reverse=True)
        for entry in data[:limit]:
            title = (entry.get("headline") or "").strip()
            if not title:
                continue
            items.append(
                NewsItem(
                    source=entry.get("source", "Finnhub"),
                    title=title,
                    link=entry.get("url", ""),
                    published=str(entry.get("datetime", "")),
                    published_kst=_kst_from_epoch(entry.get("datetime")),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Finnhub 뉴스 수집 실패: %s", exc)

    logger.info("글로벌 뉴스 %d건 수집", len(items))
    return items
