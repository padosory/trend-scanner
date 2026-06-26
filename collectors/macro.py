"""거시경제 지표 수집: KOSPI, USD/KRW.

FinanceDataReader(FDR)로 최근 2 거래일을 받아 전일 종가와 전전일 대비 변화율을 계산한다.
"""

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class MacroData:
    date: pd.Timestamp
    kospi_close: float
    kospi_change_pct: float
    usdkrw: float
    usdkrw_change_pct: float


def fetch(target_date: str) -> "MacroData | None":
    """target_date(YYYYMMDD) 이하 마지막 거래일 기준 거시경제 스냅샷을 반환한다."""
    import FinanceDataReader as fdr  # 런타임 임포트 — 설치 확인 후 사용

    end = pd.Timestamp(target_date)
    start = (end - pd.DateOffset(days=15)).strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    try:
        kospi = fdr.DataReader("KS11", start, end_str)
        if kospi.empty or len(kospi) < 2:
            logger.warning("KOSPI 데이터 부족")
            return None
        kospi_close = float(kospi["Close"].iloc[-1])
        kospi_prev = float(kospi["Close"].iloc[-2])
        kospi_chg = (kospi_close / kospi_prev - 1) * 100
        scan_date: pd.Timestamp = kospi.index[-1]
    except Exception as exc:  # noqa: BLE001
        logger.warning("KOSPI 조회 실패: %s", exc)
        return None

    usdkrw_close = float("nan")
    usdkrw_chg = float("nan")
    try:
        fx = fdr.DataReader("USD/KRW", start, end_str)
        if not fx.empty and len(fx) >= 2:
            usdkrw_close = float(fx["Close"].iloc[-1])
            usdkrw_prev = float(fx["Close"].iloc[-2])
            usdkrw_chg = (usdkrw_close / usdkrw_prev - 1) * 100
    except Exception as exc:  # noqa: BLE001
        logger.warning("USD/KRW 조회 실패: %s", exc)

    return MacroData(
        date=scan_date,
        kospi_close=kospi_close,
        kospi_change_pct=kospi_chg,
        usdkrw=usdkrw_close,
        usdkrw_change_pct=usdkrw_chg,
    )
