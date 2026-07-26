"""STEP3: 눌림목 스캔 (단테/와인스타인 방식).

워치리스트(STEP2 통과) 종목을 매일 재검사해서 매수 시점을 찾는다.

거래량 감소/반전 조건(decreasing-then-increasing)은 1~4차 백테스트에서 보유기간
중간값이 계속 1일에 머무는 등 예측력이 약하다는 신호가 나와서, 더 직관적인
가격반전 신호(양봉 마감 + 직전 N일 고가 돌파)로 교체했다 (설계서.md §6 4차→5차).

⚠️ 현재 라이브 파이프라인(collectors/stocks.py)에서 사용하지 않음 — trend_filter.py(STEP1)와
   마찬가지로 백테스트 검증 결과에 따른 의도적 제외다. (설계서.md §6)
   STEP2+RS 위에 눌림목 대기를 얹으면 거래수 3,328→191(95%+ 대기 중 탈락),
   평균 +0.90%→+0.10%로 붕괴 — "돌파 직후 즉시 진입"이 "눌림목 기다렸다 진입"보다
   낫다는 결론. 백테스트 실험(backtest/experiments.py)에서는 여전히 참조하므로 보존한다.
"""

import pandas as pd

import config


def passes(
    row: pd.Series,
    prior_highs: list[float],
    recent_low: float,
    breakout_low: float,
) -> bool:
    """row: 오늘 행. prior_highs: 오늘 이전 PULLBACK_PRICE_BREAKOUT_WINDOW일의 고가.
    recent_low: 워치리스트 등록일~오늘 사이 최저가.
    breakout_low: 워치리스트 등록(STEP2 통과) 이전 베이스 구간 저점.
    """
    if pd.isna(row["ma20"]) or not prior_highs:
        return False

    price_in_band = (
        row["ma20"] * config.PULLBACK_BAND_LOW
        <= row["close"]
        <= row["ma20"] * config.PULLBACK_BAND_HIGH
    )
    higher_low = recent_low > breakout_low
    bullish_close = row["close"] > row["open"]
    breaks_prior_high = row["close"] > max(prior_highs)

    return bool(price_in_band and higher_low and bullish_close and breaks_prior_high)
