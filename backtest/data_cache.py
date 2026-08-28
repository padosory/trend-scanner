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
_NAMES_CACHE_PATH = CACHE_DIR / "_names.parquet"
_CACHE_END_SLACK_DAYS = 2    # 요청 종료일과 캐시 마지막일 허용 간격(달력일). 주말 정도만 관용.
                             # 과거 구간 조회에만 쓴다 — 최신 구간은 아래 거래일 기준으로
                             # 엄격히 판정한다(_cache_is_fresh 참조).
_CACHE_START_SLACK_DAYS = 7  # 요청 시작일이 공휴일/주말이면 실제 첫 거래일이 며칠 뒤일 수 있음 (근로자의날·설·추석 등)

_ANCHOR_TICKER = "005930"    # 삼성전자 — 거래정지가 없어 '거래일' 판정 앵커로 쓴다
_SETTLED_HOUR_KST = 16       # 이 시각(KST) 전이면 당일 봉을 미완성(장중)으로 본다
_RECENT_REQUEST_DAYS = 7     # 요청 종료일이 오늘로부터 이 안이면 '최신 구간' 조회로 본다
_CACHE_OVERLAP_DAYS = 5      # 증분 조회를 캐시 마지막일보다 이만큼 앞에서 시작해 겹치는
                             # 구간으로 계열을 검증한다(소급조정 감지). 호출 수는 그대로다.
_READJUST_TOL = 0.01         # 겹치는 구간 종가의 허용 상대오차. KRX 호가는 정수라 사실상 0.
# KRX 가격제한폭은 ±30%이므로 하루 변동은 [0.70, 1.30]을 벗어날 수 없다. 여유를 둔
# 이 범위 밖이면 실제 등락이 아니라 소급조정(분할 0.5배·병합/감자 2~5배)으로 본다.
_ADJUST_RATIO_LO = 0.6
_ADJUST_RATIO_HI = 1.7

_listing_df: pd.DataFrame | None = None
_shares_by_ticker: dict[str, float] | None = None

# 앵커 조회 결과의 프로세스 캐시. _anchor_resolved는 '이미 시도했다'는 표시로,
# 실패(None)도 기억해야 한다 — _cache_is_fresh()가 종목마다 호출되므로 실패를
# 기억하지 않으면 KRX가 막혔을 때 2천 종목 × 타임아웃(20s)만큼 재시도한다.
_anchor_cache: pd.DataFrame | None = None
_anchor_resolved = False

# 계열 불연속으로 전체 재조회를 이미 한 종목. 한 프로세스에서 한 번만 시도해,
# 재조회 후에도 불연속이 남는 종목(진짜 그런 시세)이 매번 재조회를 유발하지 않게 한다.
_refetched: set[str] = set()

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


def get_name_map() -> dict[str, str]:
    """{티커: 종목명}. FDR StockListing로 만들고 캐시한다.

    FDR/KRX가 간헐적으로 막혀 조회가 실패할 수 있으므로(빈 응답→JSONDecodeError),
    실패 시 직전 캐시(_names.parquet)로 폴백한다. 캐시도 없으면 빈 dict(종목명
    자리에 티커가 노출되지만 리포트는 정상 생성). CI 캐시로 파케이가 유지된다.
    """
    try:
        import FinanceDataReader as fdr
        listing = fdr.StockListing("KRX")
        names = dict(zip(listing["Code"], listing["Name"]))
        if names:
            pd.DataFrame({"Code": list(names), "Name": list(names.values())}).to_parquet(_NAMES_CACHE_PATH)
            return names
    except Exception as exc:  # noqa: BLE001
        logger.warning("종목명 조회 실패 — 캐시 폴백: %s", exc)

    if _NAMES_CACHE_PATH.exists():
        try:
            df = pd.read_parquet(_NAMES_CACHE_PATH)
            return dict(zip(df["Code"], df["Name"]))
        except Exception:  # noqa: BLE001
            pass
    return {}


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


def _now_kst() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)


