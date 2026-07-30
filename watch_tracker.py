"""돌파 준비 워치리스트의 스캔 간 전이(신규/유지/승격/이탈) 추적.

워치리스트는 매 스캔마다 무상태로 재계산되므로(collectors.stocks.scan) 그것만으로는
'며칠째 대기 중인지'도, '어제 후보가 어떻게 결말났는지'도 알 수 없다. 이 모듈이
data/watch_history.json 에 스캔 간 상태를 남겨 세 가지를 만든다.

  1. 연속 등재 스캔 수 — 갓 올라온 후보와 며칠째 고가를 못 뚫는 후보를 구분한다.
  2. 전일 대비 요약(신규/유지/승격/이탈) — 승격:이탈 비율 자체가 시장 폭 지표다.
  3. 승격 링크 — "워치 N일 → 돌파". 워치리스트의 예측력 검증용.

이탈 판정에는 히스테리시스를 둔다. 고가근접 경계(WATCH_PROXIMITY_LOW=90%)를
왕복하는 종목이 매일 '이탈'로 집계되면 카운터가 노이즈로 전락하기 때문이다.
반대로 '돌파했으나 신호 제외(RS 미달)'는 결말이 확정된 이탈이라 즉시 확정한다 —
이 경로는 신호도 워치리스트도 아닌 상태로 리포트에서 사라지던 사각지대였다.

저장 파일은 signal_history.json과 같이 워크플로가 매 실행 후 repo에 커밋한다.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

import config
from scanners import breakout_scanner

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).parent / "data" / "watch_history.json"

STALE_DAYS = 5         # 연속 등재가 이 스캔 수 이상이면 '장기 대기'로 표시
_MAX_TRANS_DAYS = 180  # 전이 로그 보관 일수 (파일 무한 성장 방지)

# ticker -> 지표(add_indicators) 적용 OHLCV DataFrame(또는 None) 콜러블.
# 이탈 사유 판정에 high_52w가 필요하므로 rolling(252)이 유효한 길이여야 한다
# (부족하면 워치리스트 등재 당시 저장한 high_52w로 폴백).
OhlcvLookup = Callable[[str], "pd.DataFrame | None"]

SHAPE_COIL = "수축"    # 변동폭이 좁아지며 거래량이 마르는 중 — 매물 소화(강세 셋업)
SHAPE_STALL = "정체"   # 변동폭·거래량이 줄지 않은 채 문턱에만 머무는 중

CAUSE_RS_FAIL = "돌파·RS미달"
CAUSE_BAND = "밴드 이탈"
CAUSE_LIQUIDITY = "유동성 탈락"
CAUSE_FILTER = "필터 탈락"
CAUSE_NO_DATA = "데이터 없음"

# 결말이 확정된 이탈 — 히스테리시스 없이 즉시 확정한다.
_RESOLVED_CAUSES = (CAUSE_RS_FAIL, CAUSE_LIQUIDITY)


@dataclass
class Promotion:
    """워치리스트 → 돌파 신호로 승격된 종목."""
    ticker: str
    name: str
    days_watched: int   # 승격 전까지 워치리스트에 머문 스캔 수


@dataclass
class Exited:
    """워치리스트에서 확정 이탈한 종목."""
    ticker: str
    name: str
    days_watched: int
    cause: str
    proximity_pct: float | None   # 이탈 시점 고가근접(%) — 판정 불가 시 None


@dataclass
class Shape:
    """체류 형태 — 같은 대기일수라도 수축과 정체는 의미가 정반대다."""
    label: str      # SHAPE_COIL | SHAPE_STALL
    detail: str     # 판정 근거(툴팁용)
    coiling: bool   # 수축 여부 — 장기 대기 경고를 끌지 결정한다


@dataclass
class WatchDelta:
    """오늘 스캔의 워치리스트 전이 결과."""
    days: dict[str, int]        # ticker -> 연속 등재 스캔 수 (오늘 워치리스트 종목)
    promoted: list[Promotion]
    exited: list[Exited]
    n_new: int                  # 오늘 새로 올라온 후보 수
    n_kept: int                 # 어제에 이어 유지된 후보 수
    shapes: dict[str, Shape] = field(default_factory=dict)  # ticker -> 체류 형태
    baseline: bool = False      # 첫 실행(비교 대상 없음) — 전부 '신규'로 보이는 오해 방지
    stale_days: int = STALE_DAYS

    @property
    def n_promoted(self) -> int:
        return len(self.promoted)

    @property
    def n_exited(self) -> int:
        return len(self.exited)

    @property
    def n_rs_fail(self) -> int:
        """이탈 중 '돌파는 했으나 RS 게이트 미달' 건수."""
        return sum(1 for e in self.exited if e.cause == CAUSE_RS_FAIL)

    @property
    def promoted_days(self) -> dict[str, int]:
        """ticker -> 승격 전 대기 스캔 수 (신호 카드 배지용)."""
        return {p.ticker: p.days_watched for p in self.promoted}

    @property
    def exit_label(self) -> str:
        """이탈 종목 요약 문자열 (툴팁용) — 목록 테이블은 만들지 않는다."""
        return " · ".join(f"{e.name}({e.cause})" for e in self.exited)


def load() -> dict:
    """상태 파일을 로드한다. 없거나 손상 시 빈 dict."""
    if not HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("워치 히스토리 로드 실패: %s", exc)
        return {}


def _save(state: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _exit_cause(
    ticker: str,
    as_of: pd.Timestamp,
    ohlcv_lookup: OhlcvLookup,
    prev_high_52w: float | None,
) -> tuple[str, "float | None"]:
    """워치리스트에서 빠진 종목이 '왜' 빠졌는지 판정한다.

    승격(신호 진입)은 호출 전에 이미 걸러졌으므로 여기서는 나머지 경로만 본다.
    """
    df = ohlcv_lookup(ticker)
    if df is None or df.empty or df.index[-1] != as_of:
        return CAUSE_NO_DATA, None   # 거래정지·상장폐지·데이터 지연

    row = df.iloc[-1]
    close = row.get("close", float("nan"))
    high = row.get("high_52w", float("nan"))
    if pd.isna(high) and prev_high_52w:
        high = prev_high_52w         # 조회 구간이 252거래일 미만이면 등재 당시 값으로 폴백
    if pd.isna(close) or pd.isna(high) or high <= 0:
        return CAUSE_NO_DATA, None

    prox = float(close / high * 100)

    # 유동성은 신호·워치리스트 공통 전제 필터라 돌파 판정보다 먼저 본다.
    avg_tv = row.get("avg_trading_value20", float("nan"))
    if pd.isna(avg_tv) or avg_tv < config.MIN_AVG_TRADING_VALUE:
        return CAUSE_LIQUIDITY, prox

    # 돌파를 통과했는데 신호가 아니다 = RS 게이트에서 탈락 (리포트에서 사라지던 경로)
    if breakout_scanner.passes(row):
        return CAUSE_RS_FAIL, prox

    if prox < config.WATCH_PROXIMITY_LOW * 100:
        return CAUSE_BAND, prox      # 고가권에서 밀려남

    return CAUSE_FILTER, prox        # 밴드 안이지만 후보에서 빠짐(시총 등)


def _mean(series: "pd.Series") -> float:
    """NaN을 제외한 평균. 유효 값이 없으면 0."""
    v = series.dropna()
    return float(v.mean()) if not v.empty else 0.0


def classify_shape(df: "pd.DataFrame | None", days: int) -> "Shape | None":
    """워치리스트 체류 구간의 형태(수축/정체)를 판정한다.

    경과일수는 그 자체로 방향을 말해주지 않는다. 오닐의 컵앤핸들이 수 주~수십 주,
    미너비니의 VCP가 '고가 근처에서 변동폭이 좁아지고 거래량이 마르는' 구간을 최상의
    셋업으로 보듯, 문턱에서의 긴 체류는 매물을 소화하는 강세 신호일 수도 있다.
    둘을 가르는 것은 시간이 아니라 그 기간의 변동폭·거래량 추이다.

      수축 — 후반 변동폭이 전반보다(또는 체류 직전 베이스라인보다) 좁아지고
             거래량도 함께 마른 상태. 매도 물량이 소진되는 중.
      정체 — 변동폭이 줄지 않은 채 문턱만 두드리는 상태. 매수세만 소모된다.

    Args:
        df: 지표 적용 OHLCV (마지막 행 = 스캔 거래일). 체류 구간을 tail로 자른다.
        days: 연속 등재 스캔 수

    Returns:
        Shape. 표본이 부족하거나(체류 < WATCH_SHAPE_MIN_DAYS) 데이터가 모자라면 None.
    """
    if df is None or df.empty:
        return None
    n = min(int(days), config.WATCH_SHAPE_MAX_DAYS)
    if n < config.WATCH_SHAPE_MIN_DAYS:
        return None
    if not {"high", "low", "close", "volume"} <= set(df.columns):
        return None

    win = df.tail(n)
    if len(win) < n:
        return None

    rng = (win["high"] - win["low"]) / win["close"]   # 일중 변동폭 비율
    vol = win["volume"]
    half = max(1, n // 2)
    late_rng, early_rng = _mean(rng.tail(half)), _mean(rng.head(n - half))
    late_vol, early_vol = _mean(vol.tail(half)), _mean(vol.head(n - half))
    if late_rng <= 0 or early_rng <= 0 or late_vol <= 0 or early_vol <= 0:
        return None

    # 체류 직전 구간 — 등재 전부터 이미 좁게 굳어 있던 베이스를 '수축 없음'으로
    # 오판하지 않기 위한 절대 기준.
    base = df.iloc[-(n + config.WATCH_SHAPE_BASELINE_DAYS):-n]
    base_rng = _mean((base["high"] - base["low"]) / base["close"]) if len(base) >= 5 else 0.0
    base_vol = _mean(base["volume"]) if len(base) >= 5 else 0.0

    contracting = late_rng < early_rng * config.WATCH_COIL_RANGE_RATIO
    tight = base_rng > 0 and late_rng < base_rng * config.WATCH_COIL_TIGHT_RATIO
    vol_drying = late_vol < early_vol * config.WATCH_COIL_VOL_RATIO or (
        base_vol > 0 and late_vol < base_vol * config.WATCH_COIL_VOL_RATIO
    )
    coiling = (contracting or tight) and vol_drying

    detail = (
        f"체류 {n}거래일 · 변동폭 {early_rng * 100:.1f}% → {late_rng * 100:.1f}% · "
        f"거래량 {late_vol / early_vol:.1f}배"
    )
    detail += (
        " — 변동폭이 좁아지고 거래량이 마르는 중(매물 소화)"
        if coiling
        else " — 변동폭·거래량이 줄지 않은 채 문턱에 머무는 중"
    )
    return Shape(SHAPE_COIL if coiling else SHAPE_STALL, detail, coiling)


def _compute_shapes(
    watch_map: dict, items: dict[str, dict], ohlcv_lookup: OhlcvLookup
) -> dict[str, Shape]:
    """오늘 워치리스트 종목의 체류 형태를 판정한다(짧은 체류는 생략)."""
    shapes: dict[str, Shape] = {}
    for ticker in watch_map:
        days = int(items.get(ticker, {}).get("days", 1))
        if days < config.WATCH_SHAPE_MIN_DAYS:
            continue
        try:
            shape = classify_shape(ohlcv_lookup(ticker), days)
        except Exception as exc:  # noqa: BLE001
            logger.debug("체류 형태 판정 실패 %s: %s", ticker, exc)
            continue
        if shape is not None:
            shapes[ticker] = shape
    return shapes


def _delta_from_record(
    rec: dict, days: dict[str, int], shapes: dict[str, Shape]
) -> WatchDelta:
    """저장된 전이 기록을 WatchDelta로 되살린다 (같은 거래일 재실행 경로)."""
    return WatchDelta(
        days=days,
        promoted=[Promotion(**p) for p in rec.get("promoted_items", [])],
        exited=[Exited(**e) for e in rec.get("exited_items", [])],
        n_new=int(rec.get("new", 0)),
        n_kept=int(rec.get("kept", 0)),
        shapes=shapes,
        baseline=bool(rec.get("baseline", False)),
    )


def update(
    watchlist: list,
    signals: list,
    scan_date: pd.Timestamp,
    name_map: dict[str, str],
    ohlcv_lookup: OhlcvLookup,
) -> WatchDelta:
    """워치리스트 상태를 갱신하고 오늘의 전이를 반환한다.

    Args:
        watchlist: 오늘 스캔의 list[WatchItem]
        signals: 오늘 스캔의 list[StockSignal] — 승격 판정에 사용
        scan_date: 스캔 거래일(effective_date)
        name_map: {ticker: 종목명}
        ohlcv_lookup: 이탈 사유 판정용 OHLCV 콜러블

    Returns:
        WatchDelta. 같은 거래일 재실행 시 카운트를 중복 증가시키지 않고
        기록된 전이를 그대로 돌려준다(멱등).
    """
    date_str = scan_date.strftime("%Y-%m-%d")
    state = load()
    items: dict[str, dict] = state.get("items", {})
    trans: dict[str, dict] = state.get("transitions", {})

    watch_map = {w.ticker: w for w in watchlist}

    # 같은 거래일 재실행 — 이미 기록된 전이를 재사용한다(형태는 OHLCV에서 재계산).
    if state.get("last_scan") == date_str and date_str in trans:
        days = {tk: int(items.get(tk, {}).get("days", 1)) for tk in watch_map}
        logger.info("워치 전이: %s 기록 재사용(재실행)", date_str)
        return _delta_from_record(
            trans[date_str], days, _compute_shapes(watch_map, items, ohlcv_lookup)
        )

    baseline = "last_scan" not in state
    signal_set = {s.ticker for s in signals}

    # ── 빠진 종목의 사유를 먼저 판정한다 (상태 변경 전) ──────────────────────
    dropped = [t for t in items if t not in watch_map and t not in signal_set]
    causes = {
        t: _exit_cause(t, scan_date, ohlcv_lookup, items[t].get("high_52w"))
        for t in dropped
    }

    # 빠진 종목 전부가 '데이터 없음'이면 시장이 아니라 수집이 무너진 날이다.
    # 그대로 진행하면 추적 중이던 종목이 통째로 이탈 처리되므로 상태를 보존한다.
    # (실제 급락장이라면 데이터는 살아 있고 '밴드 이탈'로 분류된다)
    if len(dropped) >= 3 and all(c == CAUSE_NO_DATA for c, _ in causes.values()):
        logger.warning(
            "워치 전이 스킵(%s): 추적 %d종목 전부 데이터 조회 실패 — 상태 보존",
            date_str, len(dropped),
        )
        return WatchDelta(
            days={tk: int(items.get(tk, {}).get("days", 1)) for tk in watch_map},
            promoted=[], exited=[], n_new=0, n_kept=0,
            shapes=_compute_shapes(watch_map, items, ohlcv_lookup),
        )

    # ── 오늘 워치리스트: 신규 등재 / 연속 등재일수 증가 ──────────────────────
    n_new = n_kept = 0
    for ticker, w in watch_map.items():
        item = items.get(ticker)
        if item is None:
            items[ticker] = {
                "first_seen": date_str,
                "last_seen": date_str,
                "days": 1,
                "reason": w.reason,
                "proximity_pct": round(w.proximity_pct, 1),
                "high_52w": round(float(w.high_52w), 2),
                "out_streak": 0,
            }
            n_new += 1
        else:
            # 히스테리시스로 살려둔(out_streak>0) 종목이 복귀하면 연속 등재가 이어진다.
            item["days"] = int(item.get("days", 1)) + 1
            item["last_seen"] = date_str
            item["reason"] = w.reason
            item["proximity_pct"] = round(w.proximity_pct, 1)
            item["high_52w"] = round(float(w.high_52w), 2)
            item["out_streak"] = 0
            n_kept += 1

    # ── 체류 형태(수축/정체) 판정 — 등재일수 갱신 후에 계산해야 구간이 맞는다 ──
    shapes = _compute_shapes(watch_map, items, ohlcv_lookup)
    for ticker in watch_map:
        shape = shapes.get(ticker)
        if shape is not None:
            items[ticker]["shape"] = shape.label
        else:
            items[ticker].pop("shape", None)

    # ── 어제 후보 중 오늘 빠진 것: 승격 / 이탈 확정 / 이탈 대기 ───────────────
    promoted: list[Promotion] = []
    exited: list[Exited] = []
    for ticker in list(items):
        if ticker in watch_map:
            continue
        item = items[ticker]
        days_watched = int(item.get("days", 1))
        name = name_map.get(ticker, ticker)

        if ticker in signal_set:
            promoted.append(Promotion(ticker, name, days_watched))
            del items[ticker]
            continue

        cause, prox = causes[ticker]
        streak = int(item.get("out_streak", 0)) + 1
        item["out_streak"] = streak

        # 결말 확정 사유는 즉시, 나머지는 경계선 진동을 걸러내고 확정한다.
        confirmed = (
            cause in _RESOLVED_CAUSES
            or streak >= config.WATCH_EXIT_GRACE_SCANS
            or (prox is not None and prox < config.WATCH_EXIT_PROXIMITY * 100)
        )
        if confirmed:
            exited.append(
                Exited(ticker, name, days_watched, cause,
                       round(prox, 1) if prox is not None else None)
            )
            del items[ticker]

    # ── 상태 저장 ────────────────────────────────────────────────────────────
    trans[date_str] = {
        "new": n_new,
        "kept": n_kept,
        "promoted": len(promoted),
        "exited": len(exited),
        "baseline": baseline,
        "promoted_items": [vars(p) for p in promoted],
        "exited_items": [vars(e) for e in exited],
    }
    for old in sorted(trans)[:-_MAX_TRANS_DAYS]:
        del trans[old]

    state["last_scan"] = date_str
    state["items"] = items
    state["transitions"] = trans
    _save(state)

    logger.info(
        "워치 전이(%s): 신규 %d · 유지 %d · 승격 %d · 이탈 %d(RS미달 %d) · 수축 %d/정체 %d%s",
        date_str, n_new, n_kept, len(promoted), len(exited),
        sum(1 for e in exited if e.cause == CAUSE_RS_FAIL),
        sum(1 for s in shapes.values() if s.coiling),
        sum(1 for s in shapes.values() if not s.coiling),
        " [첫 실행 — 기준선]" if baseline else "",
    )

    return WatchDelta(
        days={tk: int(items[tk]["days"]) for tk in watch_map if tk in items},
        promoted=promoted,
        exited=exited,
        n_new=n_new,
        n_kept=n_kept,
        shapes=shapes,
        baseline=baseline,
    )
