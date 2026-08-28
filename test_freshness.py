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


def test_after_close_run_repairs_cache() -> None:
    """요청 종료일이 마지막 거래일보다 앞서도 신선도를 엄격히 봐야 한다.

    2026-08-28 17:28 수동 실행(run #60) 재현. 타겟이 8/27인데 마지막 거래일은
    8/28이라 `end_ts >= last_td`가 거짓이 되고, 구 로직은 엄격 판정을 건너뛰고
    슬랙 규칙(8/27 - 8/25 = 2일 <= 2)으로 떨어져 3일 밀린 캐시를 통과시켰다.
    그래서 재조회가 아예 안 일어났고 리포트가 8/25에 머물렀다.
    """
    print()
    print("[5] 장 마감 후 실행 — 요청 종료일 < 마지막 거래일")
    bars = pd.DataFrame(
        {"open": [1, 2, 3, 4], "high": [1, 2, 3, 4], "low": [1, 2, 3, 4],
         "close": [100, 200, 300, 400], "volume": [1, 1, 1, 1]},
        index=pd.to_datetime(["2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]),
    )
    data_cache._pykrx_ohlcv = lambda *a, **k: bars.copy()
    data_cache.get_last_trading_day = _REAL_LAST_TD
    data_cache._now_kst = lambda: pd.Timestamp("2026-08-28 17:28")
    _reset_anchor()

    check("마지막 거래일(마감 후라 당일 포함)",
          str(data_cache.get_last_trading_day().date()), "2026-08-28")
    check("캐시 8/25 · 요청 8/27 (run #60 재현) — 재조회해야",
          data_cache._cache_is_fresh(_cache_ending("2026-08-25"), pd.Timestamp("2026-08-27")),
          False)
    check("캐시 8/27 · 요청 8/27 — 재조회 불필요",
          data_cache._cache_is_fresh(_cache_ending("2026-08-27"), pd.Timestamp("2026-08-27")),
          True)


def test_target_date_follows_trading_day() -> None:
    """스캔 타겟은 달력상 '어제'가 아니라 마지막으로 완결된 거래일이어야 한다."""
    print()
    print("[6] 스캔 타겟 = 마지막 완결 거래일")
    from run_daily_report import _resolve_target_date

    fri = pd.Timestamp("2026-08-28")
    check("장 마감 후(금 17:28) — 오늘 장을 반영",
          _resolve_target_date(None, fri, pd.Timestamp("2026-08-28 17:28")), "20260828")
    check("개장 전(월 05:13) — 직전 거래일",
          _resolve_target_date(None, fri, pd.Timestamp("2026-08-31 05:13")), "20260828")
    check("거래일 판정 불가 — 어제로 폴백",
          _resolve_target_date(None, None, pd.Timestamp("2026-08-31 05:13")), "20260830")
    check("--date 명시가 최우선",
          _resolve_target_date("20260101", fri, pd.Timestamp("2026-08-31 05:13")), "20260101")


def test_readjustment_detected() -> None:
    """겹치는 구간의 종가가 어긋나면 소급조정으로 판정해야 한다."""
    print()
    print("[7] 소급조정 감지 — 겹침 구간 비교")
    idx = pd.bdate_range("2026-07-24", periods=5)
    old = pd.DataFrame({"close": [894.0] * 5}, index=idx)
    same = pd.DataFrame({"close": [894.0] * 5}, index=idx)
    new5 = pd.DataFrame({"close": [4470.0] * 5}, index=idx)          # 5:1 감자
    half = pd.DataFrame({"close": [447.0] * 5}, index=idx)           # 2:1 분할
    apart = pd.DataFrame({"close": [900.0] * 5}, index=pd.bdate_range("2026-08-10", periods=5))
    halted = pd.DataFrame({"close": [0.0] * 5}, index=idx)

    check("같은 계열 — 조정 아님", data_cache._series_readjusted(old, same), False)
    check("5배 차이 (금호전기 재현) — 조정", data_cache._series_readjusted(old, new5), True)
    check("0.5배 차이 (티앤엘 재현) — 조정", data_cache._series_readjusted(old, half), True)
    check("겹치는 구간 없음 — 판정 보류", data_cache._series_readjusted(old, apart), False)
    check("겹침이 전부 0값(거래정지) — 판정 보류",
          data_cache._series_readjusted(old, halted), False)


def test_snapshot_adjust_guard() -> None:
    """FDR 스냅샷 1행은 하루 변동으로 설명되는 값일 때만 붙여야 한다."""
    print()
    print("[8] 스냅샷 갱신 가드 — 가격제한폭(±30%) 기준")
    f = data_cache._is_plausible_daily_move
    check("상한가 +30%", f(1000, 1300), True)
    check("하한가 -30%", f(1000, 700), True)
    check("2:1 액면분할 (0.5배)", f(1000, 500), False)
    check("5:1 감자 (5배)", f(894, 4470), False)
    check("전일 종가 0 — 판정 불가 시 관용", f(0, 4470), True)


def test_fetch_refetches_on_readjustment() -> None:
    """소급조정이 감지되면 그 종목만 전체 재조회해 계열을 통일해야 한다.

    증분 병합만 하면 조정 전 행이 남아 계열에 불연속이 생긴다 — 배포된 리포트에서
    금호전기가 2026-07-30 894 → 07-31 4,600으로 4.6배 튀어 있었다.
    """
    print()
    print("[9] 소급조정 시 전체 재조회")
    import tempfile, pathlib
    real_dir = data_cache.CACHE_DIR
    tmp = pathlib.Path(tempfile.mkdtemp())
    cols = ["open", "high", "low", "close", "volume"]

    def frame(index, price):
        return pd.DataFrame({c: [float(price)] * len(index) for c in cols}, index=index)

    try:
        data_cache.CACHE_DIR = tmp
        data_cache._now_kst = lambda: pd.Timestamp("2026-08-28 17:28")
        data_cache.get_last_trading_day = lambda: pd.Timestamp("2026-08-28")

        cached_idx = pd.bdate_range("2026-03-02", "2026-07-30")
        frame(cached_idx, 894).to_parquet(tmp / "001210.parquet")

        truth = frame(pd.bdate_range("2026-03-02", "2026-08-28"), 4470)
        calls = []

        def fake(ticker, start, end):
            calls.append((start, end))
            return truth.loc[pd.Timestamp(start):pd.Timestamp(end)].copy()

        data_cache._pykrx_ohlcv = fake
        out = data_cache.fetch_ohlcv("001210", "20260302", "20260828")

        check("pykrx 호출 횟수 (증분 + 전체)", len(calls), 2)
        check("증분 조회가 캐시 마지막일보다 앞에서 시작", calls[0][0] <= "20260730", True)
        check("반환 계열에 조정 전 값이 남지 않음", bool((out["close"] == 894).any()), False)
        check("반환 계열 마지막 종가", float(out["close"].iloc[-1]), 4470.0)
        saved = pd.read_parquet(tmp / "001210.parquet")
        check("캐시도 새 계열로 덮어씀", bool((saved["close"] == 894).any()), False)

        # 대조군 — 조정이 없으면 전체 재조회 없이 증분 병합
        frame(cached_idx, 4470).to_parquet(tmp / "005930.parquet")
        calls.clear()
        out2 = data_cache.fetch_ohlcv("005930", "20260302", "20260828")
        check("조정 없으면 호출 1회(증분만)", len(calls), 1)
        check("증분 병합 후 마지막 종가", float(out2["close"].iloc[-1]), 4470.0)
    finally:
        data_cache.CACHE_DIR = real_dir


def test_existing_break_repaired() -> None:
    """캐시에 이미 박힌 불연속은 계열 전체를 훑어 한 번만 정리해야 한다.

    겹침 검증은 단절이 최근 며칠 안에 있을 때만 걸린다. 실제로 문제가 된 셋은
    전부 창 밖이었다(금호전기 07-31, 조아제약 08-26, 티앤엘 08-06).
    """
    print()
    print("[10] 캐시 계열 불연속 — 감지와 1회 정리")
    import tempfile, pathlib
    cols = ["open", "high", "low", "close", "volume"]

    def frame(index, prices):
        return pd.DataFrame({c: [float(x) for x in prices] for c in cols}, index=index)

    idx6 = pd.bdate_range("2026-07-24", periods=6)
    check("연속 계열 — 단절 없음",
          data_cache._series_has_break(frame(idx6, [1000, 1100, 1050, 1000, 980, 1010])), False)
    check("상한가 연달아(+30%) — 오탐 없어야",
          data_cache._series_has_break(frame(idx6, [1000, 1300, 1690, 2197, 2856, 3712])), False)
    check("5배 단절 (금호전기 재현)",
          data_cache._series_has_break(frame(idx6, [894, 894, 894, 4470, 4115, 3710])), True)
    check("0.5배 단절 (티앤엘 재현)",
          data_cache._series_has_break(frame(idx6, [65600, 64000, 32200, 31500, 32000, 31800])), True)
    check("거래정지 0값 섞임 — 오탐 없어야",
          data_cache._series_has_break(frame(idx6, [894, 0, 0, 894, 900, 890])), False)

    real_dir = data_cache.CACHE_DIR
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        data_cache.CACHE_DIR = tmp
        data_cache._now_kst = lambda: pd.Timestamp("2026-08-28 17:28")
        data_cache.get_last_trading_day = lambda: pd.Timestamp("2026-08-28")
        data_cache._refetched.clear()

        idx = pd.bdate_range("2026-03-02", "2026-08-28")
        broken = [894.0] * 100 + [4470.0] * (len(idx) - 100)   # 중간에 5배 단절
        frame(idx, broken).to_parquet(tmp / "001210.parquet")

        calls = []

        def fake(ticker, start, end):
            calls.append((start, end))
            return frame(idx, broken).loc[pd.Timestamp(start):pd.Timestamp(end)].copy()

        data_cache._pykrx_ohlcv = fake
        data_cache.fetch_ohlcv("001210", "20260302", "20260828")
        check("불연속 캐시 → 전체 재조회 1회", len(calls), 1)
        check("재조회 이력에 기록", "001210" in data_cache._refetched, True)

        # 재조회 후에도 단절이 남는 계열이라도 같은 프로세스에서 다시 받지 않는다
        calls.clear()
        data_cache.fetch_ohlcv("001210", "20260302", "20260828")
        check("같은 프로세스에서 재시도 안 함", len(calls), 0)
    finally:
        data_cache.CACHE_DIR = real_dir
        data_cache._refetched.clear()


def main() -> int:
    try:
        test_freshness_uses_real_trading_day()
        test_backtest_path_unchanged()
        test_intraday_bar_excluded()
        test_anchor_failure_not_retried()
        test_after_close_run_repairs_cache()
        test_target_date_follows_trading_day()
        test_readjustment_detected()
        test_snapshot_adjust_guard()
        test_fetch_refetches_on_readjustment()
        test_existing_break_repaired()
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
