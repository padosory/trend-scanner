"""추세추종 스캐너 백테스트 엔진.

일별 시뮬레이션 흐름:
  1. 보유종목 청산 체크 (손절가 / 전일종가 MA20 이탈)
  2. 워치리스트(STEP2 통과 후 대기 중) 종목의 STEP3 재검사 → 진입
  3. 신규종목 STEP1+STEP2 통과 체크 (+ RS 게이트) → 워치리스트 등록

워치리스트는 STEP2 통과일 기준 TTL(config.WATCHLIST_TTL_DAYS)이 지나면 만료되어
더 이상 STEP3 대상이 아니게 된다(돌파 실패로 간주).

(진입 직전 1일 확인 지연을 넣어본 적이 있는데, 손절 비중은 줄었지만 손절 1건당
평균손실이 거의 2배로 커져서 기대값이 더 나빠져 되돌렸다 — 설계서.md §6 참고)
"""

import logging
from dataclasses import dataclass, field

import pandas as pd

import config
from backtest.data_cache import estimate_market_cap
from backtest.market_data import load_market_data
from scanners import breakout_scanner, pullback_scanner, trend_filter

logger = logging.getLogger(__name__)


@dataclass
class WatchlistEntry:
    ticker: str
    added_date: pd.Timestamp
    expires_date: pd.Timestamp
    breakout_low: float  # STEP2 통과 이전 베이스 구간 저점 (higher-low 비교 기준)


@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    stop_price: float


@dataclass
class Trade:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str
    return_pct: float = field(init=False)

    def __post_init__(self) -> None:
        self.return_pct = (self.exit_price / self.entry_price - 1) * 100


class BacktestEngine:
    def __init__(self, tickers: list[str], start: str, end: str):
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end)

        market = load_market_data(tickers, start, end)
        self.data = market.data
        self.dates = market.dates
        self.index = market.index
        # RS(상대강도) percentile: 날짜별로 전종목 직전 60일 수익률을 cross-sectional
        # 랭킹해서 "그 날 시장 대비 상위 몇 %인지"를 구한다. KOSPI 대비 단순 outperform
        # 여부(이진)보다 엄격해서, 시장보다 덜 빠지기만 한 잡주를 걸러낸다.
        self.rs_percentile = market.rs_percentile

        self.watchlist: dict[str, WatchlistEntry] = {}
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.rs_log: list[dict] = []

    def run(self) -> list[Trade]:
        for date in self.dates:
            self._process_exits(date)
            self._process_watchlist(date)
            self._process_new_signals(date)
        return self.trades

    # ---- 1. 청산 ----

    def _process_exits(self, date: pd.Timestamp) -> None:
        for ticker in list(self.positions):
            df = self.data[ticker]
            if date not in df.index:
                continue
            row = df.loc[date]
            pos = self.positions[ticker]

            if row["low"] <= pos.stop_price:
                self._close(pos, date, pos.stop_price, "stop_loss")
                continue

            idx = df.index.get_loc(date)
            if idx == 0:
                continue
            prev_row = df.iloc[idx - 1]
            if prev_row["close"] < prev_row["ma20"]:
                self._close(pos, date, row["open"], "ma20_break")

    def _close(self, pos: Position, date: pd.Timestamp, price: float, reason: str) -> None:
        self.trades.append(
            Trade(pos.ticker, pos.entry_date, pos.entry_price, date, price, reason)
        )
        del self.positions[pos.ticker]

    # ---- 2. 워치리스트 → STEP3 → 진입 ----

    def _process_watchlist(self, date: pd.Timestamp) -> None:
        for ticker in list(self.watchlist):
            entry = self.watchlist[ticker]

            if date > entry.expires_date:
                del self.watchlist[ticker]
                continue
            if ticker in self.positions:
                del self.watchlist[ticker]
                continue

            df = self.data[ticker]
            if date not in df.index:
                continue
            row = df.loc[date]
            idx = df.index.get_loc(date)

            window = config.PULLBACK_PRICE_BREAKOUT_WINDOW
            if idx < window:
                continue
            prior_highs = df["high"].iloc[idx - window : idx].tolist()

            added_idx = df.index.get_loc(entry.added_date)
            recent_low = df["low"].iloc[added_idx : idx + 1].min()

            if pullback_scanner.passes(row, prior_highs, recent_low, entry.breakout_low):
                self._enter(ticker, date, row, recent_low)
                del self.watchlist[ticker]

    def _enter(self, ticker: str, date: pd.Timestamp, row: pd.Series, support_low: float) -> None:
        """support_low: 워치리스트 등록 이후 최저가 — 단일 캔들(진입일 저가)보다
        안정적인 지지선으로 보고 손절 기준에 씀(Gemini 제안)."""
        entry_price = row["close"]
        stop = max(support_low, entry_price * (1 - config.STOP_LOSS_PCT))
        self.positions[ticker] = Position(ticker, date, entry_price, stop)

    # ---- 3. 신규 STEP1+STEP2 (+ RS 게이트) → 워치리스트 등록 ----

    def _process_new_signals(self, date: pd.Timestamp) -> None:
        for ticker, df in self.data.items():
            if ticker in self.watchlist or ticker in self.positions:
                continue
            if date not in df.index:
                continue

            row = df.loc[date]
            if not trend_filter.passes(row):
                continue
            if not breakout_scanner.passes(row):
                continue
            if pd.isna(row["avg_trading_value20"]) or row["avg_trading_value20"] < config.MIN_AVG_TRADING_VALUE:
                continue

            market_cap = estimate_market_cap(ticker, row["close"])
            if market_cap is not None and market_cap < config.MIN_MARKET_CAP:
                continue

            idx = df.index.get_loc(date)
            rs_return = self._stock_rs_return(df, idx)
            index_return = self._index_rs_return(date)
            percentile = self.rs_percentile.at[date, ticker] if date in self.rs_percentile.index else None
            if rs_return is not None:
                self.rs_log.append({
                    "ticker": ticker,
                    "date": date,
                    "rs_return_60d": rs_return,
                    "kospi_return_60d": index_return,
                    "rs_percentile": percentile,
                })

            if percentile is None or pd.isna(percentile) or percentile < config.RS_PERCENTILE_THRESHOLD:
                continue

            base_window = df.iloc[max(0, idx - config.RESISTANCE_WINDOW) : idx]
            breakout_low = base_window["low"].min() if not base_window.empty else row["low"]

            expires = date + pd.tseries.offsets.BDay(config.WATCHLIST_TTL_DAYS)
            self.watchlist[ticker] = WatchlistEntry(ticker, date, expires, breakout_low)

    def _stock_rs_return(self, df: pd.DataFrame, idx: int) -> float | None:
        lookback = config.RS_LOOKBACK_DAYS
        if idx < lookback:
            return None
        return df["close"].iloc[idx] / df["close"].iloc[idx - lookback] - 1

    def _index_rs_return(self, date: pd.Timestamp) -> float | None:
        if date not in self.index.index:
            return None
        value = self.index.loc[date, "return_60d"]
        return None if pd.isna(value) else float(value)
