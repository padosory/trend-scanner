"""신호 히스토리 저장 및 성과 추적.

매 실행 시 당일 신호를 data/signal_history.json 에 누적 기록하고,
과거 신호의 현재가를 조회해 경과일·수익률·상태를 평가한다.
저장 파일은 GitHub Actions 워크플로에서 매 실행 후 repo 에 커밋되어 지속된다.

진입가 규약은 **신호일 다음 거래일의 시가(T+1 시가)**다. 신호는 종가가 확정돼야
나오므로 신호일 종가로는 살 수 없다. 종가를 진입가로 쓰면 '그날 종가를 미리 알고
그 값에 산다'는 룩어헤드가 되고, 검증(trend_scanner_검증결과_v3_CLOSED.md §1.1)에서
이 차이 하나로 평균 수익률의 부호가 뒤집혔다. 참고용 패널이라도 같은 편향을
반복하지 않기 위해 T+1 시가를 쓴다.

진입가·청산가는 기록해둔 숫자가 아니라 **평가 시점의 시세 계열에서 매번 다시
읽는다.** 액면분할·병합이 일어나면 과거 시세가 소급 조정되는데, 기록 당시의 원본
가격과 조정된 현재가를 섞어 계산하면 분할 배수만큼의 가짜 손익이 찍힌다(2026-08-05
티앤엘 신호가 실제 -0.6%인데 -50.9%로 표시된 사례). 양쪽을 같은 계열에서 읽으면
이 왜곡이 생기지 않는다. 기록된 신호일 종가는 참고·정합성 점검용으로만 남긴다.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).parent / "data" / "signal_history.json"

# 기록된 신호일 종가와 시세 계열의 종가가 이 비율 이상 어긋나면 소급 조정
# (액면분할·병합 등)으로 보고 경고한다. 수익률 계산에는 영향이 없다 — 계산은
# 양쪽 모두 조정된 계열에서 읽으므로, 이 경고는 기록 자체의 정합성 알림이다.
_READJUST_TOLERANCE = 0.01

STATUS_PENDING = "진입대기"   # 신호 당일 — T+1 시가가 아직 없어 진입이 성립하지 않음
STATUS_OPEN = "진행중"
STATUS_CLOSED = "청산"

# ticker -> 지표(add_indicators)가 적용된 OHLCV DataFrame(또는 None) 콜러블.
# 'open'·'close'·'resistance_60' 컬럼과 날짜 오름차순 인덱스를 가져야 한다.
OhlcvLookup = Callable[[str], "pd.DataFrame | None"]


@dataclass
class PerfRow:
    date: str                  # 신호 발생일 YYYY-MM-DD
    ticker: str
    name: str
    entry: float | None        # 진입가(T+1 시가). 진입대기면 None
    current: float | None      # 현재가(진행중) 또는 청산가(청산). 진입대기면 None
    return_pct: float | None   # 수익률 %. 진입대기면 None
    days_held: int             # 진입일 기준 경과/보유 일수(달력일)
    status: str                # 진입대기 / 진행중 / 청산

    @property
    def held_label(self) -> str:
        """보유기간 표시. 진입 당일에 청산된 건은 '0일'보다 '당일'이 정확하다."""
        if self.status == STATUS_PENDING:
            return "—"
        return "당일" if self.days_held == 0 else f"{self.days_held}일"


@dataclass
class PerfSummary:
    """추적 히스토리 집계 — 청산된 신호 기준 성과 요약."""
    n_closed: int      # 청산 완료 신호 수
    n_open: int        # 아직 진행중인 신호 수
    n_pending: int     # 진입대기(T+1 미도래) 신호 수
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


def _signal_close(record: dict) -> "float | None":
    """기록된 신호일 종가. 구 히스토리는 이 값을 'entry' 키로 갖고 있다.

    구 키 이름이 'entry'인 것은 당시 이 값을 진입가로 썼기 때문이다. 지금은
    진입가가 아니라 참고값이므로 새로 쓰는 기록은 'signal_close'를 쓴다.
    """
    v = record.get("signal_close", record.get("entry"))
    return None if v is None else float(v)


def _valid_price(*candidates) -> "float | None":
    """첫 번째 유효 가격(양수·비결측)을 고른다.

    거래정지일은 시가·고가·저가가 0으로 들어오는 경우가 있어(2026-08-26 탑코미디어)
    0을 가격으로 받으면 수익률이 무한대가 된다. 시가가 못 쓸 값이면 종가로 폴백한다.
    """
    for v in candidates:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0 and f == f:  # f == f: NaN 배제
            return f
    return None


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
                # 진입가가 아니라 신호 당시 종가. 진입가는 평가 시점에 T+1 시가로 구한다.
                "signal_close": round(float(s.close), 2),
            }
        )
        added += 1

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("신호 히스토리 기록: +%d건 (누적 %d건)", added, len(history))


def _check_readjusted(record: dict, df: pd.DataFrame, sig_date: pd.Timestamp) -> None:
    """기록된 신호일 종가와 현재 시세 계열이 어긋나면 경고만 남긴다.

    수익률은 계열 안에서만 계산하므로 결과는 이미 안전하다. 다만 어긋남 자체가
    액면분할·병합 발생 신호라, 히스토리를 눈으로 읽을 때 혼란을 막기 위해 남긴다.
    """
    recorded = _signal_close(record)
    if recorded is None or sig_date not in df.index:
        return
    actual = _valid_price(df.loc[sig_date, "close"])
    if actual is None or recorded <= 0:
        return
    if abs(actual / recorded - 1) > _READJUST_TOLERANCE:
        logger.info(
            "시세 소급조정 감지 %s(%s): 기록 종가 %.0f vs 현재 계열 %.0f "
            "(액면분할·병합 추정 — 수익률은 조정계열 기준으로 계산됨)",
            record.get("name", record["ticker"]), record["ticker"], recorded, actual,
        )


def _perf_row(entry: dict, as_of: pd.Timestamp, df: "pd.DataFrame | None") -> "PerfRow | None":
    """신호 하나를 채택 전략(저항선 재이탈 청산)으로 평가한다.

    진입은 신호일(T) 다음 거래일 시가. 청산은 T 이후 첫 거래일부터 종가가 그날의
    저항선(resistance_60) 아래로 마감한 시점의 종가다(설계서 §6의 청산 규칙).
    진입일 시가에 사서 같은 날 종가에 청산되는 경우가 있는데, 규칙상 정상이다.
    """
    ticker = entry["ticker"]
    if df is None or df.empty or "resistance_60" not in df.columns or "close" not in df.columns:
        return None

    sig_date = pd.Timestamp(entry["date"])
    name = entry.get("name", ticker)
    after = df[df.index > sig_date]

    if after.empty:
        # 신호 당일 — 살 수 있는 시가가 아직 없다. 0% 수익으로 채우지 않는다.
        return PerfRow(date=entry["date"], ticker=ticker, name=name, entry=None,
                       current=None, return_pct=None, days_held=0, status=STATUS_PENDING)

    _check_readjusted(entry, df, sig_date)

    entry_date = after.index[0]
    first = after.iloc[0]
    entry_price = _valid_price(first.get("open"), first.get("close"))
    if entry_price is None:
        return None  # 거래정지 등으로 진입가를 정할 수 없음

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
        return PerfRow(
            date=entry["date"], ticker=ticker, name=name, entry=entry_price,
            current=exit_price, return_pct=(exit_price / entry_price - 1) * 100,
            days_held=(exit_date - entry_date).days, status=STATUS_CLOSED,
        )

    # 진행중 — 진입일 이후 마지막 유효 종가 기준
    valid = after["close"].dropna()
    current = _valid_price(valid.iloc[-1]) if not valid.empty else None
    if current is None:
        current = entry_price
    return PerfRow(
        date=entry["date"], ticker=ticker, name=name, entry=entry_price,
        current=current, return_pct=(current / entry_price - 1) * 100,
        days_held=(as_of - entry_date).days, status=STATUS_OPEN,
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

    청산된 신호만으로 승률·평균수익률·평균보유일을 계산한다(진행중·진입대기는
    개수만 센다). 청산된 신호가 하나도 없으면 None.
    """
    rows = _rows_in_window(as_of, ohlcv_lookup, window_days)
    closed = [r for r in rows if r.status == STATUS_CLOSED and r.return_pct is not None]
    n_open = sum(1 for r in rows if r.status == STATUS_OPEN)
    n_pending = sum(1 for r in rows if r.status == STATUS_PENDING)
    if not closed:
        return None

    wins = sum(1 for r in closed if r.return_pct > 0)
    return PerfSummary(
        n_closed=len(closed),
        n_open=n_open,
        n_pending=n_pending,
        win_rate=wins / len(closed) * 100,
        avg_return=sum(r.return_pct for r in closed) / len(closed),
        avg_days=sum(r.days_held for r in closed) / len(closed),
        window_days=window_days,
    )
