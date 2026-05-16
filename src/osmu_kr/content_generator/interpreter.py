"""interpreter — 0단계 keyword 정규화 (룰 + LLM 보강).

[ 위치 — 파이프라인 ]
  keyword_researcher → ★ interpreter ★ → collector → contents_maker → ...

[ 책임 ]
  사용자가 넘긴 raw 키워드 문자열을 KeywordContext 로 정규화한다.
  키워드 해석(domain / intent / topic_summary) 을 collector 진입 전에 끝낸다.

[ 두 모드 ]
  1) 룰 모드 (use_llm=False, default)
     · 외부 호출 0회. keyword_classifier + intent 사전 + rule_topic_summary 만 사용.
     · 비용·지연 최소. 0-API-key 환경에서도 동작.
  2) LLM 보강 모드 (use_llm=True 또는 OSMU_USE_LLM_INTERPRETER=1)
     · ANTHROPIC_API_KEY 가 있으면 Claude 한 번 호출해 domain / intent / topic_summary
       세 필드를 ‘덮어쓴다’. JSON 응답을 강제하고 파싱 실패는 즉시 룰 결과로 폴백.
     · 키 없거나 호출 실패 → 룰 결과 그대로 (source='llm_fallback_rule').

[ 보강 대상 — 사용자 정의 ]
  · 미등재 키워드(예: ‘스텔라 블레이드 빌드’)는 룰 분류기에서 ‘일반’ 으로 떨어진다.
  · 그래서 collector / Writer 에 도메인 정보가 도달하지 않고 글이 ‘일반 비즈니스
    가이드’ 로 새는 게 본 시스템의 큰 문제였다.
  · LLM 보강은 이 ‘분류기 사각지대’ 를 덮는 게 핵심이다.

[ 보강 비활성 시그널 ]
  · ANTHROPIC_API_KEY 미설정
  · OSMU_DISABLE_LLM_INTERPRETER=1
  → 둘 다 use_llm=True 라도 룰 결과만 반환 (source='llm_fallback_rule')
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import replace
from typing import Optional, Union

from .keyword_classifier import Domain
from .keyword_context import KeywordContext, rule_topic_summary

log = logging.getLogger(__name__)


# Domain 영문 코드 → 한글 라벨 (LLM 응답 정규화용)
_DOMAIN_TO_KO = {
    "game": "게임",
    "finance": "재테크/금융",
    "diet": "다이어트/건강",
    "it": "IT/디지털",
    "beauty": "뷰티/화장품",
    "travel": "여행",
    "food": "음식/요리",
    "general": "일반",
}
_VALID_INTENTS = {"공략", "추천", "비교", "리뷰", "구매", "방법", "순위", "팁", "정보"}


# ── LLM 프롬프트 ────────────────────────────────────────
_SYSTEM_PROMPT = """당신은 한국어 검색 키워드를 분석하는 전문가입니다.
주어진 키워드를 보고 다음 세 가지를 결정하세요.

1) domain — 키워드의 주제 도메인을 아래 8개 중 하나로 정확히 선택:
   game / finance / diet / it / beauty / travel / food / general
   · 비디오 게임(타이틀명, 게임 캐릭터, 빌드, 공략 등) → game
   · 주식·ETF·재테크·부동산·예적금·투자 상품 → finance
   · 식단·운동·체중 관리·건강 관리 → diet
   · 노트북·스마트폰·AI 도구·앱·SW 등 IT 제품/서비스 → it
   · 화장품·스킨케어·메이크업 → beauty
   · 여행지·호텔·관광 코스 → travel
   · 요리·레시피·음식점 → food
   · 위 어디에도 명확히 들지 않으면 → general

2) intent — 검색 의도를 아래 9개 중 하나:
   공략 / 추천 / 비교 / 리뷰 / 구매 / 방법 / 순위 / 팁 / 정보
   · 의도가 명시적으로 안 보이면 '정보'.

3) topic_summary — 한 줄(60자 이내)로 “이 키워드가 정확히 무엇인지” 설명.
   · 키워드 자체가 무엇을 가리키는지 + 독자가 무엇을 찾고 있는지를 간결하게.
   · 예: ‘비대칭 4vs1 호러 게임 데드바이데이라이트의 캐릭터·맵 공략을 찾는 키워드’

