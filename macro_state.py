"""전일 대비 계산이 필요한 지표의 상태 저장.

BTC 도미넌스처럼 무료 API가 '현재값'만 주는 지표는, 전일 스냅샷을 저장해
다음 실행에서 등락을 계산한다. data/ 는 GitHub Actions에서 매일 커밋되어 지속된다.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).parent / "data" / "macro_state.json"


def load() -> dict:
    """직전 스냅샷을 로드한다. 없거나 손상 시 빈 dict."""
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("macro_state 로드 실패: %s", exc)
        return {}


def save(snapshot: dict) -> None:
    """오늘 스냅샷을 저장한다."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update_dominance(current: float, scan_date: str) -> "float | None":
    """오늘 도미넌스를 기록하고 직전 스캔 대비 등락률(%)을 반환한다. 비교 대상이 없으면 None.

    날짜 키는 스캔 기준일(effective_date)이다. 전에는 FDR 코스피 응답의 마지막 행
    날짜를 썼는데, 그 피드가 멈추면 매일 '같은 날'로 판정돼 등락률이 공란이 됐다.

    같은 스캔일 재실행은 첫 실행의 비교 기준(base_dominance)을 유지한다. 기준을
    덮어쓰면 재실행 때 등락률이 사라지거나 재실행 간 차이로 바뀐다.
    """
    prev = load()
    if prev.get("date") == scan_date:
        base = prev.get("base_dominance")
    else:
        base = prev.get("btc_dominance")

    save({"date": scan_date, "btc_dominance": current, "base_dominance": base})
    return (current / base - 1) * 100 if base else None
