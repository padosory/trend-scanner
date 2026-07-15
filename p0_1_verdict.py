"""
P0-1 판정: 체결 시점 현실화(T종가 → T+1시가) 4개 시나리오 비교.

용도: trend_scanner 알파 검증의 '체결 착시' 학습/종료용 판정.
      실탄 배포가 아니라, "겉보기 엣지가 얼마나 체결 가정에 의존했나"를 눈으로 확인.

사용법:
    backtest/results/ 가 있는 위치에서 실행하거나 RESULTS_DIR 를 절대경로로 수정.
    python p0_1_verdict.py

의존성: pandas (이미 프로젝트에 있음)

주의: CSV의 return_pct 는 총수익(비용 미반영). P0-1 은 '체결 타이밍 착시'만
      학습용으로 보는 단계이므로, 여기 결과에 비용/슬리피지(P0-3)는 들어있지 않다.
"""

import glob
import os
import sys
import pandas as pd

# Windows 콘솔(cp949)에서 한글·이모지 출력 깨짐/크래시 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ─────────────────────────── CONFIG ───────────────────────────
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backtest", "results")   # CSV 폴더 (스크립트 위치 기준)
TOP_N = 20                                 # 상위 N건 비중(집중도/취약성)
# 실제 스키마 확정: signal_step2_rs_*.csv 는 아래 컬럼명을 씀.
RETURN_COL = "return_pct"                   # 이미 % 단위 (예: 0.29, -5.01)
DATE_COL = "entry_date"                     # 진입일 기준 연도 집계

RETURN_CANDIDATES = ["return_pct", "ret", "return", "returns", "pnl", "profit",
                     "trade_return", "net_return", "ret_net", "r"]
DATE_CANDIDATES = ["entry_date", "date", "buy_date", "sell_date",
                   "exit_date", "signal_date", "trade_date"]

# 시나리오 라벨 ↔ 파일명 조각
SCENARIOS = {
    "A (종가)":       "close",
    "B (T+1 시가)":   "next_open",
    "C (T+1 고가)":   "next_high",
    "D (T+1 VWAP)":   "next_vwap",
}
# ───────────────────────────────────────────────────────────────


def find_file(tag):
    # signal_step2_rs_{tag}_*.csv 매칭. close 가 next_close 등에 오염 안 되게
    # 정확히 _{tag}_ 경계 또는 _{tag}.csv 로 끝나는 것만.
    hits = glob.glob(os.path.join(RESULTS_DIR, f"*{tag}*.csv"))
    hits = [h for h in hits if f"_{tag}_" in os.path.basename(h)
            or os.path.basename(h).endswith(f"_{tag}.csv")]
    return sorted(hits)[-1] if hits else None


def pick_col(df, override, candidates):
    if override and override in df.columns:
        return override
    lower = {c.lower(): c for c in df.columns}
    if override and override.lower() in lower:
        return lower[override.lower()]
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def summarize(df):
    rcol = pick_col(df, RETURN_COL, RETURN_CANDIDATES)
    dcol = pick_col(df, DATE_COL, DATE_CANDIDATES)
    if rcol is None:
        return None, f"수익률 컬럼 못 찾음. 컬럼: {list(df.columns)}"

    r = pd.to_numeric(df[rcol], errors="coerce").dropna()
    # 스케일: 컬럼명에 'pct'가 있으면 이미 % 단위 → 그대로. 아니면 비율로 보고 ×100.
    scale = 1.0 if "pct" in rcol.lower() else (100.0 if r.abs().median() < 1 else 1.0)
    r_pct = r * scale

    n = len(r_pct)
    mean = r_pct.mean()
    median = r_pct.median()
    total = r_pct.sum()
    top = r_pct.nlargest(min(TOP_N, n))
    # 집중도(취약성)는 총수익>0 일 때만 의미. 총합<=0 이면 부호가 뒤집혀 해석불가 → None.
    top_share = (top.sum() / total * 100) if total > 0 else None

    # 연도별 평균 양수 여부
    per_year_ok, per_year = None, {}
    if dcol is not None:
        yrs = pd.to_datetime(df.loc[r.index, dcol], errors="coerce").dt.year
        g = r_pct.groupby(yrs).mean().dropna()
        per_year = {int(y): round(v, 3) for y, v in g.items()}
        per_year_ok = bool((g > 0).all()) if len(g) else None

    return {
        "n": n, "mean": mean, "median": median,
        "top_share": top_share, "per_year": per_year,
        "per_year_ok": per_year_ok, "rcol": rcol, "dcol": dcol,
    }, None


