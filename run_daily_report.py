"""일별 스캔 → HTML 리포트 → Telegram 알림 메인 스크립트.

사용법:
  python run_daily_report.py                   # 어제 날짜 기준 스캔
  python run_daily_report.py --date 20260625   # 특정 날짜 스캔
  python run_daily_report.py --skip-ai         # Gemini 없이 실행
  python run_daily_report.py --skip-telegram   # Telegram 알림 건너뜀
"""

import argparse
import logging
import os

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="daily trend scan + report + Telegram")
    parser.add_argument("--date", help="YYYYMMDD. 기본: 어제")
    parser.add_argument("--skip-ai", action="store_true", help="Gemini AI 분석 건너뜀")
    parser.add_argument("--skip-telegram", action="store_true", help="Telegram 알림 건너뜀")
    args = parser.parse_args()

    target_date = args.date or (pd.Timestamp.now() - pd.Timedelta(days=1)).strftime("%Y%m%d")
    logger.info("=== 일별 스캔 시작 (타겟: %s) ===", target_date)

    # ── 1. 주식 STEP2+RS 스캔 ──────────────────────────────────────────────
    from collectors.stocks import scan as stock_scan
    signals, effective_date = stock_scan(target_date)

    # ── 2. 종목명 맵 ────────────────────────────────────────────────────────
    import FinanceDataReader as fdr
    listing = fdr.StockListing("KRX")
    name_map: dict[str, str] = dict(zip(listing["Code"], listing["Name"]))

    # ── 3. 거시경제 수집 ─────────────────────────────────────────────────────
    from collectors.macro import fetch as macro_fetch
    macro = macro_fetch(effective_date.strftime("%Y%m%d"))

    # ── 4. 뉴스 수집 ────────────────────────────────────────────────────────
    from collectors.news import fetch as news_fetch
    news_items = news_fetch()

    # ── 5. AI 분석 ──────────────────────────────────────────────────────────
    news_summary = ""
    comments: dict[str, str] = {}
    if not args.skip_ai:
        try:
            from ai_analyzer.summarizer import analyze
            news_summary, comments = analyze(news_items, signals, name_map)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 분석 실패 (건너뜀): %s", exc)

    # ── 6. 차트 생성 ─────────────────────────────────────────────────────────
    from backtest.data_cache import fetch_ohlcv
    from report_builder.charts import make_chart_div
    from scanners.indicators import add_indicators

    charts: dict[str, str] = {}
    chart_start = (effective_date - pd.DateOffset(days=180)).strftime("%Y%m%d")
    chart_end = effective_date.strftime("%Y%m%d")
    for signal in signals:
        try:
            df = fetch_ohlcv(signal.ticker, chart_start, chart_end)
            df = add_indicators(df)
            charts[signal.ticker] = make_chart_div(signal.ticker, df, effective_date,
                                                   high_52w=signal.high_52w)
        except Exception as exc:  # noqa: BLE001
            logger.debug("차트 생성 실패 %s: %s", signal.ticker, exc)

    # ── 7. 리포트 빌드 ────────────────────────────────────────────────────────
    from report_builder.builder import build as build_report
    out_path = build_report(
        scan_date=effective_date,
        signals=signals,
        macro=macro,
        news_summary=news_summary,
        news_items=news_items,
        name_map=name_map,
        comments=comments,
        charts=charts,
    )
    logger.info("리포트 완료: %s", out_path)

    # ── 8. Telegram 알림 ──────────────────────────────────────────────────────
    if not args.skip_telegram:
        pages_base = os.environ.get("GITHUB_PAGES_URL", "")
        if pages_base:
            report_url = f"{pages_base}/report_{effective_date.strftime('%Y%m%d')}.html"
        else:
            report_url = out_path.as_uri()

        from notifier.telegram import notify_report
        notify_report(
            scan_date=effective_date.strftime("%Y-%m-%d"),
            signal_count=len(signals),
            report_url=report_url,
            news_summary=news_summary,
            top_tickers=[s.ticker for s in signals],
        )

    logger.info("=== 완료 ===")


if __name__ == "__main__":
    main()
