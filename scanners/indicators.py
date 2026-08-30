"""OHLCV DataFrame에 스캐너가 쓰는 보조지표 컬럼을 추가한다."""

import pandas as pd

import config


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df: open/high/low/close/volume 컬럼, 날짜 오름차순 인덱스.

    rolling()은 현재 행까지의 과거 데이터만 사용하므로 미래데이터 참조(look-ahead) 없음.
    """
    df = df.copy()

    for window in config.MA_WINDOWS:
        df[f"ma{window}"] = df["close"].rolling(window).mean()

    df["ma200_rising"] = df["ma200"] > df["ma200"].shift(config.MA200_SLOPE_LOOKBACK)

    df["high_52w"] = df["high"].rolling(252).max()
    df["resistance_60"] = df["high"].shift(1).rolling(config.RESISTANCE_WINDOW).max()
    # 신규상장 직후나 장기 거래정지 구간은 직전 N일 고가가 전부 0이라 저항선이 0이 된다.
    # 0을 그대로 두면 (1) `close > resistance_60`이 항상 참이라 가짜 돌파가 잡히고
    # (2) 손절폭이 항상 100%로 계산된다. 저항선이 성립하지 않는 구간이므로 결측 처리한다.
    df.loc[df["resistance_60"] <= 0, "resistance_60"] = float("nan")
    df["vol_avg20"] = df["volume"].rolling(20).mean()

    df["trading_value"] = df["close"] * df["volume"]
    df["avg_trading_value20"] = df["trading_value"].rolling(20).mean()

    return df
