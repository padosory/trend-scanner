"""STEP1: 추세 필터 (미너비니 Trend Template 변형).

전체 종목 중 정배열 + 상승추세 종목만 추린다.
"""

import pandas as pd


def passes(row: pd.Series) -> bool:
    """row: indicators.add_indicators()가 적용된 DataFrame의 한 행."""
    if pd.isna(row["ma200"]) or pd.isna(row["ma150"]) or pd.isna(row["ma50"]):
        return False
    return bool(
        row["close"] > row["ma200"]
        and row["close"] > row["ma150"]
        and row["close"] > row["ma50"]
        and row["ma50"] > row["ma150"]
        and row["ma150"] > row["ma200"]
        and row["ma200_rising"]
    )
