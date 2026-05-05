"""KeywordContext — collector 입력 구조 (1단계).

[ 왜 필요한가 ]
지금까지 keyword 가 단순 str 로 모듈 사이를 흘러다녔다. 그러다 보니
‘데드바이데이라이트 = 게임’ 같은 도메인 정보가 collector → contents_maker
중간에서 사라지고, LLM 이 일반 비즈니스 도입 가이드 같은 결과를 냈다.

이 모듈은 그 출발점을 막는다 — keyword 를 모듈 경계 너머로 넘길 때 항상
{keyword + inferred_topic + intent_hint} 묶음으로 감싸서 전달.

[ 자동 추론 — 룰 기반 ]
- inferred_topic: keyword_classifier.profile_for() 의 한국어 라벨 그대로
  · 게임 / 재테크·금융 / 다이어트·건강 / IT·디지털 / 뷰티 / 여행 / 음식 / 일반
- intent_hint:  키워드에 포함된 의도 단어로 추출
  · 공략 / 추천 / 비교 / 리뷰 / 구매 / 방법 / 순위 / 정보(default)

LLM 호출 없이 룰만으로 90% 이상 잡힌다. 모자라면 향후 LLM 보조 추론을 추가.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

from .keyword_classifier import Domain, classify, profile_for

log = logging.getLogger(__name__)


# ── intent 추론 사전 (한국어 키워드 안의 의도 단어) ────
INTENT_KEYWORDS: Dict[str, List[str]] = {
    # 게임/IT 도메인에서 강한 신호
    "공략":   ["공략", "꿀팁", "노하우", "가이드 영상", "전략", "팁 모음"],
    # 일반 의도
    "추천":   ["추천", "베스트", "best", "top", "TOP", "탑"],
    "비교":   ["비교", " vs ", "vs ", "차이", "어떤게", "어느게"],
    "리뷰":   ["리뷰", "후기", "사용기", "솔직 후기", "내돈내산"],
    "구매":   ["가격", "구매", "쇼핑", "할인", "최저가"],
    "방법":   ["하는법", "하는 법", "방법", "어떻게"],
    "순위":   ["순위", "랭킹", "ranking"],
    "팁":     ["팁", "꿀팁", "초보", "초보자"],
}

# intent → 영문 카테고리 (LLM 프롬프트나 영어 검색어 변환에 활용 가능)
INTENT_EN: Dict[str, str] = {
    "공략": "guide tips",
    "추천": "recommendation best picks",
    "비교": "comparison versus",
    "리뷰": "review",
    "구매": "buying purchase",
    "방법": "how to",
    "순위": "ranking",
    "팁":   "tips",
    "정보": "information",
}


def infer_intent(keyword: str) -> str:
    """키워드 텍스트만으로 intent 추론. 매칭 안 되면 '정보'."""
    if not keyword:
        return "정보"
    lower = keyword.lower()
    for intent, terms in INTENT_KEYWORDS.items():
        for t in terms:
            if t in keyword or t.lower() in lower:
                return intent
    return "정보"


@dataclass
class KeywordContext:
    """collector 로 넘기는 키워드 컨텍스트.

    필드:
      · keyword         : 원문 키워드 (사용자 입력 그대로)
      · inferred_topic  : 도메인 한글 라벨 (예: '게임')
      · intent_hint     : 추론된 의도 한글 라벨 (예: '공략', '추천')
      · domain          : 도메인 영문 코드 (예: 'game')
      · raw_signals     : 추가 메타 (LLM 추론 점수 등 향후 확장)

    str 와 KeywordContext 둘 다 받는 함수에서 한 줄로 정규화하기 위한
    classmethod `coerce()` 가 핵심 진입점이다.
    """
    keyword: str
    inferred_topic: str
    intent_hint: str
    domain: str = ""
    raw_signals: Dict[str, str] = field(default_factory=dict)

    # ── 생성 ────────────────────────────────────────
    @classmethod
    def from_keyword(cls, keyword: str) -> "KeywordContext":
        """keyword 단독으로부터 자동 추론 컨텍스트 생성."""
        kw = (keyword or "").strip()
        if not kw:
            return cls(keyword="", inferred_topic="일반", intent_hint="정보",
                        domain=Domain.GENERAL.value)
        profile = profile_for(kw)
        intent = infer_intent(kw)
        return cls(
            keyword=kw,
            inferred_topic=profile.name_ko,
            intent_hint=intent,
            domain=profile.domain.value,
        )

    @classmethod
    def coerce(cls, value: Union[str, "KeywordContext", None]) -> "KeywordContext":
        """str 또는 KeywordContext 또는 None → 항상 KeywordContext."""
        if value is None:
            return cls.from_keyword("")
        if isinstance(value, cls):
            return value
        return cls.from_keyword(str(value))

    # ── 표현 ────────────────────────────────────────
    def to_log_dict(self) -> dict:
        """로그 한 줄로 찍기 좋은 dict."""
        return {
            "keyword": self.keyword,
            "inferred_topic": self.inferred_topic,
            "intent_hint": self.intent_hint,
            "domain": self.domain,
        }

    def short(self) -> str:
        """짧은 한 줄 요약 — 로그 헤더에 적합."""
        return (f"keyword='{self.keyword}' "
                f"topic={self.inferred_topic}({self.domain}) "
                f"intent={self.intent_hint}")
