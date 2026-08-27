"""일별 스캔 → HTML 리포트 → Telegram 알림 메인 스크립트.

사용법:
  python run_daily_report.py                     # 어제 날짜 기준 스캔
  python run_daily_report.py --date 20260625     # 특정 날짜 스캔
  python run_daily_report.py --skip-ai           # Gemini 없이 실행
  python run_daily_report.py --skip-telegram     # Telegram 알림 건너뜀
  python run_daily_report.py --skip-if-current   # 이미 최신 거래일이 반영돼 있으면 아무것도 안 함

종료 코드는 0(정상) 또는 1(스캔 기준일이 마지막 거래일보다 뒤처짐)이다. 1로 끝나면
워크플로가 실패로 표시되고 Pages 배포가 건너뛰어져, 낡은 리포트가 최신인 척
재발행되지 않는다.
"""

import argparse
import logging
import os
import sys

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="daily trend scan + report + Telegram")
    parser.add_argument("--date", help="YYYYMMDD. 기본: 어제")
    parser.add_argument("--skip-ai", action="store_true", help="Gemini AI 분석 건너뜀")
    parser.add_argument("--skip-telegram", action="store_true", help="Telegram 알림 건너뜀")
    parser.add_argument("--skip-if-current", action="store_true",
                        help="마지막 거래일이 이미 반영돼 있으면 즉시 종료 (백업 스케줄용)")
    args = parser.parse_args()

    # '어제'는 KST 기준. CI 러너는 UTC라 naive now()를 쓰면 21:30 UTC(=KST 익일 06:30)
    # 실행에서 target이 하루 더 밀려(예: 월 21:30 UTC → 일요일) 직전 거래일 데이터를
    # 통째로 잘라내고 그 전 거래일 리포트를 내보낸다.
    import config
    target_date = args.date or (
        pd.Timestamp.now(tz=config.MARKET_TZ) - pd.Timedelta(days=1)
    ).strftime("%Y%m%d")

    # 마지막 '완결된' 거래일. 스캔 결과가 여기 못 미치면 낡은 리포트다(§아래 검증).
    from backtest.data_cache import get_last_trading_day
    last_trading_day = get_last_trading_day()

    if args.skip_if_current:
        # 백업 스케줄용. 앞선 실행이 이미 마지막 거래일을 처리했으면 전체 스캔을
        # 건너뛴다 — 중복 텔레그램·중복 AI 호출을 막는다.
        import macro_state
        done = macro_state.load().get("date")
        if last_trading_day is not None and done == last_trading_day.strftime("%Y-%m-%d"):
            logger.info("이미 최신(%s) 반영됨 — 스킵", done)
            return 0
        logger.info("최신 거래일(%s) 미반영(마지막 처리 %s) — 스캔 진행",
                    last_trading_day.date() if last_trading_day is not None else "판정불가", done)

    logger.info("=== 일별 스캔 시작 (타겟: %s, 마지막 거래일: %s) ===",
                target_date,
                last_trading_day.date() if last_trading_day is not None else "판정불가")

    # ── 1. 주식 STEP2+RS 스캔 ──────────────────────────────────────────────
    from collectors.stocks import scan as stock_scan
    signals, effective_date, funnel, watchlist = stock_scan(target_date)

    # ── 1-1. 신선도 검증 ────────────────────────────────────────────────────
    # 스캔이 마지막 거래일에 못 미치면 '어제 리포트를 오늘 것처럼' 다시 내보내는
    # 상태다. 2026-08-06·08-07·08-26이 이렇게 조용히 유실됐다(실행은 전부 성공).
    # 여기서 경고를 리포트·알림에 싣고, 마지막에 exit 1로 실패시킨다.
    stale_warning = ""
    if args.date is None and last_trading_day is not None and effective_date < last_trading_day:
        missed = (last_trading_day - effective_date).days
        stale_warning = (
            f"데이터가 낡았습니다 — 스캔 기준일 {effective_date.strftime('%Y-%m-%d')}, "
            f"마지막 거래일 {last_trading_day.strftime('%Y-%m-%d')} "
            f"({missed}일 뒤처짐). 이 리포트는 최신 거래일을 반영하지 않습니다."
        )
        logger.error("신선도 검증 실패 — %s", stale_warning)

    # ── 2. 종목명 맵 (FDR 실패 시 캐시 폴백 — KRX 간헐 차단에도 리포트는 생성) ──
    from backtest.data_cache import get_name_map
    name_map: dict[str, str] = get_name_map()

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
    # 뉴스 API는 과거 조회를 지원하지 않아 항상 '지금 최신'이 온다 → 스캔 기준일이
    # 아니라 수집 시각에 묶인다. 리포트에 시각을 밝혀 오해를 막는다.
    from collectors.news import fetch_global, fetch_korea, now_kst_label
    kr_news_items = fetch_korea()
    global_news_items = fetch_global()
    news_collected_at = now_kst_label()
    logger.info("뉴스 수집 시각(KST): %s — 스캔 기준일 %s",
                news_collected_at, effective_date.strftime("%Y-%m-%d"))

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

    # ── 6. 신호 히스토리 기록 + 성과 추적 ──────────────────────────────────
    import signal_tracker
    from backtest.data_cache import fetch_ohlcv
    from scanners.indicators import add_indicators

    signal_tracker.record(signals, effective_date, name_map)

    chart_end = effective_date.strftime("%Y%m%d")
    # 저항선 재이탈(성과추적 청산) 판정 + 차트 + 워치 이탈사유 판정에 쓸 지표 적용
    # OHLCV. 410일이면 52주 고가(rolling 252거래일)까지 유효해 워치 이탈 사유를
    # 가려낼 수 있고, 스캔이 이미 420일로 채운 parquet 캐시 안에 들어와 추가
    # 다운로드가 발생하지 않는다(캐시 시작 슬랙 7일).
    track_start = (effective_date - pd.DateOffset(days=410)).strftime("%Y%m%d")
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

    # ── 6-2. 워치리스트 전이 추적 (연속 등재일수 · 신규/유지/승격/이탈) ──────
    # 워치리스트는 무상태로 재계산되므로 여기서 스캔 간 상태를 이어붙인다.
    import watch_tracker
    watch_delta = watch_tracker.update(
        watchlist, signals, effective_date, name_map, _ohlcv_lookup
    )

    # ── 6-1. 차트 JSON 생성 (신호·워치리스트·성과추적 종목, 클릭 시 lazy 렌더) ──────
    from report_builder.charts import make_chart_json

    # 52주고가 수평선 값 (있는 종목만; 성과추적 종목은 없으면 생략)
    high52_map: dict[str, float] = {s.ticker: s.high_52w for s in signals}
    high52_map.update({w.ticker: w.high_52w for w in watchlist[:20]})

    chart_tickers = (
        [s.ticker for s in signals]
        + [w.ticker for w in watchlist[:20]]
        + [r.ticker for r in perf_rows]
    )
    charts: dict[str, str] = {}
    for tk in dict.fromkeys(chart_tickers):  # 순서 유지 + 중복 제거
        df = _ohlcv_lookup(tk)
        if df is None or df.empty:
            continue
        try:
            cj = make_chart_json(tk, df, effective_date, high_52w=high52_map.get(tk))
            if cj:
                charts[tk] = cj
        except Exception as exc:  # noqa: BLE001
            logger.debug("차트 생성 실패 %s: %s", tk, exc)

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
        news_collected_at=news_collected_at,
        name_map=name_map,
        comments=comments,
        charts=charts,
        dart_data=dart_data,
        perf_rows=perf_rows,
        perf_summary=perf_summary,
        funnel=funnel,
        watchlist=watchlist,
        watch_delta=watch_delta,
        stale_warning=stale_warning,
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
            stale_warning=stale_warning,
        )

    if stale_warning:
        logger.error("=== 실패 종료 (신선도) — %s ===", stale_warning)
        return 1

    logger.info("=== 완료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
