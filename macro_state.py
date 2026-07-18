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
