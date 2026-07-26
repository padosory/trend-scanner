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
        # Telegram은 400 등 실패 시 본문에 사유(description)를 담아준다 — 이를 같이 남겨야
        # "chat not found" / "chat_id is empty" 같은 실제 원인을 알 수 있다.
        detail = ""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                detail = f" — {resp.json().get('description', resp.text)}"
            except ValueError:
                detail = f" — {resp.text}"
        logger.error("Telegram 발송 실패: %s%s", exc, detail)
        return False


def notify_report(
    scan_date: str,
    signal_count: int,
    report_url: str,
    news_summary: str,
    top_tickers: list[str],
    funnel=None,
    perf_summary=None,
) -> None:
    """일별 리포트 요약 알림 발송.

    funnel(collectors.stocks.ScanFunnel)·perf_summary(signal_tracker.PerfSummary)는
    선택 인자로, 있으면 각각 '왜 관망인지'와 누적 성과를 압축 한 줄로 덧붙인다.
    """
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
        if funnel is not None and funnel.breakout > 0:
            lines.append(
                f"돌파 <b>{funnel.breakout}개</b> → 주도주(RS) <b>0개</b> · 오늘은 관망입니다."
            )
        else:
            lines.append("돌파 종목이 없습니다 · 오늘은 관망입니다.")
    else:
        lines.append(f"신호 종목: <b>{signal_count}개</b>")
        if funnel is not None:
            lines.append(f"<i>돌파 {funnel.breakout} → 주도주(RS) {funnel.rs}</i>")
        if ticker_str:
            lines.append(f"주목 종목: {ticker_str}")

    if perf_summary is not None:
        lines.append(
            f"\n📈 성과(최근 {perf_summary.window_days}일): 승률 <b>{perf_summary.win_rate:.0f}%</b>"
            f" · 평균 <b>{perf_summary.avg_return:+.2f}%</b>"
            f" · 청산 {perf_summary.n_closed}건/진행중 {perf_summary.n_open}건"
        )

    if news_summary:
        short = news_summary[:200] + ("…" if len(news_summary) > 200 else "")
        lines.append(f"\n📰 국내 시황: {short}")

    lines.append(f"\n🔗 <a href='{report_url}'>리포트 전체 보기</a>")

    _send(token, chat_id, "\n".join(lines))
    logger.info("Telegram 알림 발송 완료")
