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
import threading
import time
from pathlib import Path

import pandas as pd
from pykrx import stock

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

REQUEST_DELAY_SEC = 0.3
FETCH_TIMEOUT_SEC = 20   # 종목당 pykrx 조회 최대 대기(초). 초과 시 스킵 — KRX 스로틀 시
                         # 한 종목이 실행을 수 분씩 잡아 전체가 몇 시간 가는 것을 방지.
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


_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def _pykrx_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """pykrx OHLCV를 FETCH_TIMEOUT_SEC 타임아웃을 걸어 조회하고 표준 컬럼으로 정규화.

    KRX 스로틀 시 get_market_ohlcv 한 건이 수 분씩 걸릴 수 있어, 데몬 스레드로
    감싸 시간 제한을 건다(크로스플랫폼). 초과하면 TimeoutError를 던지고(스레드는
    데몬이라 프로세스 종료를 막지 않음), 상위 호출자가 해당 종목을 스킵한다.
    """
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["df"] = stock.get_market_ohlcv(start, end, ticker)
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(FETCH_TIMEOUT_SEC)
    time.sleep(REQUEST_DELAY_SEC)  # 성공/실패와 무관하게 KRX 호출 페이싱

    if t.is_alive():
        raise TimeoutError(f"pykrx 조회 타임아웃({FETCH_TIMEOUT_SEC}s): {ticker}")
    if "err" in box:
        raise box["err"]  # type: ignore[misc]

    df: pd.DataFrame = box["df"]  # type: ignore[assignment]
    df = df.rename(columns=_COLUMN_MAP)[_OHLCV_COLUMNS]
    df.index = pd.to_datetime(df.index)
    return df


def fetch_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """start/end: YYYYMMDD. 캐시를 최대한 재사용하고, 끝만 부족하면 증분 조회한다.

    - 캐시가 요청 구간을 (종료일 5일 슬랙 안에서) 덮으면 그대로 반환.
    - 시작 구간은 덮지만 끝이 낡았으면 '마지막 캐시일 다음날~end'만 받아 병합
      (전체 재다운로드 대신 증분).
    - 캐시가 없거나 시작 구간이 부족하면 전체 조회.
    - 조회 실패/타임아웃 시 기존 캐시가 있으면 그걸 반환(스캔 폭 붕괴 방지),
      없으면 예외를 던져 상위에서 스킵.
    """
    cache_path = CACHE_DIR / f"{ticker}.parquet"
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)

    cached: pd.DataFrame | None = None
    if cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("캐시 로드 실패 %s: %s", ticker, exc)
            cached = None

    if cached is not None and not cached.empty:
        covers_start = cached.index.min() <= start_ts + pd.Timedelta(days=_CACHE_START_SLACK_DAYS)
        if covers_start:
            # 끝이 이미 최신(슬랙 안)이면 조회 없이 캐시 사용
            if end_ts - cached.index.max() <= pd.Timedelta(days=_CACHE_END_SLACK_DAYS):
                return cached.loc[start_ts:end_ts]
            # 끝만 부족 → 마지막 캐시일 다음날부터만 증분 조회
            inc_start = (cached.index.max() + pd.Timedelta(days=1)).strftime("%Y%m%d")
            logger.info("pykrx 증분 조회: %s (%s~%s)", ticker, inc_start, end)
            try:
                new = _pykrx_ohlcv(ticker, inc_start, end)
            except Exception as exc:  # noqa: BLE001
                logger.debug("증분 조회 실패 %s: %s — 기존 캐시로 진행", ticker, exc)
                return cached.loc[start_ts:end_ts]
            if new is not None and not new.empty:
                combined = pd.concat([cached, new])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                combined.to_parquet(cache_path)
                return combined.loc[start_ts:end_ts]
            return cached.loc[start_ts:end_ts]  # 새 거래 데이터 없음(휴장 등)

    # 캐시 없음 or 시작 구간 미달 → 전체 조회
    logger.info("pykrx 조회: %s (%s~%s)", ticker, start, end)
    try:
        full = _pykrx_ohlcv(ticker, start, end)
    except Exception:  # noqa: BLE001
        if cached is not None and not cached.empty:
            logger.debug("전체 조회 실패 %s — 기존 캐시로 진행", ticker)
            return cached.loc[start_ts:end_ts]
        raise
    full.to_parquet(cache_path)
    return full.loc[start_ts:end_ts]


