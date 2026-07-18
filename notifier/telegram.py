"""Telegram Bot API를 통한 알림 발송.

환경변수:
  TELEGRAM_BOT_TOKEN  — stock_trader 프로젝트와 별도로 발급한 봇 토큰
  TELEGRAM_CHAT_ID    — 알림 받을 채팅 ID (사용자 ID 또는 그룹 ID)
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


def _send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    url = _API.format(token=token, method="sendMessage")
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                  "disable_web_page_preview": True},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Telegram 발송 실패: %s", exc)
        return False


def notify_report(
    scan_date: str,
    signal_count: int,
    report_url: str,
    news_summary: str,
    top_tickers: list[str],
) -> None:
    """일별 리포트 요약 알림 발송."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 — Telegram 알림 건너뜀")
        return

    ticker_str = ", ".join(top_tickers[:5])
    if len(top_tickers) > 5:
        ticker_str += f" 외 {len(top_tickers) - 5}개"

    lines = [f"<b>📊 마켓 대시보드 — {scan_date}</b>"]

    if signal_count == 0:
        lines.append("오늘은 신호 종목이 없습니다.")
    else:
        lines.append(f"신호 종목: <b>{signal_count}개</b>")
        if ticker_str:
            lines.append(f"주목 종목: {ticker_str}")

    if news_summary:
        short = news_summary[:200] + ("…" if len(news_summary) > 200 else "")
        lines.append(f"\n📰 국내 시황: {short}")

    lines.append(f"\n🔗 <a href='{report_url}'>리포트 전체 보기</a>")

    _send(token, chat_id, "\n".join(lines))
    logger.info("Telegram 알림 발송 완료")
