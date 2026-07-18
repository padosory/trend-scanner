"""신호 히스토리 저장 및 성과 추적.

매 실행 시 당일 신호를 data/signal_history.json 에 누적 기록하고,
과거 신호의 현재가를 조회해 경과일·수익률·상태를 평가한다.
저장 파일은 GitHub Actions 워크플로에서 매 실행 후 repo 에 커밋되어 지속된다.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import config

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).parent / "data" / "signal_history.json"


@dataclass
class PerfRow:
    date: str          # 신호 발생일 YYYY-MM-DD
    ticker: str
    name: str
    entry: float       # 진입가(신호일 종가)
    current: float     # 현재가
    return_pct: float  # 수익률 %
    days_held: int     # 경과 일수(달력일)
    status: str        # 진행중 / 손절이탈


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


def evaluate(as_of: pd.Timestamp, price_lookup, lookback_days: int = 30) -> list[PerfRow]:
    """lookback_days 이내 신호를 현재가 기준으로 평가한다.

    Args:
        as_of: 평가 기준일 (보통 스캔 거래일)
        price_lookup: ticker -> 현재 종가(float, 실패 시 None) 콜러블
        lookback_days: 추적할 신호의 최대 경과 일수

    Returns:
        최근 신호 우선 정렬된 PerfRow 목록
    """
    history = load()
    cutoff = as_of - pd.Timedelta(days=lookback_days)

    rows: list[PerfRow] = []
    for e in history:
        sig_date = pd.Timestamp(e["date"])
        if sig_date < cutoff or sig_date > as_of:
            continue
        current = price_lookup(e["ticker"])
        if current is None or current != current:  # None 또는 NaN
            continue
        entry = float(e["entry"])
        ret = (current / entry - 1) * 100
        status = "손절이탈" if ret <= -config.STOP_LOSS_PCT * 100 else "진행중"
        rows.append(
            PerfRow(
                date=e["date"],
                ticker=e["ticker"],
                name=e.get("name", e["ticker"]),
                entry=entry,
                current=float(current),
                return_pct=ret,
                days_held=(as_of - sig_date).days,
                status=status,
            )
        )

    rows.sort(key=lambda r: r.date, reverse=True)
    return rows
