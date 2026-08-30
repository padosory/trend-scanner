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

import numpy as np
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
STATUS_NOENTRY = "미진입"     # T+1에 저항선을 끝내 회복하지 못해 매수스톱이 체결되지 않음

ENTRY_OPEN = "시가"           # T+1 시가가 이미 저항선 위 — 시가 체결
ENTRY_STOP = "저항선"         # 시가는 저항선 아래였으나 장중 회복 — 저항선 가격 체결

# 신호 카드의 '추격 주의' 기준. 신호일 종가가 돌파한 저항선보다 이 비율 이상 위면
# 다음날 진입 시 손절폭이 그만큼 벌어진다. 종가 기준 돌파폭 12% 초과 구간의
# 중간값이 -2.9%(20% 초과는 -5.2%)로 꺾이는 것을 확인했다(설계서 §7.11).
CHASE_WARN_PCT = 12.0
CHASE_ALERT_PCT = 20.0


@dataclass(frozen=True)
class ExitRule:
    """손절선을 어떻게 올릴지에 대한 규칙. 셋을 나란히 돌려 비교한다(설계서 §7.11).

    key/label 외에 파라미터는 하나만 갖는다:
      lag     — 손절선 = max(돌파저항선, N거래일 전 시점의 resistance_60). 러닝맥스.
      pivot_k — 손절선 = max(돌파저항선, 돌파된 확정 스윙고점 중 최고).
                스윙고점은 좌우 k봉보다 높은 고점이며 k봉 뒤에야 확정된다(룩어헤드 없음).
      둘 다 None → 현행(매일 갱신하는 resistance_60을 그대로 손절선으로 씀).
    """
    key: str
    label: str
    desc: str
    lag: "int | None" = None
    pivot_k: "int | None" = None


# 현행은 비교 연속성을 위해 남긴다. lag/pivot 파라미터는 각 계열의 최고점이 아니라
# 중간 근처를 골랐다 — 최고점(N=40, k=12)을 고르면 사후 선택이 된다.
RULE_CURRENT = ExitRule("current", "현행", "매일 갱신 — 사실상 어제 장중 고가")
RULE_LAG = ExitRule("lag10", "지연", "10거래일 전 저항선까지만 반영", lag=10)
RULE_PIVOT = ExitRule("pivot5", "계단식", "확정 스윙고점을 돌파하면 그 자리로 상향", pivot_k=5)
EXIT_RULES = (RULE_CURRENT, RULE_LAG, RULE_PIVOT)

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
    status: str                # 진입대기 / 진행중 / 청산 / 미진입
    entry_kind: str = ""       # 시가 / 저항선 (역지정가가 어디서 체결됐나). 미진입이면 ""
    stop_dist_pct: "float | None" = None  # (진입가 - 돌파저항선)/진입가 %. 초기 손절폭

    @property
    def held_label(self) -> str:
        """보유기간 표시. 진입 당일에 청산된 건은 '0일'보다 '당일'이 정확하다."""
        if self.status in (STATUS_PENDING, STATUS_NOENTRY):
            return "—"
        return "당일" if self.days_held == 0 else f"{self.days_held}일"

    @property
    def chase_level(self) -> str:
        """초기 손절폭에 따른 추격 위험도. '' / 'warn' / 'alert'."""
        d = self.stop_dist_pct
        if d is None:
            return ""
        if d >= CHASE_ALERT_PCT:
            return "alert"
        return "warn" if d >= CHASE_WARN_PCT else ""


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
    # ── 아래는 규칙 비교·착시 방지용 ──
    rule_key: str = RULE_CURRENT.key
    rule_label: str = RULE_CURRENT.label
    rule_desc: str = RULE_CURRENT.desc
    n_noentry: int = 0            # 저항선을 회복 못 해 매수스톱이 안 걸린 신호 수
    median_return: float = 0.0    # 청산 신호 수익률 중간값 %
    top3_share: "float | None" = None  # 상위 3건이 전체 수익합에서 차지하는 비중 %
    # ── 규칙 간 공정 비교용 (청산 + 진행중, 미실현 포함) ──
    n_all: int = 0                # 진입이 성립한 전체 신호 수
    avg_all: "float | None" = None     # 전체 평균 수익률 %(진행중은 미실현)
    median_all: "float | None" = None  # 전체 중간값 %
    avg_days_all: "float | None" = None  # 전체 평균 보유일(진행중은 경과일)

    @property
    def tail_warning(self) -> bool:
        """상위 3건이 전체 수익의 100%를 넘으면(= 그 3건 빼면 순손실) 표시."""
        return self.top3_share is not None and self.top3_share > 100

    @property
    def closed_biased(self) -> bool:
        """청산분만 보면 규칙 간 비교가 불공정한 상태인지.

        느슨한 손절 규칙은 손절선이 진입가 위로 올라가야 '이익 청산'이 나오는데
        그러려면 시간이 걸린다. 그래서 추적 초기에는 **빨리 죽은 손실만 청산으로
        잡히고 이기는 건은 진행중에 남아** 청산분 성적이 실제보다 나쁘게 보인다.
        진행중 비중이 3할을 넘으면 청산분 비교를 신뢰하지 말라는 뜻으로 쓴다.
        """
        return self.n_all > 0 and self.n_open / self.n_all > 0.3


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


