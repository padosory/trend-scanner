"""HTML 리포트 조립 및 파일 저장."""

import logging
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent.parent / "reports"
_TEMPLATE_DIR = Path(__file__).parent


def build(
    scan_date: pd.Timestamp,
    signals: list,
    macro,
    kr_summary: str,
    global_summary: str,
    kr_news_items: list,
    global_news_items: list,
    name_map: dict[str, str],
    comments: dict[str, str],
    charts: dict[str, str],
    dart_data: dict | None = None,
    perf_rows: list | None = None,
    perf_summary=None,
    funnel=None,
    watchlist: list | None = None,
) -> Path:
    """HTML 리포트를 생성하고 저장 경로를 반환한다.

    Args:
        scan_date: 스캔 기준 거래일
        signals: list[StockSignal]
        macro: MacroData | None
        kr_summary: 국내 시황 Gemini AI 요약 문자열
        global_summary: 글로벌 시황 Gemini AI 요약 문자열
        kr_news_items: list[NewsItem] (국내)
        global_news_items: list[NewsItem] (글로벌)
        name_map: {ticker: 종목명}
        comments: {ticker: AI 코멘트}
        charts: {ticker: plotly div HTML}

    Returns:
        저장된 리포트 파일 경로
    """
    REPORTS_DIR.mkdir(exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=False)
    template = env.get_template("template.html")

    html = template.render(
        scan_date=scan_date.strftime("%Y-%m-%d"),
        signals=signals,
        macro=macro,
        kr_summary=kr_summary,
        global_summary=global_summary,
        kr_news_items=kr_news_items,
        global_news_items=global_news_items,
        name_map=name_map,
        comments=comments,
        charts=charts,
        dart_data=dart_data or {},
        perf_rows=perf_rows or [],
        perf_summary=perf_summary,
        funnel=funnel,
        watchlist=watchlist or [],
    )

    date_str = scan_date.strftime("%Y%m%d")
    out_path = REPORTS_DIR / f"report_{date_str}.html"
    out_path.write_text(html, encoding="utf-8")

    (REPORTS_DIR / "index.html").write_text(html, encoding="utf-8")

    logger.info("리포트 저장: %s", out_path)
    return out_path