def refresh_latest() -> "pd.Timestamp | None":
    """FDR 전종목 스냅샷으로 캐시들을 '최신 거래일'까지 1콜에 대량 갱신한다.

    pykrx의 날짜별 스냅샷 엔드포인트는 이 환경에서 막혀 있어(per-ticker만 동작),
    per-ticker 개별 증분 조회는 수천 콜이 든다. 대신 FDR StockListing(전종목 현재가
    스냅샷)을 써서 최신 1거래일치를 한 번에 반영한다.

    안전장치:
      - FDR 스냅샷은 날짜 라벨이 없어 '최신 세션'만 준다. 앵커 종목(삼성전자)을
        pykrx per-ticker로 조회해 최신 거래일(snap_date)·직전 거래일(prev_date)·
        종가를 확정하고, FDR의 앵커 종가가 일치할 때만(장중/불일치면 스킵) 반영.
      - 캐시가 '직전 거래일(prev_date)'까지 정확히 차 있는 종목에만 snap_date 1행을
        붙인다(연속 보장). gap이 있는 종목은 건드리지 않고 per-ticker 조회에 맡긴다.

    반환: 갱신 기준 최신 거래일(snap_date), 갱신 불가/스킵 시 None.
    """
    anchor = "005930"  # 삼성전자 — 거래정지 없는 안정적 앵커
    try:
        recent = (pd.Timestamp.now() - pd.Timedelta(days=15)).strftime("%Y%m%d")
        today = pd.Timestamp.now().strftime("%Y%m%d")
        a = _pykrx_ohlcv(anchor, recent, today)
    except Exception as exc:  # noqa: BLE001
        logger.info("스냅샷 갱신 스킵 — 앵커 조회 실패: %s", exc)
        return None
    if a is None or len(a) < 2:
        return None
    snap_date, prev_date = a.index[-1], a.index[-2]
    anchor_close = int(round(float(a["close"].iloc[-1])))

    try:
        import FinanceDataReader as fdr
        listing = fdr.StockListing("KRX")
    except Exception as exc:  # noqa: BLE001
        logger.info("스냅샷 갱신 스킵 — FDR 조회 실패: %s", exc)
        return None

    arow = listing[listing["Code"] == anchor]
    if arow.empty or int(round(float(arow.iloc[0]["Close"]))) != anchor_close:
        logger.info("스냅샷 갱신 스킵 — FDR가 최신 세션(%s)과 불일치(장중 등)", snap_date.date())
        return None

    # 전종목 OHLCV 스냅샷을 dict로
    snap: dict[str, tuple] = {}
    for _, r in listing.iterrows():
        try:
            snap[r["Code"]] = (float(r["Open"]), float(r["High"]), float(r["Low"]),
                               float(r["Close"]), float(r["Volume"]))
        except (ValueError, TypeError):
            continue

    updated = 0
    for p in CACHE_DIR.glob("*.parquet"):
        code = p.stem
        if code.startswith("_") or code not in snap:
            continue
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        if df.empty or df.index.max() != prev_date:  # 이미 최신이거나 gap → 스킵
            continue
        o, h, l, c, v = snap[code]
        if any(pd.isna(x) for x in (o, h, l, c, v)):
            continue
        row = pd.DataFrame({"open": [o], "high": [h], "low": [l], "close": [c], "volume": [v]},
                           index=[snap_date])
        combined = pd.concat([df, row])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        combined.to_parquet(p)
        updated += 1

    logger.info("FDR 스냅샷 갱신: %d종목 → %s (직전 %s)", updated, snap_date.date(), prev_date.date())
    return snap_date
