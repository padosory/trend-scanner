"""일별 STEP2+RS 스캔.

backtest/에서 썼던 fetch_ohlcv / add_indicators / breakout_scanner를 그대로
재사용한다. RS percentile 계산도 market_data.load_market_data()와 동일한
cross-sectional 방법으로 단일 날짜를 위해 구한다.
"""

import logging
from dataclasses import dataclass

import pandas as pd

import config
from backtest.data_cache import estimate_market_cap, fetch_ohlcv, get_universe
from scanners import breakout_scanner
from scanners.indicators import add_indicators

logger = logging.getLogger(__name__)

_OHLCV_HISTORY_DAYS = 420  # 지표 계산에 필요한 기간 (52주 252일 + 여유)


@dataclass
class StockSignal:
    ticker: str
    close: float
    volume: int
    high_52w: float
    resistance_60: float
    vol_avg20: float
    avg_trading_value20: float
    rs_percentile: float
    return_60d: float


def scan(target_date: str) -> tuple[list[StockSignal], pd.Timestamp]:
    """STEP2+RS 스캔 실행.

    Args:
        target_date: YYYYMMDD 문자열 (스캔 대상 날짜)

    Returns:
        (signals, effective_date) — effective_date는 실제 데이터가 있는 마지막 거래일
    """
    target_ts = pd.Timestamp(target_date)
    start = (target_ts - pd.DateOffset(days=_OHLCV_HISTORY_DAYS)).strftime("%Y%m%d")

    tickers = get_universe()
    logger.info("유니버스 %d개 종목 로딩 시작 (%s ~ %s)", len(tickers), start, target_date)

    ticker_data: dict[str, pd.DataFrame] = {}
    for i, ticker in enumerate(tickers):
        if i % 300 == 0 and i > 0:
            logger.info("  진행 %d/%d...", i, len(tickers))
        try:
            df = fetch_ohlcv(ticker, start, target_date)
            if len(df) < 210:  # MA200 계산 최소 길이
                continue
            df = df.loc[:target_ts]
            if df.empty:
                continue
            ticker_data[ticker] = add_indicators(df)
        except Exception as exc:  # noqa: BLE001
            logger.debug("skip %s: %s", ticker, exc)

    if not ticker_data:
        logger.warning("유효 데이터 없음 — 휴장일이거나 데이터 문제")
        return [], target_ts

    # 실제 스캔 날짜: 타겟 이하 데이터가 가장 많은 거래일
    effective_date: pd.Timestamp = max(df.index[-1] for df in ticker_data.values())

    # RS percentile: market_data.py와 동일한 방법으로 cross-sectional 계산
    closes = pd.DataFrame({t: df["close"] for t, df in ticker_data.items()})
    returns_60d = closes.pct_change(config.RS_LOOKBACK_DAYS)
    rs_pct_df = returns_60d.rank(axis=1, pct=True)

    if effective_date not in rs_pct_df.index:
        available = rs_pct_df.index[rs_pct_df.index <= effective_date]
        if available.empty:
            return [], effective_date
        effective_date = available[-1]

    rs_pct_row = rs_pct_df.loc[effective_date]
    ret_row = returns_60d.loc[effective_date]

    signals: list[StockSignal] = []
    for ticker, df in ticker_data.items():
        if df.index[-1] != effective_date:
            continue
        row = df.iloc[-1]

        # 유동성 필터
        avg_tv = row.get("avg_trading_value20", float("nan"))
        if pd.isna(avg_tv) or avg_tv < config.MIN_AVG_TRADING_VALUE:
            continue

        # 시가총액 필터
        mkt_cap = estimate_market_cap(ticker, float(row["close"]))
        if mkt_cap is not None and mkt_cap < config.MIN_MARKET_CAP:
            continue

        # STEP2 돌파 스캔
        if not breakout_scanner.passes(row):
            continue

        # RS 퍼센타일 게이트
        pct = rs_pct_row.get(ticker, float("nan"))
        if pd.isna(pct) or pct < config.RS_PERCENTILE_THRESHOLD:
            continue

        ret = ret_row.get(ticker, float("nan"))
        signals.append(
            StockSignal(
                ticker=ticker,
                close=float(row["close"]),
                volume=int(row["volume"]),
                high_52w=float(row["high_52w"]),
                resistance_60=float(row["resistance_60"]),
                vol_avg20=float(row["vol_avg20"]),
                avg_trading_value20=float(avg_tv),
                rs_percentile=float(pct),
                return_60d=float(ret) if not pd.isna(ret) else 0.0,
            )
        )

    signals.sort(key=lambda s: s.rs_percentile, reverse=True)
    logger.info("STEP2+RS 신호: %d개 (스캔일: %s)", len(signals), effective_date.strftime("%Y-%m-%d"))
    return signals, effective_date