def _anchor_history() -> "pd.DataFrame | None":
    """앵커 종목의 최근 OHLCV — **완결된 거래일만** 남긴다 (프로세스 내 1회 조회).

    장 마감 전에 조회하면 pykrx가 당일 미완성 봉을 마지막 행으로 준다. 그걸
    '완결된 거래일'로 취급하면 장중 가격이 확정 종가처럼 캐시에 박히므로 잘라낸다.
    """
    global _anchor_cache, _anchor_resolved
    if _anchor_resolved:
        return _anchor_cache

    _anchor_resolved = True  # 성공·실패 모두 1회로 확정 (종목별 재시도 방지)

    now_kst = _now_kst()
    recent = (now_kst - pd.Timedelta(days=15)).strftime("%Y%m%d")
    try:
        df = _pykrx_ohlcv(_ANCHOR_TICKER, recent, now_kst.strftime("%Y%m%d"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("앵커(%s) 조회 실패 — 거래일 판정 불가, 신선도 검증을 건너뛴다: %s",
                       _ANCHOR_TICKER, exc)
        return None
    if df is None or df.empty:
        logger.warning("앵커(%s) 응답 없음 — 거래일 판정 불가", _ANCHOR_TICKER)
        return None

    if df.index[-1] == now_kst.normalize() and now_kst.hour < _SETTLED_HOUR_KST:
        logger.info("장중 실행 감지 — 당일(%s) 미완성 봉 제외", df.index[-1].date())
        df = df.iloc[:-1]
    if df.empty:
        return None

    _anchor_cache = df
    return df


def get_last_trading_day() -> "pd.Timestamp | None":
    """오늘(KST) 기준 마지막 '완결된' 거래일. 판정 불가 시 None.

    데이터 신선도의 기준선이다. 캐시 최신일과 '요청 종료일(달력일)'의 간격으로
    신선도를 재면, 하루 뒤처진 캐시가 휴장 때문인지 갱신 실패 때문인지 구분되지
    않는다 — 2026-08-26 거래일이 통째로 유실되고도 실행이 성공으로 끝난 직접 원인이다.
    """
    df = _anchor_history()
    return None if df is None else df.index[-1]


def _last_trading_day_on_or_before(end_ts: pd.Timestamp) -> "pd.Timestamp | None":
    """end_ts 이하의 마지막 거래일. 앵커 이력으로 판정하고, 불가하면 None.

    신선도의 기준선은 '오늘의 마지막 거래일'이 아니라 '요청 종료일 이하의 마지막
    거래일'이다. 둘은 장 마감 후 실행에서 갈라진다 — 2026-08-28 17:28 수동 실행이
    그랬다(요청 종료일 8/27, 오늘의 마지막 거래일 8/28).
    """
    df = _anchor_history()
    if df is None:
        return None
    idx = df.index[df.index <= end_ts]
    return idx[-1] if len(idx) else None


def _cache_is_fresh(cached: pd.DataFrame, end_ts: pd.Timestamp) -> bool:
    """캐시가 요청 종료일 기준으로 최신인지.

    최신 구간 조회(요청 종료일이 오늘 근처)면 실제 거래일 기준으로 엄격히 본다.
    과거 구간 조회(백테스트)는 종료일이 휴장일일 수 있어 기존 슬랙 규칙을 쓴다 —
    여기에 거래일 기준을 적용하면 매 실행마다 전 종목 재조회가 발생한다.
    """
    if cached.empty:
        return False
    if end_ts >= _now_kst().normalize() - pd.Timedelta(days=_RECENT_REQUEST_DAYS):
        last_td = get_last_trading_day()
        if last_td is not None:
            # end_ts가 마지막 거래일보다 앞설 수 있다 — 장 마감 후 실행이 그렇다.
            # 예전에는 그 경우 엄격 판정을 통째로 건너뛰고 아래 슬랙 규칙으로
            # 떨어져서, 3일 밀린 캐시가 '신선'으로 통과했다(2026-08-28 run #60:
            # 요청 8/27 · 캐시 8/25 · 간격 2일 <= 슬랙 2 → 재조회 없이 통과).
            expected = (last_td if end_ts >= last_td
                        else _last_trading_day_on_or_before(end_ts))
            if expected is not None:
                return bool(cached.index.max() >= expected)
    return bool(end_ts - cached.index.max() <= pd.Timedelta(days=_CACHE_END_SLACK_DAYS))


def _is_recent_request(end_ts: pd.Timestamp) -> bool:
    """요청 종료일이 오늘 근처인가 — 최신 구간 조회와 과거(백테스트) 조회를 가른다."""
    return bool(end_ts >= _now_kst().normalize() - pd.Timedelta(days=_RECENT_REQUEST_DAYS))


def _series_has_break(df: pd.DataFrame) -> bool:
    """계열 안에 하루 변동으로 설명 안 되는 단절이 있는지.

    겹침 검증(_series_readjusted)은 조정을 '앞으로' 막을 뿐, 이미 캐시에 박힌
    불연속은 못 고친다 — 단절이 최근 며칠 밖에 있으면 겹침 창에 안 걸리기 때문이다.
    실제로 배포된 리포트에 그런 계열이 셋 있었다(금호전기 07-31 4.60배,
    조아제약 08-26 5.00배, 티앤엘 08-06 0.49배).
    """
    if df.empty or "close" not in df.columns or len(df) < 2:
        return False
    c = df["close"].astype(float).to_numpy()
    prev, cur = c[:-1], c[1:]
    ok = (prev > 0) & (cur > 0)
    if not ok.any():
        return False
    ratio = cur[ok] / prev[ok]
    return bool(((ratio < _ADJUST_RATIO_LO) | (ratio > _ADJUST_RATIO_HI)).any())


def _series_readjusted(cached: pd.DataFrame, new: pd.DataFrame) -> bool:
    """겹치는 구간의 종가가 어긋나면 소급조정(액면분할·병합·감자)으로 본다.

    pykrx는 조정이 일어나면 과거 시세를 통째로 다시 계산해서 준다. 증분만 붙이면
    조정 전 행이 캐시에 그대로 남아 한 계열에 두 기준이 섞인다. 실제로 그랬다 —
    2026-07-31 금호전기 4.60배, 08-26 조아제약 5.00배, 08-06 티앤엘 0.49배.
    이 불연속은 MA200·52주 신고가·resistance_60을 전부 오염시킨다(가짜 돌파를
    만들거나, 반대로 진짜 돌파를 가린다).
    """
    overlap = cached.index.intersection(new.index)
    if overlap.empty:
        return False
    a = cached.loc[overlap, "close"].astype(float)
    b = new.loc[overlap, "close"].astype(float)
    usable = (a > 0) & (b > 0)      # 거래정지일 0값은 비교에서 뺀다
    if not usable.any():
        return False
    return bool(((b[usable] / a[usable]) - 1).abs().max() > _READJUST_TOL)


def _is_plausible_daily_move(prev_close: float, close: float) -> bool:
    """전일 종가 대비 하루 변동으로 설명 가능한 값인지. 판정 불가면 True(관용)."""
    if not (prev_close > 0 and close > 0):
        return True
    return _ADJUST_RATIO_LO <= close / prev_close <= _ADJUST_RATIO_HI


def fetch_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """start/end: YYYYMMDD. 캐시를 최대한 재사용하고, 끝만 부족하면 증분 조회한다.

    - 캐시가 요청 구간을 덮으면(최신 구간은 마지막 거래일까지 차 있어야) 그대로 반환.
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
            # 캐시에 이미 박힌 불연속은 겹침 검증으로 못 잡는다(단절이 창 밖에 있다).
            # 최신 구간 조회에 한해 계열 전체를 훑어 한 번만 정리한다.
            if (ticker not in _refetched and _is_recent_request(end_ts)
                    and _series_has_break(cached)):
                _refetched.add(ticker)
                logger.warning("캐시 계열 불연속 감지 %s — 전체 재조회로 정리", ticker)
                try:
                    full = _pykrx_ohlcv(ticker, start, end)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("불연속 정리용 전체 재조회 실패 %s: %s — 기존 캐시로 진행",
                                   ticker, exc)
                    full = None
                if full is not None and not full.empty:
                    full.to_parquet(cache_path)
                    return full.loc[start_ts:end_ts]
            # 끝이 이미 최신이면 조회 없이 캐시 사용
            if _cache_is_fresh(cached, end_ts):
                return cached.loc[start_ts:end_ts]
            # 끝만 부족 → 증분 조회. 시작을 캐시 마지막일보다 _CACHE_OVERLAP_DAYS
            # 앞에서 잡아, 겹치는 구간으로 계열이 소급조정됐는지 함께 검증한다.
            # 조회 횟수는 그대로고 범위만 며칠 넓어진다.
            inc_start = (cached.index.max()
                         - pd.Timedelta(days=_CACHE_OVERLAP_DAYS)).strftime("%Y%m%d")
            logger.info("pykrx 증분 조회: %s (%s~%s)", ticker, inc_start, end)
            try:
                new = _pykrx_ohlcv(ticker, inc_start, end)
            except Exception as exc:  # noqa: BLE001
                logger.debug("증분 조회 실패 %s: %s — 기존 캐시로 진행", ticker, exc)
                return cached.loc[start_ts:end_ts]
            if new is not None and not new.empty and _series_readjusted(cached, new):
                # 조정 전후를 병합하면 불연속이 남는다 — 이 종목만 전체 재조회해
                # 계열을 하나로 통일한다.
                logger.warning("소급조정 감지 %s — 전체 재조회로 계열 통일", ticker)
                try:
                    full = _pykrx_ohlcv(ticker, start, end)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("소급조정 후 전체 재조회 실패 %s: %s — 기존 캐시로 진행",
                                   ticker, exc)
                    return cached.loc[start_ts:end_ts]
                if full is not None and not full.empty:
                    full.to_parquet(cache_path)
                    return full.loc[start_ts:end_ts]
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
        종가를 확정하고, FDR의 앵커 종가가 일치할 때만 반영한다.
      - 앵커는 _anchor_history()가 당일 미완성 봉을 잘라낸 뒤의 값이다. 따라서
        장중 실행이면 pykrx 앵커 종가(직전 세션)와 FDR 앵커 가격(실시간)이 어긋나
        자동으로 스킵된다. 예전에는 양쪽 다 실시간 값이라 이 가드가 장중을 전혀
        걸러내지 못했다.
      - 캐시가 '직전 거래일(prev_date)'까지 정확히 차 있는 종목에만 snap_date 1행을
        붙인다(연속 보장). 하루보다 더 밀린 종목은 이 경로로 못 메우므로 건드리지
        않고 per-ticker 증분 조회에 맡긴다 — 그 종목 수를 로그로 남긴다.

    반환: 갱신 기준 최신 거래일(snap_date), 갱신 불가/스킵 시 None.
    """
    anchor = _ANCHOR_TICKER
    a = _anchor_history()
    if a is None or len(a) < 2:
        logger.info("스냅샷 갱신 스킵 — 앵커 거래일 판정 불가")
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
        logger.info(
            "스냅샷 갱신 스킵 — FDR 앵커가 최신 완결 세션(%s)과 불일치(장중 실행 등). "
            "밀린 종목은 per-ticker 증분 조회로 메운다", snap_date.date(),
        )
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
    behind = 0   # prev_date보다 더 밀린 종목 — 스냅샷 1행으로는 못 메운다
    adjusted = 0 # 소급조정 의심 — 스냅샷을 붙이지 않고 전체 재조회에 맡긴다
    for p in CACHE_DIR.glob("*.parquet"):
        code = p.stem
        if code.startswith("_") or code not in snap:
            continue
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        if df.empty or df.index.max() != prev_date:  # 이미 최신이거나 gap → 스킵
            if not df.empty and df.index.max() < prev_date:
                behind += 1
            continue
        o, h, l, c, v = snap[code]
        if any(pd.isna(x) for x in (o, h, l, c, v)):
            continue
        # 소급조정 가드. FDR 스냅샷은 '지금 기준' 값이라, 조정이 일어난 종목에
        # 붙이면 조정 전 계열 위에 조정 후 1행이 얹혀 불연속이 생긴다. 하루 변동으로
        # 설명 안 되는 값이면 붙이지 않고 넘긴다 — 캐시가 낡은 채로 남아 fetch_ohlcv()의
        # 겹침 검증이 전체 재조회로 정리한다.
        if not _is_plausible_daily_move(float(df["close"].iloc[-1]), float(c)):
            adjusted += 1
            continue
        row = pd.DataFrame({"open": [o], "high": [h], "low": [l], "close": [c], "volume": [v]},
                           index=[snap_date])
        combined = pd.concat([df, row])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        combined.to_parquet(p)
        updated += 1

    logger.info("FDR 스냅샷 갱신: %d종목 → %s (직전 %s)", updated, snap_date.date(), prev_date.date())
    if adjusted:
        logger.warning(
            "소급조정 의심 %d종목 — 스냅샷 갱신 보류, per-ticker 전체 재조회로 정리한다",
            adjusted,
        )
    if behind:
        logger.warning(
            "캐시가 %s보다 더 밀린 종목 %d개 — 스냅샷으로 못 메움, per-ticker 증분 조회로 복구 예정"
            " (실행이 길어질 수 있음)", prev_date.date(), behind,
        )
    return snap_date
