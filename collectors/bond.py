"""한국 국고채 금리 수집 (한국은행 ECOS OpenAPI).

한국 채권시장 벤치마크인 국고채 3년물 일별 금리를 가져온다.
(미국은 10년물이 벤치마크지만 한국은 3년물이 대표 지표.)

필요 환경변수:
    ECOS_API_KEY  — 한국은행 ECOS 무료 API 키 (ecos.bok.or.kr)
"""

import logging
import os

logger = logging.getLogger(__name__)

_STAT_CODE = "817Y002"    # 시장금리(일별)
_ITEM_CODE = "010200000"  # 국고채(3년)
_CYCLE = "D"


def fetch_kr3y(start: str, end: str) -> "tuple[float, float, list[float]]":
    """국고채 3년 금리 (현재값, 전일대비 변화율%, 최근 시계열)를 반환한다.

    Args:
        start, end: YYYYMMDD 문자열

    Returns:
        실패/키 없음 시 (nan, nan, [])
    """
    import requests

    key = os.environ.get("ECOS_API_KEY", "")
    if not key:
        logger.warning("ECOS_API_KEY 없음 — 국고채 스킵")
        return float("nan"), float("nan"), []

    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/30/"
        f"{_STAT_CODE}/{_CYCLE}/{start}/{end}/{_ITEM_CODE}"
    )
    try:
        data = requests.get(url, timeout=10).json()
        rows = data["StatisticSearch"]["row"]  # 데이터 없으면 KeyError → except
        vals = [
            float(r["DATA_VALUE"])
            for r in rows
            if r.get("DATA_VALUE") not in (None, "")
        ]
        if len(vals) < 2:
            return (vals[-1] if vals else float("nan")), float("nan"), vals
        chg = (vals[-1] / vals[-2] - 1) * 100
        return vals[-1], chg, vals[-15:]
    except Exception as exc:  # noqa: BLE001
        logger.warning("국고채 조회 실패: %s", exc)
        return float("nan"), float("nan"), []
