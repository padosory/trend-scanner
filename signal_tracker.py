"""신호 히스토리 저장 및 성과 추적.

매 실행 시 당일 신호를 data/signal_history.json 에 누적 기록하고,
과거 신호의 현재가를 조회해 경과일·수익률·상태를 평가한다.
저장 파일은 GitHub Actions 워크플로에서 매 실행 후 repo 에 커밋되어 지속된다.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).parent / "data" / "signal_history.json"

# ticker -> 지표(add_indicators)가 적용된 OHLCV DataFrame(또는 None) 콜러블.
# 'close'·'resistance_60' 컬럼과 날짜 오름차순 인덱스를 가져야 한다.
OhlcvLookup = Callable[[str], "pd.DataFrame | None"]


@dataclass
class PerfRow:
    date: str          # 신호 발생일 YYYY-MM-DD
    ticker: str
    name: str
    entry: float       # 진입가(신호일 종가)
    current: float     # 현재가(진행중) 또는 청산가(청산)
    return_pct: float  # 수익률 %
    days_held: int     # 경과/보유 일수(달력일)
    status: str        # 진행중 / 청산 (채택 전략과 동일하게 저항선 재이탈 기준)


@dataclass
class PerfSummary:
    """추적 히스토리 집계 — 청산된 신호 기준 성과 요약."""
    n_closed: int      # 청산 완료 신호 수
    n_open: int        # 아직 진행중인 신호 수
    win_rate: float    # 청산 신호 중 수익 비율 %
    avg_return: float  # 청산 신호 평균 수익률 %
    avg_days: float    # 청산 신호 평균 보유일
    window_days: int   # 집계 대상 기간(달력일)


def load() -> list[dict]:
    """히스토리 파일을 로드한다. 없거나 손상 시 빈 리스트."""
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("신호 히스토리 로드 실패: %s", exc)
        return []


def record(signals, scan_date: pd.Timestamp, name_map: dict[str, str]) -> None:
    """당일 신호를 히스토리에 누적한다 (같은 날짜+종목 중복 방지)."""
    history = load()
    existing = {(e["date"], e["ticker"]) for e in history}
    date_str = scan_date.strftime("%Y-%m-%d")

    added = 0
    for s in signals:
        if (date_str, s.ticker) in existing:
            continue
        history.append(
            {
                "date": date_str,
                "ticker": s.ticker,
                "name": name_map.get(s.ticker, s.ticker),
                "entry": round(float(s.close), 2),
            }
        )
        added += 1

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("신호 히스토리 기록: +%d건 (누적 %d건)", added, len(history))


def _perf_row(entry: dict, as_of: pd.Timestamp, df: "pd.DataFrame | None") -> "PerfRow | None":
    """신호 하나를 채택 전략(저항선 재이탈 청산)으로 평가한다.

    신호일(T) 이후 첫 거래일부터 종가가 그날의 저항선(resistance_60) 아래로
    마감하면 '청산'으로 보고 그 종가를 청산가로 쓴다. 아직 저항선 위면 '진행중'.
    이는 백테스트가 채택한 청산 규칙(설계서 §6: close < 직전60일저항선)과 동일하다.
    """
    ticker = entry["ticker"]
    if df is None or df.empty or "resistance_60" not in df.columns or "close" not in df.columns:
        return None

    sig_date = pd.Timestamp(entry["date"])
    entry_price = float(entry["entry"])
    name = entry.get("name", ticker)

    after = df[df.index > sig_date]

    exit_price: float | None = None
    exit_date: pd.Timestamp | None = None
    for date, r in after.iterrows():
        close = r["close"]
        resist = r["resistance_60"]
        if pd.isna(close) or pd.isna(resist):
            continue
        if close < resist:
            exit_price = float(close)
            exit_date = date
            break

    if exit_price is not None:
        ret = (exit_price / entry_price - 1) * 100
        return PerfRow(
            date=entry["date"], ticker=ticker, name=name, entry=entry_price,
            current=exit_price, return_pct=ret,
            days_held=(exit_date - sig_date).days, status="청산",
        )

    # 진행중 — 신호일 이후 마지막 유효 종가 기준
    valid = after["close"].dropna()
    current = float(valid.iloc[-1]) if not valid.empty else entry_price
    ret = (current / entry_price - 1) * 100
    return PerfRow(
        date=entry["date"], ticker=ticker, name=name, entry=entry_price,
        current=current, return_pct=ret,
        days_held=(as_of - sig_date).days, status="진행중",
    )


def _rows_in_window(as_of: pd.Timestamp, ohlcv_lookup: OhlcvLookup, window_days: int) -> list[PerfRow]:
    """window_days 이내 신호를 평가해 PerfRow 목록으로 반환 (내부 공용)."""
    history = load()
    cutoff = as_of - pd.Timedelta(days=window_days)

    rows: list[PerfRow] = []
    for e in history:
        sig_date = pd.Timestamp(e["date"])
        if sig_date < cutoff or sig_date > as_of:
            continue
        row = _perf_row(e, as_of, ohlcv_lookup(e["ticker"]))
        if row is not None:
            rows.append(row)
    return rows


def evaluate(as_of: pd.Timestamp, ohlcv_lookup: OhlcvLookup, lookback_days: int = 30) -> list[PerfRow]:
    """lookback_days 이내 신호를 채택 전략 기준으로 평가한다.

    Args:
        as_of: 평가 기준일 (보통 스캔 거래일)
        ohlcv_lookup: ticker -> 지표 적용 OHLCV(DataFrame, 실패 시 None) 콜러블
        lookback_days: 추적할 신호의 최대 경과 일수

    Returns:
        최근 신호 우선 정렬된 PerfRow 목록
    """
    rows = _rows_in_window(as_of, ohlcv_lookup, lookback_days)
    rows.sort(key=lambda r: r.date, reverse=True)
    return rows


def summarize(as_of: pd.Timestamp, ohlcv_lookup: OhlcvLookup, window_days: int = 90) -> "PerfSummary | None":
    """window_days 이내 신호의 청산 성과를 집계한다.

    청산된 신호만으로 승률·평균수익률·평균보유일을 계산한다(진행중은 개수만 센다).
    청산된 신호가 하나도 없으면 None.
    """
    rows = _rows_in_window(as_of, ohlcv_lookup, window_days)
    closed = [r for r in rows if r.status == "청산"]
    n_open = sum(1 for r in rows if r.status == "진행중")
    if not closed:
        return None

    wins = sum(1 for r in closed if r.return_pct > 0)
    return PerfSummary(
        n_closed=len(closed),
        n_open=n_open,
        win_rate=wins / len(closed) * 100,
        avg_return=sum(r.return_pct for r in closed) / len(closed),
        avg_days=sum(r.days_held for r in closed) / len(closed),
        window_days=window_days,
    )
