"""실행 상태 저장 — 같은 거래일을 두 번 처리하지 않기 위한 마커.

백업 스케줄이 '이미 끝난 날'을 다시 스캔해 리포트·텔레그램을 두 번 보내는 것을
막는다. 마커는 스캔 자신이 확정한 기준일(effective_date)이고, 리포트 생성과
알림이 전부 끝나고 신선도 검증까지 통과한 뒤에만 기록한다 — 중간에 죽은 실행은
마커를 남기지 않으므로 백업이 제 역할(복구 스캔)을 한다.

마커를 따로 두는 이유: 전에는 macro_state.json 의 date 를 중복방지 키로 겸용했다.
그 값은 FDR 코스피 응답의 마지막 행 날짜이고, 저장 자체가 CoinGecko 도미넌스
조회 성공에 걸려 있었다. 외부 피드 둘 중 하나만 멈춰도 마커가 과거에 얼어붙어
백업이 매일 전체 재스캔을 했다 — 실측: date 가 2026-09-17 에 2주간 정체(09-21~10-02),
이어서 10-02~10-13 은 파일이 아예 기록되지 않아, 그 기간 거의 모든 거래일에
리포트가 두 번 발송됐다. 중복방지 키는 외부 피드가 아니라 스캔 결과에 묶여야 한다.

data/ 는 GitHub Actions 에서 매일 커밋되어 다음 실행으로 이어진다.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).parent / "data" / "run_state.json"


def load() -> dict:
    """마지막 완료 상태를 로드한다. 없거나 손상 시 빈 dict.

    빈 dict 는 '처리된 날 없음'으로 읽혀 스캔을 진행시킨다 — 마커를 못 읽었을 때
    중복 리포트(안전)로 기울고 리포트 누락(위험)으로 기울지 않는 쪽이다.
    """
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_state 로드 실패: %s", exc)
        return {}


def is_done(trading_day) -> bool:
    """trading_day(pd.Timestamp)의 리포트가 이미 완료됐는지."""
    if trading_day is None:
        return False
    return load().get("last_report_date") == trading_day.strftime("%Y-%m-%d")


def mark_done(scan_date, completed_at: str = "") -> None:
    """scan_date(pd.Timestamp) 리포트 완료를 기록한다."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"last_report_date": scan_date.strftime("%Y-%m-%d")}
    if completed_at:
        snapshot["completed_at"] = completed_at
    STATE_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("run_state 기록: %s", snapshot)
