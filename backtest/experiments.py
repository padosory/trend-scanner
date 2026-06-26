"""개별 신호 검증 실험 — 각 함수는 (entry_fn, exit_fn) 튜플을 반환한다.

설계서.md §6.x 참고. 기존 스캐너(scanners/*.passes())를 그대로 재사용하고,
청산은 신호마다 "자연스러운" 방식을 쓴다 (공통 규칙 강제 안 함).
"""

import pandas as pd

import config
from backtest.market_data import MarketData
from scanners import breakout_scanner, pullback_scanner, trend_filter


def ma_cross(window: int):
    """maN 상향 돌파 진입 / 하향 돌파 청산. 자기완결형이라 stop 없음."""
    col = f"ma{window}"

    def entry(ticker, df, idx, market: MarketData) -> bool:
        if idx == 0:
            return False
        today, yesterday = df.iloc[idx], df.iloc[idx - 1]
        if pd.isna(today[col]) or pd.isna(yesterday[col]):
            return False
        return bool(yesterday["close"] <= yesterday[col] and today["close"] > today[col])

    def exit(ticker, df, idx, market: MarketData, entry_date, entry_price):
        if idx == 0:
            return None
        today, yesterday = df.iloc[idx], df.iloc[idx - 1]
        if pd.isna(today[col]) or pd.isna(yesterday[col]):
            return None
        if yesterday["close"] >= yesterday[col] and today["close"] < today[col]:
            return today["close"], "ma_cross_exit"
        return None

    return entry, exit


def step1_trend_filter():
    """STEP1(정배열+상승기울기) 단독. 필터가 꺼지면 청산."""

    def entry(ticker, df, idx, market: MarketData) -> bool:
        return trend_filter.passes(df.iloc[idx])

    def exit(ticker, df, idx, market: MarketData, entry_date, entry_price):
        row = df.iloc[idx]
        if not trend_filter.passes(row):
            return row["close"], "trend_filter_false"
        return None

    return entry, exit


def _managed_exit(ticker, df, idx, market: MarketData, entry_date, entry_price):
    """설계서.md §4의 표준 청산 규칙: stop_loss -8% 또는 전일종가가 전일ma20 아래 마감."""
    row = df.iloc[idx]
    stop_price = entry_price * (1 - config.STOP_LOSS_PCT)
    if row["low"] <= stop_price:
        return stop_price, "stop_loss"
    if idx == 0:
        return None
    prev = df.iloc[idx - 1]
    if not pd.isna(prev["ma20"]) and prev["close"] < prev["ma20"]:
        return row["open"], "ma20_break"
    return None


def _resistance_exit(ticker, df, idx, market: MarketData, entry_date, entry_price):
    """돌파 실패(저항선 재이탈) 청산 — 6차에서 가장 성과가 좋았던 청산 방식."""
    row = df.iloc[idx]
    if pd.isna(row["resistance_60"]):
        return None
    if row["close"] < row["resistance_60"]:
        return row["close"], "breakout_failed"
    return None


def _rs_passes(ticker, df, idx, market: MarketData) -> bool:
    date = df.index[idx]
    if date not in market.rs_percentile.index or ticker not in market.rs_percentile.columns:
        return False
    pct = market.rs_percentile.at[date, ticker]
    return bool(not pd.isna(pct) and pct >= config.RS_PERCENTILE_THRESHOLD)


def step2_breakout():
    """STEP2(52주고가 근접+거래량+저항돌파) 단독. 돌파 실패(저항선 재이탈) 시 청산."""

    def entry(ticker, df, idx, market: MarketData) -> bool:
        return breakout_scanner.passes(df.iloc[idx])

    return entry, _resistance_exit


def step2_managed():
    """STEP2(돌파) 단독 + 표준 위험관리 청산(stop_loss -8% 또는 ma20 이탈).
    추세필터(STEP1)/RS 게이트 없이, 돌파 즉시 진입하는 가장 단순한 실전형 시스템."""

    def entry(ticker, df, idx, market: MarketData) -> bool:
        return breakout_scanner.passes(df.iloc[idx])

    return entry, _managed_exit


def step2_rs():
    """6차(STEP2 단독+저항선재이탈)에 RS percentile 게이트만 추가. STEP1/STEP3는
    여전히 안 씀 — RS를 "단독 시스템"이 아니라 STEP2 위에 얹는 게이트로 썼을 때
    효과가 다른지 보기 위한 실험."""

    def entry(ticker, df, idx, market: MarketData) -> bool:
        return breakout_scanner.passes(df.iloc[idx]) and _rs_passes(ticker, df, idx, market)

    return entry, _resistance_exit


def step2_rs_trend():
    """step2_rs에 STEP1(추세필터) 게이트도 추가. STEP1 단독은 -0.67%로 나빴지만,
    RS와 같은 논리로 "이미 좋은 진입 타이밍(돌파) 위의 추가 게이트"로 쓰면 다를 수
    있는지 확인. STEP3(눌림목 대기)는 여전히 안 씀."""

    def entry(ticker, df, idx, market: MarketData) -> bool:
        row = df.iloc[idx]
        return (
            breakout_scanner.passes(row)
            and trend_filter.passes(row)
            and _rs_passes(ticker, df, idx, market)
        )

    return entry, _resistance_exit


