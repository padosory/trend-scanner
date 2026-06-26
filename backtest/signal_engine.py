"""개별 신호(STEP1/STEP2/STEP3/RS/MA크로스)를 단독으로 검증하는 범용 백테스트 엔진.

기존 BacktestEngine(워치리스트 기반 STEP1~3 파이프라인)과 달리, 신호 하나의
진입/청산만 보는 단순 시뮬레이션이다. 종목당 포지션 1개, 진입 시점에 유동성/
시가총액 필터(기존 엔진과 동일 기준)만 공통으로 적용하고, 그 외 진입/청산 조건은
실험마다 다르게 주입한다 (backtest/experiments.py 참고).
"""

import logging
from typing import Callable

import pandas as pd

import config
from backtest.data_cache import estimate_market_cap
from backtest.engine import Trade
from backtest.market_data import MarketData, load_market_data

logger = logging.getLogger(__name__)

EntryFn = Callable[[str, pd.DataFrame, int, MarketData], bool]
ExitFn = Callable[[str, pd.DataFrame, int, MarketData, pd.Timestamp, float], tuple[float, str] | None]


class SignalBacktestEngine:
    def __init__(self, tickers: list[str], start: str, end: str, entry_fn: EntryFn, exit_fn: ExitFn):
        self.market = load_market_data(tickers, start, end)
        self.entry_fn = entry_fn
        self.exit_fn = exit_fn
        self.positions: dict[str, tuple[pd.Timestamp, float]] = {}
        self.trades: list[Trade] = []

    def run(self) -> list[Trade]:
        for date in self.market.dates:
            self._process_exits(date)
            self._process_entries(date)
        return self.trades

    def _process_exits(self, date: pd.Timestamp) -> None:
        for ticker in list(self.positions):
            df = self.market.data[ticker]
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            entry_date, entry_price = self.positions[ticker]

            result = self.exit_fn(ticker, df, idx, self.market, entry_date, entry_price)
            if result is None:
                continue
            price, reason = result
            self.trades.append(Trade(ticker, entry_date, entry_price, date, price, reason))
            del self.positions[ticker]

    def _process_entries(self, date: pd.Timestamp) -> None:
        for ticker, df in self.market.data.items():
            if ticker in self.positions or date not in df.index:
                continue

            row = df.loc[date]
            if pd.isna(row["avg_trading_value20"]) or row["avg_trading_value20"] < config.MIN_AVG_TRADING_VALUE:
                continue
            market_cap = estimate_market_cap(ticker, row["close"])
            if market_cap is not None and market_cap < config.MIN_MARKET_CAP:
                continue

            idx = df.index.get_loc(date)
            if self.entry_fn(ticker, df, idx, self.market):
                self.positions[ticker] = (date, row["close"])
