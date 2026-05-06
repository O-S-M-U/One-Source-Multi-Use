"""blueprint_validator — paragraph_blueprint 가 ‘일반 템플릿’ 인지 자동 reject.

[ 정책 ]
사용자 정의:
  · ‘개념 → 활용 → 결론’ 처럼 chatGPT 가 누구에게나 던질 법한 일반 구조는 자동 실패.
  · 글이 검색 의도(intent)를 직접 해결하는 구체 행동/요소를 담아야 함.

[ 검사 항목 ]
  1) titles 가 일반 추상어(GENERIC_TOKENS) 위주로 구성됐는가
  2) titles 에 키워드 본문 또는 의도(intent)가 거의 안 잡혀 있는가
  3) section 수가 너무 적은가 (3개 미만)
  4) 첫·마지막 단락이 llm_generated 가 아닌가 (룰 위반)

문제가 있으면 issues 리스트로 반환. 비어 있으면 통과.
"""
from __future__ import annotations

import logging
import re
from typing import List

from .blueprint import BlueprintResult
from .keyword_context import INTENT_KEYWORDS, KeywordContext

log = logging.getLogger(__name__)


# 추상 단어들 — 단락 제목이 이것들로만 이루어지면 일반 템플릿 의심
GENERIC_TOKENS = {
    "개념", "정의", "소개", "활용", "활용법",
    "결론", "마무리", "요약", "정리", "끝맺음",
    "도입", "서론", "본론", "총정리",
    "기본", "기초",
}

# 단락 제목에 절대 단독으로 와선 안 되는 ‘구조 그 자체’ 표현
TEMPLATE_PHRASES = [
    "개념 → 활용",
    "개념과 활용",
    "개념 정리",
    "활용 방법",
    "활용과 결론",
    "결론과 정리",
]


def _strip_index(title: str) -> str:
    """‘1. 어쩌고’ 의 1. 같은 인덱스 prefix 제거."""
    return re.sub(r"^\s*\d+[\.\)]\s*", "", title).strip()


def _bareword_only_generic(title: str) -> bool:
    """단락 제목이 추상 단어 1~2개로만 구성됐는지."""
    t = _strip_index(title)
    tokens = re.findall(r"[가-힣A-Za-z]+", t)
    if not tokens or len(tokens) > 3:
        return False
    return all(tok in GENERIC_TOKENS for tok in tokens)


def validate_blueprint(bp: BlueprintResult, ctx: KeywordContext) -> List[str]:
    """검증 결과 issues — 비어 있으면 통과."""
    issues: List[str] = []

    if len(bp.paragraphs) < 3:
        issues.append(f"section_count_too_small:{len(bp.paragraphs)}")

    # 1) 첫·마지막 단락 타입 위반
    if bp.paragraphs:
        if bp.paragraphs[0].paragraph_type != "llm_generated":
            issues.append("first_paragraph_must_be_llm_generated")
        if bp.paragraphs[-1].paragraph_type != "llm_generated":
            issues.append("last_paragraph_must_be_llm_generated")

    # 2) 추상 일반어 위주의 단락 제목 비율
    generic_count = sum(1 for p in bp.paragraphs if _bareword_only_generic(p.title))
    if bp.paragraphs and generic_count >= max(2, len(bp.paragraphs) // 2):
        issues.append(f"too_many_generic_titles:{generic_count}/{len(bp.paragraphs)}")

    # 3) 명시적 일반 템플릿 phrase
    joined = " | ".join(p.title for p in bp.paragraphs)
    for phrase in TEMPLATE_PHRASES:
        if phrase in joined:
            issues.append(f"template_phrase_detected:{phrase}")
            break

    # 4) 키워드/의도가 단락 제목·설명 어디에도 없으면 일반 글 의심
    keyword = (ctx.keyword or "").strip()
    intent = (ctx.intent_hint or "").strip()
    title_text = joined.lower()
    description_text = " ".join(p.description for p in bp.paragraphs)
    haystack = f"{joined}\n{description_text}".lower()

    # intent 동의어까지 함께 매칭 — '공략' ↔ '팁/전략/가이드' 등
    intent_terms = [intent] if intent else []
    intent_terms += INTENT_KEYWORDS.get(intent, [])
    if keyword:
        kw_hit = any(part and part.lower() in haystack for part in keyword.split() if part)
        intent_hit = any(t and t.lower() in haystack for t in intent_terms)
        if not kw_hit and not intent_hit:
            issues.append("titles_have_no_keyword_or_intent_signal")

    # 5) commercial_elements 가 비어 있으면 ‘돈 안 되는 글’ 위험 — 경고 issue
    ce = bp.commercial_elements
    if ce.is_empty():
        issues.append("commercial_elements_empty")
    else:
        if not ce.recommendations:
            issues.append("commercial_recommendations_empty")
        if not ce.cta_candidates:
            issues.append("commercial_cta_empty")

    if issues:
        log.warning("[blueprint_validator] reject: %s — titles=%s", issues, joined)
    return issues
