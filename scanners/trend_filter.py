"""STEP1: 추세 필터 (미너비니 Trend Template 변형).

전체 종목 중 정배열 + 상승추세 종목만 추린다.

⚠️ 현재 라이브 파이프라인(collectors/stocks.py)에서 사용하지 않음 — 누락이 아니라
   백테스트 검증 결과에 따른 의도적 제외다. (설계서.md §6)
   - STEP1 단독: 평균 -0.67% (전 신호 중 최악 축, 평균손실 -5%)
   - STEP2+RS 위에 게이트로 얹어도 +0.90% → +0.50%로 악화 (수익 종목까지 걸러냄)
   최종 채택 시스템은 STEP2(돌파) + RS percentile 게이트 + 저항선 재이탈 청산이며,
   STEP1(추세필터)·STEP3(눌림목)는 단독이든 게이트든 도움이 안 돼 후보에서 제외됐다.
   백테스트 실험(backtest/experiments.py)에서는 여전히 참조하므로 파일은 보존한다.
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
