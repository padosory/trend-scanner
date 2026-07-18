"""거시경제 지표 수집: KOSPI, KOSDAQ, S&P 500, NASDAQ, USD/KRW,
그리고 심리 지표(VIX·공포탐욕지수·BTC 도미넌스)."""

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)

# 공포·탐욕 지수 영문 분류 → 한국어
_FNG_KO = {
    "Extreme Fear": "극단적 공포",
    "Fear": "공포",
    "Neutral": "중립",
    "Greed": "탐욕",
    "Extreme Greed": "극단적 탐욕",
}


@dataclass
class MacroData:
    date: pd.Timestamp
    kospi_close: float
    kospi_change_pct: float
    kosdaq_close: float
    kosdaq_change_pct: float
    sp500_close: float
    sp500_change_pct: float
    nasdaq_close: float
    nasdaq_change_pct: float
    usdkrw: float
    usdkrw_change_pct: float
    # ── 안전자산 · 금리 · 크립토 · 심리 (실패 시 각각 graceful하게 비활성) ──
    gold: float = float("nan")
    gold_change_pct: float = float("nan")
    us10y: float = float("nan")
    us10y_change_pct: float = float("nan")
    kr3y: float = float("nan")
    kr3y_change_pct: float = float("nan")
    btc_usd: float = float("nan")
    btc_change_pct: float = float("nan")
    vix: float = float("nan")
    vix_change_pct: float = float("nan")
    fng_value: "int | None" = None
    fng_class: str = ""
    btc_dominance: float = float("nan")
    btc_dominance_change_pct: float = float("nan")
    # 카드별 최근 종가 시계열(스파크라인용). key -> list[float]
    sparklines: dict = field(default_factory=dict)


def _fetch_series(fdr, symbol: str, start: str, end: str) -> "tuple[float, float, list[float]]":
    """종가, 전일 대비 변화율(%), 최근 종가 시계열을 반환. 실패 시 (nan, nan, [])."""
    try:
        df = fdr.DataReader(symbol, start, end)
        closes = [float(x) for x in df["Close"].dropna().tolist()] if not df.empty else []
        if len(closes) < 2:
            return float("nan"), float("nan"), closes
        return closes[-1], (closes[-1] / closes[-2] - 1) * 100, closes[-15:]
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 조회 실패: %s", symbol, exc)
        return float("nan"), float("nan"), []


def _fetch_fng() -> "tuple[int | None, str]":
    """공포·탐욕 지수(크립토, alternative.me) — (값, 한국어분류). 실패 시 (None, '')."""
    import requests

    try:
        data = requests.get(
            "https://api.alternative.me/fng/?limit=1", timeout=10
        ).json()["data"][0]
        value = int(data["value"])
        return value, _FNG_KO.get(data["value_classification"], data["value_classification"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("공포탐욕지수 조회 실패: %s", exc)
        return None, ""


def _fetch_btc_dominance() -> float:
    """BTC 도미넌스(%, CoinGecko). 실패 시 nan."""
    import requests

    try:
        g = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()
        return float(g["data"]["market_cap_percentage"]["btc"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("BTC 도미넌스 조회 실패: %s", exc)
        return float("nan")


def fetch(target_date: str) -> "MacroData | None":
    """target_date(YYYYMMDD) 이하 마지막 거래일 기준 거시경제 스냅샷을 반환한다."""
    import FinanceDataReader as fdr

    end = pd.Timestamp(target_date)
    start = (end - pd.DateOffset(days=15)).strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    # KOSPI 는 필수 — 실패 시 None 반환
    try:
        kospi_df = fdr.DataReader("KS11", start, end_str)
        if kospi_df.empty or len(kospi_df) < 2:
            logger.warning("KOSPI 데이터 부족")
            return None
        kospi_series = [float(x) for x in kospi_df["Close"].dropna().tolist()][-15:]
        kospi_close = kospi_series[-1]
        kospi_chg   = (kospi_series[-1] / kospi_series[-2] - 1) * 100
        scan_date: pd.Timestamp = kospi_df.index[-1]
    except Exception as exc:  # noqa: BLE001
        logger.warning("KOSPI 조회 실패: %s", exc)
        return None

    kosdaq_close, kosdaq_chg, kosdaq_s = _fetch_series(fdr, "KQ11",    start, end_str)
    sp500_close,  sp500_chg,  sp500_s  = _fetch_series(fdr, "US500",   start, end_str)
    nasdaq_close, nasdaq_chg, nasdaq_s = _fetch_series(fdr, "IXIC",    start, end_str)
    usdkrw_close, usdkrw_chg, usdkrw_s = _fetch_series(fdr, "USD/KRW", start, end_str)

    # ── 안전자산 · 금리 · 크립토 · 심리 ──
    gold_close, gold_chg, gold_s   = _fetch_series(fdr, "GC=F", start, end_str)      # 금 선물(USD/oz)
    us10y_close, us10y_chg, us10y_s = _fetch_series(fdr, "US10YT", start, end_str)   # 미 국채 10년 금리(%)
    from collectors.bond import fetch_kr3y
    kr3y_close, kr3y_chg, kr3y_s = fetch_kr3y(start.replace("-", ""), end_str.replace("-", ""))  # 국고채 3년(%)
    btc_usd, btc_chg, btc_s        = _fetch_series(fdr, "BTC/USD", start, end_str)
    vix_close, vix_chg, vix_s      = _fetch_series(fdr, "VIX", start, end_str)
    fng_value, fng_class = _fetch_fng()
    btc_dom = _fetch_btc_dominance()

    # 스파크라인은 2점 이상 있는 시계열만 담는다
    sparklines = {
        k: v for k, v in {
            "kospi": kospi_series, "kosdaq": kosdaq_s, "sp500": sp500_s,
            "nasdaq": nasdaq_s, "usdkrw": usdkrw_s, "gold": gold_s,
            "us10y": us10y_s, "kr3y": kr3y_s, "btc": btc_s, "vix": vix_s,
        }.items() if len(v) >= 2
    }

    return MacroData(
        date=scan_date,
        kospi_close=kospi_close,
        kospi_change_pct=kospi_chg,
        kosdaq_close=kosdaq_close,
        kosdaq_change_pct=kosdaq_chg,
        sp500_close=sp500_close,
        sp500_change_pct=sp500_chg,
        nasdaq_close=nasdaq_close,
        nasdaq_change_pct=nasdaq_chg,
        usdkrw=usdkrw_close,
        usdkrw_change_pct=usdkrw_chg,
        gold=gold_close,
        gold_change_pct=gold_chg,
        us10y=us10y_close,
        us10y_change_pct=us10y_chg,
        kr3y=kr3y_close,
        kr3y_change_pct=kr3y_chg,
        btc_usd=btc_usd,
        btc_change_pct=btc_chg,
        vix=vix_close,
        vix_change_pct=vix_chg,
        fng_value=fng_value,
        fng_class=fng_class,
        btc_dominance=btc_dom,
        sparklines=sparklines,
    )
