"""Gemini Flash 로 뉴스 요약 + 종목 코멘트를 한 번의 JSON 호출로 처리."""

import json
import logging
import os

logger = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"
# thinking을 끄지 않으면 thinking 토큰이 max_output_tokens를 다 소비해 텍스트가 None이 됨
_THINKING_CONFIG = {"thinking_budget": 0}

_SYSTEM = (
    "당신은 금융 데이터 분석가입니다. "
    "주어지는 JSON 양식에 맞춰 군더더기 없이 정확히 지정된 글자 수 내로만 답변하세요. "
    "생각이나 부연 설명은 생략합니다."
)

# 모듈 수준 싱글톤 — 함수 스코프에서 생성하면 AFC가 클라이언트를 닫아버리는 버그 회피
_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다")
        _client = genai.Client(api_key=api_key)
    return _client


def analyze(
    news_items,
    signals,
    name_map: dict[str, str],
) -> tuple[str, dict[str, str]]:
    """뉴스 요약 + 종목 코멘트를 단일 Gemini 호출로 반환.

    Returns:
        (news_summary, {ticker: comment})
    """
    from google.genai import types

    news_section = (
        "\n".join(f"- [{i.source}] {i.title}" for i in news_items)
        if news_items else "(수집된 뉴스 없음)"
    )
    signal_lines = [
        f"- {s.ticker}({name_map.get(s.ticker, s.ticker)}): "
        f"종가={s.close:,.0f}원 RS={s.rs_percentile:.0%} 60일수익률={s.return_60d:.1%}"
        for s in signals
    ]
    signal_section = "\n".join(signal_lines) if signal_lines else "(신호 없음)"

    full_prompt = (
        f"[뉴스 헤드라인]\n{news_section}\n\n"
        f"[STEP2+RS 신호 종목]\n{signal_section}\n\n"
        "[규칙]\n"
        "- news_summary: 뉴스 기반 시장 분위기 2~3문장\n"
        "- comments: 각 신호 종목의 30자 이내 투자 포인트 (신호 없으면 빈 배열)\n\n"
        "반드시 아래 JSON 형식만 출력하세요 (다른 텍스트 없이):\n"
        '{"news_summary": "...", "comments": [{"code": "종목코드", "comment": "..."}]}'
    )

    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                thinking_config=types.ThinkingConfig(**_THINKING_CONFIG),
                max_output_tokens=500,
                # AFC가 내부적으로 client를 닫는 버그 회피 — 명시적으로 비활성화
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        raw = (response.text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        data = json.loads(raw)
        news_summary = data.get("news_summary", "")
        comments = {
            item["code"]: item["comment"]
            for item in data.get("comments", [])
            if "code" in item and "comment" in item
        }
        logger.info("Gemini 분석 완료 (신호 %d개 코멘트)", len(comments))
        return news_summary, comments

    except Exception as exc:
        logger.warning("Gemini 분석 실패: %s", exc)
        return "AI 요약 생성 실패", {}
