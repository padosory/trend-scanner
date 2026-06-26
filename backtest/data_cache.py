"""시세/종목 데이터 조회 + 로컬 parquet 캐싱.

OHLCV는 pykrx, 종목 유니버스·시가총액 근사는 FinanceDataReader(FDR)를 쓴다.
이 샌드박스 환경에서 pykrx의 종목리스트(get_market_ticker_list)와 시가총액
(get_market_cap) 엔드포인트가 항상 빈 응답을 반환해서(KRX 쪽 접근 제한으로 추정,
OHLCV 엔드포인트는 정상) FDR로 대체했다. 사용자 PC에서는 pykrx가 정상 동작할 수도
있으니, 그쪽에서 또 막히면 이 파일만 보면 된다.

stock_trader 프로젝트에서 pykrx를 짧은 시간에 과도하게 호출해 차단된 적이 있었음.
이를 피하려고 종목별 OHLCV는 날짜 하나씩이 아니라 기간 전체를 한 번에 조회하고,
디스크에 캐시해서 재실행 시 재조회하지 않는다.
"""

import logging
import time
from pathlib import Path

import pandas as pd
from pykrx import stock

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

REQUEST_DELAY_SEC = 0.3
_LISTING_CACHE_PATH = CACHE_DIR / "_listing.parquet"
_CACHE_END_SLACK_DAYS = 5    # 요청 종료일이 휴장일(연말 등)이라 실제 거래 데이터가 살짝 못 미쳐도 캐시 인정
_CACHE_START_SLACK_DAYS = 7  # 요청 시작일이 공휴일/주말이면 실제 첫 거래일이 며칠 뒤일 수 있음 (근로자의날·설·추석 등)

_listing_df: pd.DataFrame | None = None
_shares_by_ticker: dict[str, float] | None = None

_COLUMN_MAP = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
}


def _load_listing() -> pd.DataFrame:
    """FDR 상장종목 목록(Code/Market/Stocks=상장주식수). 현재 시점 스냅샷.

    프로세스 내 메모리 캐시(_listing_df)도 같이 둔다 — 디스크 parquet 캐시가 있어도
    get_shares_outstanding()이 종목×거래일마다 호출되므로, 매번 디스크에서 다시
    읽으면(특히 시그널 백테스트처럼 호출 빈도가 높을 때) 그것만으로 병목이 된다.
    """
    global _listing_df
    if _listing_df is not None:
        return _listing_df

    if _LISTING_CACHE_PATH.exists():
        _listing_df = pd.read_parquet(_LISTING_CACHE_PATH)
        return _listing_df

    import FinanceDataReader as fdr

    logger.info("FDR 상장종목 목록 조회")
    df = fdr.StockListing("KRX")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])][["Code", "Market", "Stocks"]]
    df.to_parquet(_LISTING_CACHE_PATH)
    _listing_df = df
    return df


def get_universe() -> list[str]:
    """KOSPI+KOSDAQ 상장 종목코드 (현재 시점 기준).

    주의: 현재 상장된 종목만 반환하므로, 백테스트 기간 중 상장폐지된 종목은
    빠진다(생존편향). 1차 백테스트에서는 감안하고 진행.
    """
    return _load_listing()["Code"].tolist()


def get_shares_outstanding(ticker: str) -> float | None:
    global _shares_by_ticker
    if _shares_by_ticker is None:
        listing = _load_listing()
        _shares_by_ticker = dict(zip(listing["Code"], listing["Stocks"]))
    return _shares_by_ticker.get(ticker)


def estimate_market_cap(ticker: str, price: float) -> float | None:
    """현재 상장주식수 x 과거 종가로 시가총액을 근사한다.

    유상증자/감자/액면분할 등으로 당시 실제 상장주식수와 다를 수 있지만,
    시가총액 하한 필터(소형주 배제) 용도로는 근사치로 충분하다.
    """
    shares = get_shares_outstanding(ticker)
    if shares is None:
        return None
    return shares * price


def _covers_range(cached: pd.DataFrame, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> bool:
    """캐시가 [start_ts, end_ts] 조회를 그대로 대체할 수 있는지 확인.

    종료일은 휴장일(연말 등)로 며칠 어긋날 수 있어 슬랙을 둔다. 시작일은 캐시된
    첫 날짜가 요청 시작일 이전이어야 충분 — 더 늦다면 그 사이 구간을 못 받았을
    수 있으므로(예: 이전에 더 짧은 기간으로만 캐싱한 경우) 재조회해야 한다.
    종목이 start_ts 이후 상장한 경우 재조회해도 pykrx가 같은 결과를 반환할
    뿐이라 안전하다."""
    if cached.empty:
        return False
    return bool(
        end_ts - cached.index.max() <= pd.Timedelta(days=_CACHE_END_SLACK_DAYS)
        and cached.index.min() <= start_ts + pd.Timedelta(days=_CACHE_START_SLACK_DAYS)
    )


def fetch_index_ohlcv(start: str, end: str) -> pd.DataFrame:
    """KOSPI 지수(KS11) 종가. RS(상대강도) 계산용 벤치마크.

    pykrx의 get_index_ohlcv도 이 환경에서 빈 응답이라 FDR로 조회.
    """
    cache_path = CACHE_DIR / "_kospi.parquet"
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)

    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if _covers_range(cached, start_ts, end_ts):
            return cached.loc[start_ts:end_ts]

    import FinanceDataReader as fdr

    logger.info("FDR 조회: KOSPI 지수 (%s~%s)", start, end)
    df = fdr.DataReader("KS11", start_ts.strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d"))
    df = df[["Close"]].rename(columns={"Close": "close"})
    df.to_parquet(cache_path)
    return df.loc[start_ts:end_ts]


def fetch_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """start/end: YYYYMMDD. 캐시에 요청 구간이 다 들어있으면 캐시만 쓴다."""
    cache_path = CACHE_DIR / f"{ticker}.parquet"
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)

    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if _covers_range(cached, start_ts, end_ts):
            return cached.loc[start_ts:end_ts]

    logger.info("pykrx 조회: %s (%s~%s)", ticker, start, end)
    df = stock.get_market_ohlcv(start, end, ticker)
    time.sleep(REQUEST_DELAY_SEC)

    df = df.rename(columns=_COLUMN_MAP)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index)
    df.to_parquet(cache_path)
    return df.loc[start_ts:end_ts]