응답은 반드시 다음 형식의 ‘JSON 한 객체’ 만 출력하세요. 다른 설명/코드블록 금지.
{"domain": "...", "intent": "...", "topic_summary": "..."}
"""


def _post_anthropic(api_key: str, model: str, system: str, user: str,
                    *, max_tokens: int = 300, timeout: int = 30) -> str:
    """Anthropic Messages API 단일 호출 — JSON 텍스트만 추출."""
    try:
        import requests
    except ImportError as e:
        raise RuntimeError(f"requests 모듈 없음: {e}") from e

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    blocks = data.get("content", []) or []
    for blk in blocks:
        if blk.get("type") == "text":
            return blk.get("text", "")
    raise RuntimeError("Anthropic 응답에 text 블록이 없음")


_JSON_BLOCK = re.compile(r"\{.*?\}", re.DOTALL)


def _parse_llm_json(text: str) -> dict:
    """LLM 응답에서 JSON 객체 추출. ‘설명문 + JSON’ 같이 와도 견딘다."""
    if not text:
        raise ValueError("빈 응답")
    text = text.strip()
    # 우선 그대로 JSON 시도
    try:
        return json.loads(text)
    except Exception:
        pass
    # 그다음 첫 JSON 블록만
    m = _JSON_BLOCK.search(text)
    if not m:
        raise ValueError(f"JSON 블록을 찾지 못함: {text[:120]}")
    return json.loads(m.group(0))


def _normalize_llm_fields(obj: dict) -> dict:
    """LLM 응답을 안전한 형태로 정규화. 불량 값은 KeyError 로."""
    domain = (obj.get("domain") or "").strip().lower()
    if domain not in _DOMAIN_TO_KO:
        raise ValueError(f"지원하지 않는 domain: {domain!r}")
    intent = (obj.get("intent") or "").strip()
    if intent not in _VALID_INTENTS:
        # 흔한 동의어 보정 — 그래도 매칭 안 되면 '정보'
        intent = {
            "후기": "리뷰",
            "가이드": "공략",
            "베스트": "추천",
        }.get(intent, "정보")
    summary = (obj.get("topic_summary") or "").strip()
    if not summary:
        raise ValueError("topic_summary 가 비어 있음")
    if len(summary) > 200:
        summary = summary[:200]
    return {"domain": domain, "intent": intent, "topic_summary": summary}


# ── 공개 API ────────────────────────────────────────────
def _resolve_model_default() -> str:
    """infra-5: env 또는 코드 default 모델명."""
    return os.environ.get(
        "OSMU_ANTHROPIC_MODEL_INTERPRET",
        "claude-haiku-4-5-20251001",
    )


def interpret(value: Union[str, KeywordContext, None],
              *, use_llm: Optional[bool] = None,
              api_key: Optional[str] = None,
              model: Optional[str] = None) -> KeywordContext:
    """0단계 — keyword → KeywordContext 정규화 (룰 + 옵션 LLM).

    Args:
      value      : str 키워드 또는 이미 만들어진 KeywordContext (passthrough).
      use_llm    : True 면 Anthropic 호출 시도. None 이면 환경변수
                    OSMU_USE_LLM_INTERPRETER=1 일 때만 켜진다.
      api_key    : 명시 키. 미지정 시 ANTHROPIC_API_KEY 사용.
      model      : Claude 모델명. 기본은 Haiku 4.5 (저비용 분류용).

    Returns:
      KeywordContext — source 필드로 어떻게 채워졌는지 표시:
        · 'rule'              : 룰만
        · 'llm'               : LLM 응답 채택 (덮어쓰기)
        · 'llm_fallback_rule' : LLM 시도 후 실패 → 룰 결과로 폴백
    """
    # 입력 정규화: 이미 컨텍스트면 패스, 아니면 룰로 1차 정규화
    if isinstance(value, KeywordContext):
        return value
    base = KeywordContext.coerce(value)
    if not base.keyword:
        return base

    # use_llm 결정
    if use_llm is None:
        use_llm = os.getenv("OSMU_USE_LLM_INTERPRETER", "0").strip() in {"1", "true", "TRUE", "yes"}
    if os.getenv("OSMU_DISABLE_LLM_INTERPRETER", "").strip() in {"1", "true", "yes"}:
        use_llm = False

    if not use_llm:
        return base

    key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        log.info("[interpreter] ANTHROPIC_API_KEY 없음 → 룰 결과 사용")
        return replace(base, source="llm_fallback_rule",
                        raw_signals={**base.raw_signals, "llm_skip": "no_api_key"})

    model = model or _resolve_model_default()
    user_prompt = f"키워드: {base.keyword}"
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            text = _post_anthropic(key, model, _SYSTEM_PROMPT, user_prompt)
            obj = _parse_llm_json(text)
            norm = _normalize_llm_fields(obj)
            log.info(
                "[interpreter] LLM 보강 성공: domain=%s intent=%s summary='%s'",
                norm["domain"], norm["intent"], norm["topic_summary"][:60],
            )
            return replace(
                base,
                domain=norm["domain"],
                inferred_topic=_DOMAIN_TO_KO[norm["domain"]],
                intent_hint=norm["intent"],
                topic_summary=norm["topic_summary"],
                source="llm",
                raw_signals={**base.raw_signals,
                              "llm_model": model,
                              "llm_raw": text[:300]},
            )
        except Exception as e:
            last_err = e
            log.warning("[interpreter] LLM %d차 실패: %s", attempt, e)
            time.sleep(0.8)

    log.warning("[interpreter] LLM 보강 최종 실패 → 룰 결과 사용 (last_err=%s)", last_err)
    # 룰 결과 + topic_summary 가 비었으면 룰 요약으로 한 번 더 채움
    summary = base.topic_summary or rule_topic_summary(
        base.keyword, base.inferred_topic, base.intent_hint,
    )
    return replace(
        base,
        topic_summary=summary,
        source="llm_fallback_rule",
        raw_signals={**base.raw_signals,
                      "llm_skip": f"call_failed: {last_err}"},
    )