def step2_rs_step3():
    """STEP2+RS 게이트를 통과하면(워치리스트 등록) 곧바로 진입하는 대신, STEP3
    (눌림목) 신호가 뜰 때까지 기다렸다가 진입한다 — 기존 BacktestEngine과 같은
    워치리스트 방식이지만 STEP1은 빼고 청산은 6차에서 가장 좋았던 저항선 재이탈을
    그대로 쓴다. entry 클로저 안에 워치리스트 상태(dict)를 직접 들고 있는다 —
    SignalBacktestEngine 자체는 종목당 단일 포지션 가정만 하고 대기 상태는 모르므로,
    이 실험에 한정해 entry_fn 쪽에서 직접 상태를 관리한다."""
    watchlist: dict[str, dict] = {}

    def entry(ticker, df, idx, market: MarketData) -> bool:
        date = df.index[idx]
        row = df.iloc[idx]

        pending = watchlist.get(ticker)
        if pending is not None:
            if date > pending["expires"]:
                del watchlist[ticker]
                return False
            window = config.PULLBACK_PRICE_BREAKOUT_WINDOW
            if idx < window:
                return False
            recent_low = df["low"].iloc[pending["added_idx"] : idx + 1].min()
            prior_highs = df["high"].iloc[idx - window : idx].tolist()
            if pullback_scanner.passes(row, prior_highs, recent_low, pending["breakout_low"]):
                del watchlist[ticker]
                return True
            return False

        if not (breakout_scanner.passes(row) and _rs_passes(ticker, df, idx, market)):
            return False

        base_window = df.iloc[max(0, idx - config.RESISTANCE_WINDOW) : idx]
        breakout_low = base_window["low"].min() if not base_window.empty else row["low"]
        watchlist[ticker] = {
            "added_idx": idx,
            "expires": date + pd.tseries.offsets.BDay(config.WATCHLIST_TTL_DAYS),
            "breakout_low": breakout_low,
        }
        return False

    return entry, _resistance_exit


def _ensure_pullback_columns(df: pd.DataFrame) -> None:
    """워치리스트 컨텍스트 없이 STEP3를 전체 유니버스에 단독 적용하기 위해,
    등록일 이후 최저가/등록 전 베이스 저점을 롤링 윈도우로 재현한다."""
    if "pullback_recent_low" in df.columns:
        return
    df["pullback_recent_low"] = df["low"].rolling(config.WATCHLIST_TTL_DAYS).min()
    df["pullback_breakout_low"] = (
        df["low"].shift(config.WATCHLIST_TTL_DAYS).rolling(config.RESISTANCE_WINDOW).min()
    )
    df["pullback_prior_high"] = df["high"].shift(1).rolling(config.PULLBACK_PRICE_BREAKOUT_WINDOW).max()


def step3_pullback():
    """STEP3(눌림목) 단독. 청산은 기존 엔진과 동일(stop_loss -8% 또는 ma20 이탈) —
    이 신호의 "자연스러운" 청산이 이미 그 형태였음(워치리스트 흐름에서도 동일 규칙 사용)."""

    def entry(ticker, df, idx, market: MarketData) -> bool:
        _ensure_pullback_columns(df)
        row = df.iloc[idx]
        if pd.isna(row["pullback_recent_low"]) or pd.isna(row["pullback_breakout_low"]) or pd.isna(row["pullback_prior_high"]):
            return False
        return pullback_scanner.passes(
            row, [row["pullback_prior_high"]], row["pullback_recent_low"], row["pullback_breakout_low"]
        )

    return entry, _managed_exit


def rs_alone():
    """그 날 전종목 대비 직전 60일 수익률 상위 20%(RS percentile) 단독. 상위권에서
    벗어나면 청산."""

    def entry(ticker, df, idx, market: MarketData) -> bool:
        return _rs_passes(ticker, df, idx, market)

    def exit(ticker, df, idx, market: MarketData, entry_date, entry_price):
        date = df.index[idx]
        if date not in market.rs_percentile.index or ticker not in market.rs_percentile.columns:
            return None
        pct = market.rs_percentile.at[date, ticker]
        if pd.isna(pct) or pct < config.RS_PERCENTILE_THRESHOLD:
            return df.iloc[idx]["close"], "rs_drop"
        return None

    return entry, exit


STRATEGIES = {
    "ma10": lambda: ma_cross(10),
    "ma20": lambda: ma_cross(20),
    "ma50": lambda: ma_cross(50),
    "step1": step1_trend_filter,
    "step2": step2_breakout,
    "step2_managed": step2_managed,
    "step2_rs": step2_rs,
    "step2_rs_trend": step2_rs_trend,
    "step2_rs_step3": step2_rs_step3,
    "step3": step3_pullback,
    "rs": rs_alone,
}
