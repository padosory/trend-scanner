"""데이터 신선도 판정 회귀 테스트.

    python test_freshness.py     # 종료 코드 0=통과, 1=실패

2026-08-06·08-07·08-26 세 거래일이 조용히 유실된 적이 있다. 실행은 전부 성공으로
끝났고, 리포트와 텔레그램은 전날 것을 최신인 양 다시 내보냈다. 원인은 두 가지가
겹친 것이었다.

  1. 크론이 개장(09:00 KST) 이후로 밀리면 refresh_latest()의 앵커가 당일 미완성
     봉을 '최신 거래일'로 잡아, 캐시 갱신 대상이 한 종목도 안 남는다.
  2. fetch_ohlcv()가 신선도를 '요청 종료일 - 캐시 마지막일 <= 슬랙(2일)'로 재서,
     하루 뒤처진 캐시를 최신으로 통과시킨다. 휴장 때문인지 갱신 실패 때문인지
     구분할 수 없는 판정이었다.

pytest 없이 표준 라이브러리만으로 돈다(러너에 추가 의존성을 넣지 않기 위해).
네트워크도 쓰지 않는다 — pykrx 호출은 전부 대역으로 바꿔 끼운다.
"""

import sys

import pandas as pd

from backtest import data_cache

_REAL_PYKRX = data_cache._pykrx_ohlcv
_REAL_NOW = data_cache._now_kst
_REAL_LAST_TD = data_cache.get_last_trading_day

_failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: got={got} want={want}")
    if not ok:
        _failures.append(name)


def _cache_ending(last_day: str) -> pd.DataFrame:
    """마지막 날짜가 last_day인 더미 캐시."""
    idx = pd.bdate_range(end=pd.Timestamp(last_day), periods=5)
    return pd.DataFrame({"close": range(5)}, index=idx)


def _reset_anchor() -> None:
    data_cache._anchor_cache = None
    data_cache._anchor_resolved = False


def test_freshness_uses_real_trading_day() -> None:
    """캐시가 하루 뒤처졌으면 '최신'으로 통과시키면 안 된다 (8/26 유실 재현)."""
    print("\n[1] 신선도 판정 — 실제 거래일 기준")
    data_cache._now_kst = lambda: pd.Timestamp("2026-08-27 09:58")
    data_cache.get_last_trading_day = lambda: pd.Timestamp("2026-08-26")

    check("캐시 8/25 · 요청 8/26 (유실 재현)",
          data_cache._cache_is_fresh(_cache_ending("2026-08-25"), pd.Timestamp("2026-08-26")),
          False)
    check("캐시 8/26 · 요청 8/26 (정상)",
          data_cache._cache_is_fresh(_cache_ending("2026-08-26"), pd.Timestamp("2026-08-26")),
          True)
    # 한 번 밀리면 다음 날도 계속 잡아야 한다. 구 로직은 여기서도 통과시켜서,
    # 격차가 주말로 3일 이상 벌어질 때까지 낡은 상태가 자가증식했다.
    check("캐시 8/25 · 요청 8/27 (자가증식 재현)",
          data_cache._cache_is_fresh(_cache_ending("2026-08-25"), pd.Timestamp("2026-08-27")),
          False)

    # 오탐 방지: 요청일이 휴장일이어도 마지막 거래일까지 차 있으면 최신이다.
    # (2026-08-17은 광복절 대체공휴일)
    data_cache._now_kst = lambda: pd.Timestamp("2026-08-18 06:30")
    data_cache.get_last_trading_day = lambda: pd.Timestamp("2026-08-14")
    check("캐시 8/14 · 요청 8/17(대체공휴일) — 오탐 없어야",
          data_cache._cache_is_fresh(_cache_ending("2026-08-14"), pd.Timestamp("2026-08-17")),
          True)


def test_backtest_path_unchanged() -> None:
    """과거 구간 조회는 기존 슬랙 규칙을 그대로 써야 한다.

    여기에 거래일 기준을 적용하면 종료일이 휴장일일 때마다 전 종목 재조회가 터진다.
    """
    print("\n[2] 과거 구간 조회 — 기존 규칙 유지")
    calls: list[int] = []
    data_cache._now_kst = lambda: pd.Timestamp("2026-08-27 09:58")
    data_cache.get_last_trading_day = lambda: calls.append(1) or pd.Timestamp("2026-08-26")

    check("캐시 2025-12-30 · 요청 2025-12-31",
          data_cache._cache_is_fresh(_cache_ending("2025-12-30"), pd.Timestamp("2025-12-31")),
          True)
    check("과거 조회 시 앵커 호출 안 함", len(calls), 0)


def test_intraday_bar_excluded() -> None:
    """장중 실행이면 당일 미완성 봉을 거래일로 세면 안 된다."""
    print("\n[3] 장중 미완성 봉 제외")
    data_cache.get_last_trading_day = _REAL_LAST_TD
    bars = pd.DataFrame(
        {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
         "close": [100, 200, 300], "volume": [1, 1, 1]},
        index=pd.to_datetime(["2026-08-25", "2026-08-26", "2026-08-27"]),
    )
    data_cache._pykrx_ohlcv = lambda *a, **k: bars.copy()

    for label, now, want in (
        ("장중(09:58)", "2026-08-27 09:58", "2026-08-26"),
        ("마감후(16:30)", "2026-08-27 16:30", "2026-08-27"),
        ("익일 개장전(06:00)", "2026-08-28 06:00", "2026-08-27"),
    ):
        _reset_anchor()
        data_cache._now_kst = lambda now=now: pd.Timestamp(now)
        check(f"{label} 마지막 거래일", str(data_cache.get_last_trading_day().date()), want)


def test_anchor_failure_not_retried() -> None:
    """앵커 조회가 실패해도 종목마다 재시도하면 안 된다.

    _cache_is_fresh()는 스캔 중 종목 수만큼 호출된다. 실패를 기억하지 않으면
    KRX가 막혔을 때 2천 종목 × 타임아웃(20s)으로 잡이 통째로 죽는다.
    """
    print("\n[4] 앵커 조회 실패 — 1회만 시도")
    attempts: list[int] = []

    def _boom(*a, **k):
        attempts.append(1)
        raise TimeoutError("KRX 차단 재현")

    data_cache._pykrx_ohlcv = _boom
    data_cache.get_last_trading_day = _REAL_LAST_TD
    data_cache._now_kst = lambda: pd.Timestamp("2026-08-27 06:00")
    _reset_anchor()

    for _ in range(2000):
        data_cache._cache_is_fresh(_cache_ending("2026-08-25"), pd.Timestamp("2026-08-26"))
    check("앵커 조회 시도 횟수", len(attempts), 1)
    check("판정 불가 시 기존 슬랙 규칙으로 폴백",
          data_cache._cache_is_fresh(_cache_ending("2026-08-25"), pd.Timestamp("2026-08-26")),
          True)


def main() -> int:
    try:
        test_freshness_uses_real_trading_day()
        test_backtest_path_unchanged()
        test_intraday_bar_excluded()
        test_anchor_failure_not_retried()
    finally:
        data_cache._pykrx_ohlcv = _REAL_PYKRX
        data_cache._now_kst = _REAL_NOW
        data_cache.get_last_trading_day = _REAL_LAST_TD
        _reset_anchor()

    print("\n" + "=" * 60)
    if _failures:
        print(f"실패 {len(_failures)}건: {_failures}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
