"""개별 신호(STEP1/STEP2/STEP3/RS/MA크로스)를 단독으로 검증하는 범용 백테스트 엔진.

기존 BacktestEngine(워치리스트 기반 STEP1~3 파이프라인)과 달리, 신호 하나의
진입/청산만 보는 단순 시뮬레이션이다. 종목당 포지션 1개, 진입 시점에 유동성/
시가총액 필터(기존 엔진과 동일 기준)만 공통으로 적용하고, 그 외 진입/청산 조건은
실험마다 다르게 주입한다 (backtest/experiments.py 참고).

체결 시점(fill_mode)
--------------------
- "close"     : 신호일(T) 종가 진입 / 청산신호일(T) 종가 청산 (현행)
- "next_open" : 신호 다음 거래일(T+1) 시가 진입 / 청산도 T+1 시가
- "next_high" : T+1 고가 진입 (추격매수 최악 케이스)
- "next_vwap" : T+1 (고+저+종)/3 VWAP 근사 진입

"close"가 아닌 모드는 신호일에 즉시 체결하지 않고 pending에 등록한 뒤, 다음
거래 가능일(데이터가 있는 첫 날) 시가/고가/VWAP로 실제 체결한다. exit_fn 인터페이스는
바꾸지 않고(여전히 T 종가 기준 "종료 신호 여부"만 반환), 실제 청산가는 엔진이
fill_mode에 따라 오버라이드한다.
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

FILL_MODES = ("close", "next_open", "next_high", "next_vwap")


class SignalBacktestEngine:
    def __init__(
        self,
        tickers: list[str],
        start: str,
        end: str,
        entry_fn: EntryFn,
        exit_fn: ExitFn,
        fill_mode: str = "close",
    ):
        if fill_mode not in FILL_MODES:
            raise ValueError(f"알 수 없는 fill_mode: {fill_mode!r} (지원: {FILL_MODES})")
        self.market = load_market_data(tickers, start, end)
        self.entry_fn = entry_fn
        self.exit_fn = exit_fn
        self.fill_mode = fill_mode
        self.positions: dict[str, tuple[pd.Timestamp, float]] = {}
        self.trades: list[Trade] = []
        # fill_mode != "close" 일 때만 사용하는 대기열.
        # _pending_entries[ticker] = signal_date(T)
        self._pending_entries: dict[str, pd.Timestamp] = {}
        # _pending_exits[ticker] = (signal_date(T), entry_date, entry_price, reason)
        self._pending_exits: dict[str, tuple[pd.Timestamp, pd.Timestamp, float, str]] = {}

    def run(self) -> list[Trade]:
        for date in self.market.dates:
            if self.fill_mode != "close":
                # 일반 처리 이전에 대기 중인 체결을 먼저 확정한다(날짜 순서 보장).
                # 같은 날 진입/청산 대기가 겹치면 청산을 먼저 처리한다.
                self._fill_pending_exits(date)
                self._fill_pending_entries(date)
            self._process_exits(date)
            self._process_entries(date)
        return self.trades

    def _fill_price(self, row: pd.Series) -> float:
        if self.fill_mode == "next_open":
            return row["open"]
        if self.fill_mode == "next_high":
            return row["high"]
        if self.fill_mode == "next_vwap":
            return (row["high"] + row["low"] + row["close"]) / 3
        # "close"는 이 경로로 오지 않는다.
        return row["close"]

    def _fill_pending_exits(self, date: pd.Timestamp) -> None:
        for ticker in list(self._pending_exits):
            df = self.market.data[ticker]
            if date not in df.index:
                continue  # 거래정지 등 — 데이터 있는 다음 날 처리
            signal_date, entry_date, entry_price, reason = self._pending_exits[ticker]
            if date <= signal_date:
                continue  # 신호 다음 거래일부터 체결
            row = df.loc[date]
            price = self._fill_price(row)
            if not price or pd.isna(price):
                continue  # 시가 0 = 거래정지 — 다음 거래일 재시도
            self.trades.append(Trade(ticker, entry_date, entry_price, date, price, reason))
            del self._pending_exits[ticker]
            self.positions.pop(ticker, None)

    def _fill_pending_entries(self, date: pd.Timestamp) -> None:
        for ticker in list(self._pending_entries):
            df = self.market.data[ticker]
            if date not in df.index:
                continue  # 거래정지 등 — 데이터 있는 다음 날 처리
            signal_date = self._pending_entries[ticker]
            if date <= signal_date:
                continue
            row = df.loc[date]
            price = self._fill_price(row)
            if not price or pd.isna(price):
                continue  # 시가 0 = 거래정지 — 다음 거래일 재시도
            self.positions[ticker] = (date, price)
            del self._pending_entries[ticker]

    def _process_exits(self, date: pd.Timestamp) -> None:
        for ticker in list(self.positions):
            if ticker in self._pending_exits:
                continue  # 이미 청산 대기 중 — 중복 등록 방지
            df = self.market.data[ticker]
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            entry_date, entry_price = self.positions[ticker]

            result = self.exit_fn(ticker, df, idx, self.market, entry_date, entry_price)
            if result is None:
                continue
            price, reason = result
            if self.fill_mode == "close":
                self.trades.append(Trade(ticker, entry_date, entry_price, date, price, reason))
                del self.positions[ticker]
            else:
                # exit_fn이 반환한 price는 버리고, 다음 거래일 시가/고가/VWAP로 청산.
                self._pending_exits[ticker] = (date, entry_date, entry_price, reason)

    def _process_entries(self, date: pd.Timestamp) -> None:
        for ticker, df in self.market.data.items():
            if ticker in self.positions or ticker in self._pending_entries or date not in df.index:
                continue

            row = df.loc[date]
            if pd.isna(row["avg_trading_value20"]) or row["avg_trading_value20"] < config.MIN_AVG_TRADING_VALUE:
                continue
            market_cap = estimate_market_cap(ticker, row["close"])
            if market_cap is not None and market_cap < config.MIN_MARKET_CAP:
                continue

            idx = df.index.get_loc(date)
            if self.entry_fn(ticker, df, idx, self.market):
                if self.fill_mode == "close":
                    self.positions[ticker] = (date, row["close"])
                else:
                    self._pending_entries[ticker] = date