def main():
    if not os.path.isdir(RESULTS_DIR):
        sys.exit(f"[!] RESULTS_DIR 없음: {RESULTS_DIR} — 경로 수정 필요")

    rows, details = [], {}
    for label, tag in SCENARIOS.items():
        path = find_file(tag)
        if not path:
            rows.append((label, "—", "파일 없음", "", "", ""))
            continue
        df = pd.read_csv(path, encoding="utf-8-sig")  # BOM 제거
        s, err = summarize(df)
        if err:
            rows.append((label, os.path.basename(path), err, "", "", ""))
            continue
        details[label] = s
        py = ("전연도 양수 ✅" if s["per_year_ok"] else
              ("연도별 음수 있음 ❌" if s["per_year_ok"] is False else "날짜컬럼 없음"))
        share = (f"상위{TOP_N} {s['top_share']:.0f}%" if s["top_share"] is not None
                 else f"상위{TOP_N} N/A(총합≤0)")
        rows.append((
            label, f"n={s['n']}",
            f"{s['mean']:+.3f}%", f"{s['median']:+.3f}%",
            share, py,
        ))

    # 출력 표
    hdr = ("시나리오", "건수", "평균", "중간값", f"상위{TOP_N}비중", "연도")
    w = [max(len(str(x)) for x in col) for col in zip(hdr, *rows)] if rows else []
    line = "  ".join(h.ljust(wi) for h, wi in zip(hdr, w))
    print("\n" + line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(c).ljust(wi) for c, wi in zip(r, w)))

    # ── B안 수용 기준 판정 ──
    print("\n[ B안(T+1 시가) 수용 기준 ]")
    b = details.get("B (T+1 시가)")
    if not b:
        print("  판정 불가 — B 시나리오 결과 없음")
    else:
        c1 = b["mean"] > 0
        c2 = b["per_year_ok"]
        print(f"  ① 평균 > 0          : {'통과 ✅' if c1 else '실패 ❌'}  ({b['mean']:+.3f}%)")
        if c2 is None:
            print(f"  ② 전 연도 양수       : 판정불가(날짜컬럼 없음)")
        else:
            print(f"  ② 전 연도 양수       : {'통과 ✅' if c2 else '실패 ❌'}  {b['per_year']}")
        verdict = "통과 → 다음 관문(vs S&P500)" if (c1 and c2) else "실패 → 백테스트 아티팩트로 확정"
        print(f"\n  판정: {verdict}")

    # 참고: 착시 크기 (A→B 평균 감소폭)
    a, bb = details.get("A (종가)"), details.get("B (T+1 시가)")
    if a and bb:
        print(f"\n[ 체결 착시 크기 ] A(종가) {a['mean']:+.3f}% "
              f"→ B(시가) {bb['mean']:+.3f}%  "
              f"= {bb['mean']-a['mean']:+.3f}%p")

    # P0-1 은 학습/종료용 판정임을 명시 (README 방향과 일치)
    print("\n※ trend_scanner 는 모니터링 도구로 확정. 위 판정은 '체결 착시' 학습용이며,")
    print("  실탄 승격은 이 판정만으로 불가 — README '승격 조건' 최종 관문(vs S&P500) 참조.")


if __name__ == "__main__":
    main()
