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
    signals, effective_date, funnel, watchlist = stock_scan(target_date)

    # ── 2. 종목명 맵 ────────────────────────────────────────────────────────
    import FinanceDataReader as fdr
    listing = fdr.StockListing("KRX")
    name_map: dict[str, str] = dict(zip(listing["Code"], listing["Name"]))

    # ── 3. 거시경제 수집 ─────────────────────────────────────────────────────
    from collectors.macro import fetch as macro_fetch
    macro = macro_fetch(effective_date.strftime("%Y%m%d"))

    # BTC 도미넌스 등락은 전일 스냅샷과 비교해 계산 (무료 API가 현재값만 제공)
    if macro is not None and macro.btc_dominance == macro.btc_dominance:
        import macro_state
        prev = macro_state.load()
        today_str = macro.date.strftime("%Y-%m-%d")
        prev_dom = prev.get("btc_dominance")
        if prev_dom and prev.get("date") != today_str:
            macro.btc_dominance_change_pct = (macro.btc_dominance / prev_dom - 1) * 100
        macro_state.save({"date": today_str, "btc_dominance": macro.btc_dominance})

    # ── 4. 뉴스 수집 (국내: 네이버 API, 글로벌: Finnhub API) ──────────────────
    from collectors.news import fetch_global, fetch_korea
    kr_news_items = fetch_korea()
    global_news_items = fetch_global()

    # ── 4-1. DART 공시 + 재무지표 ────────────────────────────────────────────
    dart_api_key = os.environ.get("DART_API_KEY", "")
    dart_data: dict[str, object] = {}
    if not dart_api_key:
        logger.info("DART_API_KEY 없음 — 공시/재무 스킵")
    elif not signals:
        logger.info("신호 종목 없음 — DART 조회 스킵")
    else:
        from collectors.dart import fetch as dart_fetch
        for signal in signals:
            result = dart_fetch(dart_api_key, signal.ticker, effective_date.strftime("%Y%m%d"))
            if result:
                dart_data[signal.ticker] = result
        logger.info("DART 데이터 수집 완료: %d/%d 종목", len(dart_data), len(signals))

    # ── 5. AI 분석 ──────────────────────────────────────────────────────────
    kr_summary = ""
    global_summary = ""
    comments: dict[str, str] = {}
    if not args.skip_ai:
        try:
            from ai_analyzer.summarizer import analyze
            kr_summary, global_summary, comments = analyze(
                kr_news_items, global_news_items, signals, name_map
            )
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

    # ── 6-1. 신호 히스토리 기록 + 성과 추적 ──────────────────────────────────
    import signal_tracker
    signal_tracker.record(signals, effective_date, name_map)

    # 추적기는 저항선 재이탈(채택 전략 청산)을 판정하려면 resistance_60까지 필요하므로
    # 종가만이 아니라 지표가 적용된 OHLCV DataFrame을 넘긴다. 저항선 계산에 충분한
    # 과거 구간(약 300일)을 확보해 최근 90일 신호까지 정상 평가되게 한다.
    track_start = (effective_date - pd.DateOffset(days=300)).strftime("%Y%m%d")
    _ohlcv_cache: dict[str, "pd.DataFrame | None"] = {}

    def _ohlcv_lookup(ticker: str) -> "pd.DataFrame | None":
        if ticker not in _ohlcv_cache:
            try:
                df = fetch_ohlcv(ticker, track_start, chart_end)
                _ohlcv_cache[ticker] = add_indicators(df)
            except Exception:  # noqa: BLE001
                _ohlcv_cache[ticker] = None
        return _ohlcv_cache[ticker]

    perf_rows = signal_tracker.evaluate(effective_date, _ohlcv_lookup, lookback_days=30)
    perf_summary = signal_tracker.summarize(effective_date, _ohlcv_lookup, window_days=90)

    # ── 7. 리포트 빌드 ────────────────────────────────────────────────────────
    from report_builder.builder import build as build_report
    out_path = build_report(
        scan_date=effective_date,
        signals=signals,
        macro=macro,
        kr_summary=kr_summary,
        global_summary=global_summary,
        kr_news_items=kr_news_items,
        global_news_items=global_news_items,
        name_map=name_map,
        comments=comments,
        charts=charts,
        dart_data=dart_data,
        perf_rows=perf_rows,
        perf_summary=perf_summary,
        funnel=funnel,
        watchlist=watchlist,
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
            news_summary=kr_summary,
            top_tickers=[s.ticker for s in signals],
            funnel=funnel,
            perf_summary=perf_summary,
        )

    logger.info("=== 완료 ===")


if __name__ == "__main__":
    main()
