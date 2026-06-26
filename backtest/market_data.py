"""백테스트 엔진들이 공통으로 쓰는 시세 데이터 로딩.

OHLCV 로딩 + 지표계산 + RS(상대강도) percentile 계산을 한 곳에 모아서
BacktestEngine과 SignalBacktestEngine이 동일한 캐시/로직을 재사용한다.
"""

import logging
from dataclasses import dataclass

import pandas as pd

import config
from backtest.data_cache import fetch_index_ohlcv, fetch_ohlcv
from scanners.indicators import add_indicators

logger = logging.getLogger(__name__)


@dataclass
class MarketData:
    data: dict[str, pd.DataFrame]
    rs_percentile: pd.DataFrame
    index: pd.DataFrame
    dates: list[pd.Timestamp]


def load_market_data(tickers: list[str], start: str, end: str) -> MarketData:
    data: dict[str, pd.DataFrame] = {}
    logger.info("OHLCV 로딩 중: %d개 종목", len(tickers))
    for ticker in tickers:
        df = fetch_ohlcv(ticker, start, end)
        if len(df) < 210:  # MA200 계산에 필요한 최소 길이
            continue
        data[ticker] = add_indicators(df)

    all_dates: set[pd.Timestamp] = set()
    for df in data.values():
        all_dates.update(df.index)
    dates = sorted(all_dates)

    index_df = fetch_index_ohlcv(start, end)
    index_df["return_60d"] = index_df["close"] / index_df["close"].shift(config.RS_LOOKBACK_DAYS) - 1

    # RS(상대강도) percentile: 날짜별로 전종목 직전 60일 수익률을 cross-sectional
    # 랭킹해서 "그 날 시장 대비 상위 몇 %인지"를 구한다.
    closes = pd.DataFrame({t: df["close"] for t, df in data.items()})
    returns_60d = closes.pct_change(config.RS_LOOKBACK_DAYS)
    rs_percentile = returns_60d.rank(axis=1, pct=True)

    return MarketData(data=data, rs_percentile=rs_percentile, index=index_df, dates=dates)