def _pivot_levels(df: pd.DataFrame, k: int) -> "list[tuple[int, float]]":
    """확정 스윙 고점 목록 [(확정 위치, 고점가), ...].

    좌우 k봉보다 높은 고점만 스윙 고점으로 인정한다. 그 판정은 k봉이 더 지나야
    가능하므로 확정 위치를 j+k로 둔다 — 미래 데이터를 앞당겨 쓰지 않는다.
    """
    high = df["high"].to_numpy(dtype=float)
    n = len(high)
    win = pd.Series(high).rolling(2 * k + 1, center=True).max().to_numpy()
    return [
        (j + k, float(high[j]))
        for j in range(n)
        if j + k < n and not np.isnan(win[j]) and high[j] == win[j]
    ]


def _find_exit(
    df: pd.DataFrame, entry_i: int, res_t: float, rule: ExitRule
) -> "tuple[pd.Timestamp, float] | None":
    """손절선을 규칙대로 갱신하며 청산 시점을 찾는다. (청산일, 청산가) 또는 None.

    청산가는 **조건이 성립한 다음 거래일 시가**다. 조건이 성립한 그 종가로 팔면
    '그날 종가를 미리 알고 판다'는 룩어헤드가 되어, 진입 쪽에서 이미 제거한
    편향을 청산 쪽에서 되살린다(검증결과 v3 §1.1). 진입과 같은 규약을 쓴다.
    """
    n = len(df)
    closes = df["close"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    res60 = df["resistance_60"].to_numpy(dtype=float)

    stop = float(res_t)
    pending = (
        [(cj, px) for cj, px in _pivot_levels(df, rule.pivot_k) if cj >= entry_i]
        if rule.pivot_k
        else []
    )
    pi, levels = 0, []

    for k in range(entry_i, n):
        if rule.lag is None and rule.pivot_k is None:
            stop = res60[k]                              # 현행: 매일 갱신값 그대로
        elif rule.lag is not None:
            j = k - rule.lag
            if j >= 0 and not np.isnan(res60[j]) and res60[j] > stop:
                stop = res60[j]                          # 지연: N일 전 값까지, 상향만
        else:
            while pi < len(pending) and pending[pi][0] <= k:
                levels.append(pending[pi][1])
                pi += 1
            c = closes[k]
            if levels and not np.isnan(c):
                broken = [lv for lv in levels if lv < c]  # 돌파된 = 지지로 전환된 고점
                if broken and max(broken) > stop:
                    stop = max(broken)

        c = closes[k]
        if np.isnan(c) or stop is None or np.isnan(stop):
            continue
        if c < stop:
            if k + 1 >= n:
                return None      # 청산 신호는 났으나 체결할 다음 거래일이 아직 없다
            px = _valid_price(opens[k + 1], closes[k + 1])
            if px is None:
                continue         # 거래정지 — 다음 거래일에 다시 시도
            return df.index[k + 1], px
    return None


def _perf_row(
    entry: dict, as_of: pd.Timestamp, df: "pd.DataFrame | None",
    rule: ExitRule = RULE_CURRENT,
) -> "PerfRow | None":
    """신호 하나를 주어진 청산 규칙으로 평가한다.

    진입은 **돌파당한 저항선 위에 매수스톱을 건 것**으로 본다(설계서 §7.9).
      - T+1 시가가 저항선 위        → 시가 체결
      - 시가는 아래지만 장중 회복   → 저항선 가격 체결
      - 끝내 회복 못 함             → 미진입
    돌파 전략인데 저항선 아래에서 시장가로 사는 것은 진입하자마자 청산 조건을
    충족한 상태가 되어 규칙과 모순된다. 전체의 5.8%가 그런 경우였다.

    청산은 종가가 손절선 아래로 마감한 **다음 거래일 시가**다.
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

    # 돌파당한 저항선 = 신호일 T 시점의 resistance_60 (T의 고가는 들어있지 않다).
    # 체결일(T+1)의 값을 쓰면 T의 고가가 이미 반영돼 전혀 다른 선이 된다.
    res_t = df["resistance_60"].asof(sig_date)
    entry_date = after.index[0]
    first = after.iloc[0]
    open_px = _valid_price(first.get("open"), first.get("close"))
    high_px = _valid_price(first.get("high"))
    if open_px is None:
        return None  # 거래정지 등으로 진입가를 정할 수 없음

    no_resist = res_t is None or pd.isna(res_t) or res_t <= 0
    if no_resist:
        # 저항선을 알 수 없으면 체결 여부를 판정할 수 없다 — 기존처럼 시가 진입.
        entry_price, entry_kind, stop_dist = open_px, ENTRY_OPEN, None
    elif open_px >= res_t:
        entry_price, entry_kind = open_px, ENTRY_OPEN
        stop_dist = (open_px - float(res_t)) / open_px * 100
    elif high_px is not None and high_px >= res_t:
        entry_price, entry_kind, stop_dist = float(res_t), ENTRY_STOP, 0.0
    else:
        return PerfRow(date=entry["date"], ticker=ticker, name=name, entry=None,
                       current=None, return_pct=None, days_held=0,
                       status=STATUS_NOENTRY)

    entry_i = df.index.get_loc(entry_date)
    hit = _find_exit(df, entry_i, entry_price if no_resist else float(res_t), rule)

    if hit is not None:
        exit_date, exit_price = hit
        return PerfRow(
            date=entry["date"], ticker=ticker, name=name, entry=entry_price,
            current=exit_price, return_pct=(exit_price / entry_price - 1) * 100,
            days_held=(exit_date - entry_date).days, status=STATUS_CLOSED,
            entry_kind=entry_kind, stop_dist_pct=stop_dist,
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
        entry_kind=entry_kind, stop_dist_pct=stop_dist,
    )


def _rows_in_window(
    as_of: pd.Timestamp, ohlcv_lookup: OhlcvLookup, window_days: int,
    rule: ExitRule = RULE_CURRENT,
) -> "list[PerfRow]":
    """window_days 이내 신호를 평가해 PerfRow 목록으로 반환 (내부 공용)."""
    history = load()
    cutoff = as_of - pd.Timedelta(days=window_days)

    rows: list[PerfRow] = []
    for e in history:
        sig_date = pd.Timestamp(e["date"])
        if sig_date < cutoff or sig_date > as_of:
            continue
        row = _perf_row(e, as_of, ohlcv_lookup(e["ticker"]), rule)
        if row is not None:
            rows.append(row)
    return rows


def evaluate(
    as_of: pd.Timestamp, ohlcv_lookup: OhlcvLookup, lookback_days: int = 30,
    rule: ExitRule = RULE_CURRENT,
) -> "list[PerfRow]":
    """lookback_days 이내 신호를 주어진 청산 규칙으로 평가한다.

    Args:
        as_of: 평가 기준일 (보통 스캔 거래일)
        ohlcv_lookup: ticker -> 지표 적용 OHLCV(DataFrame, 실패 시 None) 콜러블
        lookback_days: 추적할 신호의 최대 경과 일수
        rule: 청산 규칙. 표 본문은 비교 연속성을 위해 현행을 쓴다.

    Returns:
        최근 신호 우선 정렬된 PerfRow 목록
    """
    rows = _rows_in_window(as_of, ohlcv_lookup, lookback_days, rule)
    rows.sort(key=lambda r: r.date, reverse=True)
    return rows


def summarize(
    as_of: pd.Timestamp, ohlcv_lookup: OhlcvLookup, window_days: int = 90,
    rule: ExitRule = RULE_CURRENT,
) -> "PerfSummary | None":
    """window_days 이내 신호의 청산 성과를 집계한다.

    청산된 신호만으로 승률·평균·중간값·보유일을 계산한다(진행중·진입대기·미진입은
    개수만 센다). 청산된 신호가 하나도 없으면 None.

    평균과 함께 **중간값**을 내는 이유: 평균은 소수 대박에 끌려 올라가므로,
    평균만 보면 '전형적인 거래'가 손실인데도 좋아 보인다. 이 계열 전략에서
    반복 확인된 착시라 요약에 항상 같이 띄운다(검증결과 v3 §5).
    """
    rows = _rows_in_window(as_of, ohlcv_lookup, window_days, rule)
    closed = [r for r in rows if r.status == STATUS_CLOSED and r.return_pct is not None]
    n_open = sum(1 for r in rows if r.status == STATUS_OPEN)
    n_pending = sum(1 for r in rows if r.status == STATUS_PENDING)
    n_noentry = sum(1 for r in rows if r.status == STATUS_NOENTRY)
    if not closed:
        return None

    returns = sorted(r.return_pct for r in closed)
    wins = sum(1 for v in returns if v > 0)
    total = sum(returns)
    top3 = sum(returns[-3:])
    # 진행중까지 포함한 값. 청산분만 보면 느슨한 규칙이 구조적으로 불리해진다.
    # 보유일수도 같은 편향을 받는다 — 가장 오래 들고 있는 건이 아직 진행중이라
    # 청산분 평균만 보면 느슨한 규칙이 오히려 짧게 보인다.
    entered = [r for r in rows
               if r.status in (STATUS_CLOSED, STATUS_OPEN) and r.return_pct is not None]
    all_returns = [r.return_pct for r in entered]
    all_days = [r.days_held for r in entered]
    return PerfSummary(
        n_closed=len(closed),
        n_open=n_open,
        n_pending=n_pending,
        win_rate=wins / len(closed) * 100,
        avg_return=total / len(closed),
        avg_days=sum(r.days_held for r in closed) / len(closed),
        window_days=window_days,
        rule_key=rule.key,
        rule_label=rule.label,
        rule_desc=rule.desc,
        n_noentry=n_noentry,
        median_return=float(pd.Series(returns).median()),
        top3_share=(top3 / total * 100) if total else None,
        n_all=len(all_returns),
        avg_all=(sum(all_returns) / len(all_returns)) if all_returns else None,
        median_all=float(pd.Series(all_returns).median()) if all_returns else None,
        avg_days_all=(sum(all_days) / len(all_days)) if all_days else None,
    )


def summarize_rules(
    as_of: pd.Timestamp, ohlcv_lookup: OhlcvLookup, window_days: int = 90
) -> "list[PerfSummary]":
    """세 청산 규칙을 나란히 집계한다 (설계서 §7.11).

    현행 규칙은 손절선이 사실상 '어제 장중 고가'라 평균 보유가 1.5일로, 스윙
    목적의 시스템인데 구조적으로 스윙이 안 된다. 지연·계단식은 그 여유를 각각
    시간·구조로 주는 대안이다. 어느 쪽이 맞는지는 과거 데이터로 판정하지 못했고
    (에지가 6년 중 2년에만 있었다), 앞으로 쌓이는 실전 기록이 아웃오브샘플
    검증이 되므로 셋을 동시에 돌려 기록만 남긴다. 하나로 갈아치우지 않는다.
    """
    out = []
    for rule in EXIT_RULES:
        summary = summarize(as_of, ohlcv_lookup, window_days, rule)
        if summary is not None:
            out.append(summary)
    return out
