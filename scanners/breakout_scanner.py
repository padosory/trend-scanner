"""STEP2: 돌파 스캔 (오닐 방식).

STEP1 통과 종목 중 52주 고가권 돌파 시도 종목을 워치리스트로 추린다.
"""

import pandas as pd

import config


def passes(row: pd.Series) -> bool:
    """row: indicators.add_indicators()가 적용된 DataFrame의 한 행."""
    if pd.isna(row["high_52w"]) or pd.isna(row["vol_avg20"]) or pd.isna(row["resistance_60"]):
        return False
    return bool(
        row["close"] >= row["high_52w"] * config.BREAKOUT_HIGH_PCT
        and row["volume"] >= row["vol_avg20"] * config.BREAKOUT_VOLUME_MULT
        and row["close"] > row["resistance_60"]
    )
