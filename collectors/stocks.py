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


@dataclass
class ScanFunnel:
    """스캔 각 단계에서 살아남은 종목 수 (0개일 때 '왜'를 설명하기 위한 계측).

    유동성·시총은 STEP2/RS와 순서를 바꾸기 어려운 전제 필터라 앞에 두고,
    시장 국면 판단에 가장 유용한 마지막 두 단계(breakout, rs)를 뒤에 둔다.
    """
    universe: int      # 유니버스 전체 종목 수
    data_ok: int       # 지표 계산에 충분한 데이터 확보
    traded: int        # effective_date에 실제 거래된 종목
    liquidity: int     # 20일 평균 거래대금 통과
    market_cap: int    # 시가총액 통과
    breakout: int      # STEP2 돌파 통과
    rs: int            # RS 게이트 통과 (= 최종 신호 수)


@dataclass
class WatchItem:
    """돌파 준비(신호 전 단계) 종목 — 매매 신호가 아니라 시장 맥락 참고용.

    reason:
      "고가 근접"  — 52주 고가의 WATCH_PROXIMITY_LOW~BREAKOUT_HIGH_PCT 구간(돌파 임박)
      "거래량 미달" — 가격은 돌파(고가 근접 + 저항 돌파)했으나 거래량이 기준 미달
    """
    ticker: str
    close: float
    high_52w: float
    proximity_pct: float     # close / 52주고가 * 100
    vol_ratio: float         # 당일 거래량 / 20일 평균 (NaN 가능)
    avg_trading_value20: float
    return_60d: float
    reason: str


def _classify_watch(ticker: str, row: pd.Series, avg_tv: float, ret: float) -> "WatchItem | None":
    """돌파에 실패한 종목이 워치리스트(고가 근접/거래량 미달)에 해당하는지 판정.

    breakout_scanner.passes()가 False인 행에 대해서만 호출된다.
    """
    high = row["high_52w"]
    close = row["close"]
    if pd.isna(high) or high <= 0 or pd.isna(close):
        return None

    resist = row["resistance_60"]
    volavg = row["vol_avg20"]
    vol = row["volume"]
    prox = close / high

    # 가격 돌파(고가 95%+저항 돌파)했는데 여기 왔다 = breakout_scanner=False = 거래량 미달
    price_breakout = (not pd.isna(resist)) and close >= high * config.BREAKOUT_HIGH_PCT and close > resist
    if price_breakout:
        reason = "거래량 미달"
    elif config.WATCH_PROXIMITY_LOW <= prox < config.BREAKOUT_HIGH_PCT:
        reason = "고가 근접"
    else:
        return None

    vol_ratio = float(vol / volavg) if (not pd.isna(volavg) and volavg > 0) else float("nan")
    return WatchItem(
        ticker=ticker,
        close=float(close),
        high_52w=float(high),
        proximity_pct=float(prox * 100),
        vol_ratio=vol_ratio,
        avg_trading_value20=float(avg_tv),
        return_60d=float(ret) if not pd.isna(ret) else 0.0,
        reason=reason,
    )


def scan(target_date: str) -> tuple[list[StockSignal], pd.Timestamp, ScanFunnel, list[WatchItem]]:
    """STEP2+RS 스캔 실행.

    Args:
        target_date: YYYYMMDD 문자열 (스캔 대상 날짜)

    Returns:
        (signals, effective_date, funnel, watchlist) —
        signals는 STEP2+RS 최종 신호, effective_date는 실제 데이터가 있는 마지막
        거래일, funnel은 단계별 생존 종목 수, watchlist는 돌파 준비(고가 근접/
        거래량 미달) 참고용 종목(고가 근접도 내림차순)
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
        return [], target_ts, ScanFunnel(len(tickers), 0, 0, 0, 0, 0, 0), []

    # 실제 스캔 날짜: 타겟 이하 데이터가 가장 많은 거래일
    effective_date: pd.Timestamp = max(df.index[-1] for df in ticker_data.values())

    # RS percentile: market_data.py와 동일한 방법으로 cross-sectional 계산
    closes = pd.DataFrame({t: df["close"] for t, df in ticker_data.items()})
    returns_60d = closes.pct_change(config.RS_LOOKBACK_DAYS)
    rs_pct_df = returns_60d.rank(axis=1, pct=True)

    if effective_date not in rs_pct_df.index:
        available = rs_pct_df.index[rs_pct_df.index <= effective_date]
        if available.empty:
            return [], effective_date, ScanFunnel(len(tickers), len(ticker_data), 0, 0, 0, 0, 0), []
        effective_date = available[-1]

    rs_pct_row = rs_pct_df.loc[effective_date]
    ret_row = returns_60d.loc[effective_date]

    # 단계별 생존 카운터
    n_traded = n_liquidity = n_market_cap = n_breakout = 0

    signals: list[StockSignal] = []
    watchlist: list[WatchItem] = []  # 돌파 준비(고가 근접/거래량 미달) — 신호 아님, 참고용
    for ticker, df in ticker_data.items():
        if df.index[-1] != effective_date:
            continue
        n_traded += 1
        row = df.iloc[-1]

        # 유동성 필터
        avg_tv = row.get("avg_trading_value20", float("nan"))
        if pd.isna(avg_tv) or avg_tv < config.MIN_AVG_TRADING_VALUE:
            continue
        n_liquidity += 1

        # 시가총액 필터
        mkt_cap = estimate_market_cap(ticker, float(row["close"]))
        if mkt_cap is not None and mkt_cap < config.MIN_MARKET_CAP:
            continue
        n_market_cap += 1

        ret = ret_row.get(ticker, float("nan"))

        # STEP2 돌파 스캔
        if breakout_scanner.passes(row):
            n_breakout += 1
            pct = rs_pct_row.get(ticker, float("nan"))
            # RS 게이트 통과분만 신호. (돌파+RS미달은 실전상 거의 발생하지 않음)
            if not pd.isna(pct) and pct >= config.RS_PERCENTILE_THRESHOLD:
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
            continue

        # 돌파는 아니지만 '준비 중'인 종목 → 워치리스트 (고가 근접 / 거래량 미달)
        item = _classify_watch(ticker, row, float(avg_tv), ret)
        if item is not None:
            watchlist.append(item)

    signals.sort(key=lambda s: s.rs_percentile, reverse=True)
    # 고가에 가까운 순(돌파 임박에 가까운 순)으로 정렬
    watchlist.sort(key=lambda w: w.proximity_pct, reverse=True)

    funnel = ScanFunnel(
        universe=len(tickers),
        data_ok=len(ticker_data),
        traded=n_traded,
        liquidity=n_liquidity,
        market_cap=n_market_cap,
        breakout=n_breakout,
        rs=len(signals),
    )
    logger.info(
        "스캔 깔때기: 유니버스 %d → 데이터 %d → 거래 %d → 유동성 %d → 시총 %d → 돌파 %d → RS %d",
        funnel.universe, funnel.data_ok, funnel.traded, funnel.liquidity,
        funnel.market_cap, funnel.breakout, funnel.rs,
    )
    logger.info("STEP2+RS 신호: %d개 · 돌파 준비 워치리스트: %d개 (스캔일: %s)",
                len(signals), len(watchlist), effective_date.strftime("%Y-%m-%d"))
    return signals, effective_date, funnel, watchlist
