"""blueprint — collector Phase 1 산출물 (3단계).

[ 위치 — 파이프라인 ]
  keyword_researcher → interpret → ★ Phase 1 (blueprint + embedding) ★ → Phase 2 (facts) → ...

[ 책임 ]
  KeywordContext 를 받아서 ‘이 글을 어떻게 쓸 것인가’ 의 청사진을 만든다.
  v9 spec 정렬:
    · title              (글 제목, h1)
    · target_reader      (persona / knowledge_level / primary_intent)
    · paragraph_blueprint(단락별 구조·타입)
    · short_conclusion   (글 끝 한 줄 요약 — 임베딩 입력용)
    · intro              (도입 한 단락 — 임베딩 입력용)
  ※ summary_embedding 은 별도 모듈(embedder.py) 이 채운다.

[ 단락 타입 ]
  · fact_based     : 뉴스·공공자료 등 외부 사실/수치/주장이 필요한 단락 (Phase 2 fact 수집 대상)
  · llm_generated  : 도입·맺음·맥락 자유 생성 단락 (Phase 2 건너뜀)

[ 두 모드 ]
  1) 룰 모드 (use_llm=False)
     · domain_profile.section_titles + topic_summary 로 paragraph_blueprint 의
       ‘이름과 단락 타입’ 만 결정. title 은 키워드 그대로, target_reader 는 도메인 기본.
     · LLM 호출 0회. 외부 의존성 없음.
  2) LLM 보강 모드 (use_llm=True 또는 OSMU_USE_LLM_BLUEPRINT=1)
     · ANTHROPIC_API_KEY 로 Claude 호출 → JSON 응답을 파싱해 4종 모두 채움.
     · 호출 실패 / JSON 파싱 실패 / 일반 템플릿 reject → 룰 결과로 폴백.
       (일반 템플릿 reject 룰은 validator.py 가 담당, Collector.phase1 에서 호출)

[ ‘일반 템플릿 금지’ 정책 ]
  · paragraph_blueprint.titles 가 [‘개념’, ‘활용’, ‘결론’] 류 일반 템플릿이면 reject.
  · Reject 시 룰 모드로 폴백 + raw_signals 에 사유 기록.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field, replace
from typing import List, Optional, Union

from .keyword_classifier import profile_for
from .keyword_context import KeywordContext

log = logging.getLogger(__name__)


# ── v13 enum ──────────────────────────────────────────
KNOWLEDGE_LEVELS = ("beginner", "intermediate", "expert")
# v13 spec: primary_intent 는 3종 (정보탐색/구매결정/문제해결).
# 키워드 의도(공략·추천·비교·…)는 별도 KeywordContext.intent_hint 에만 남고
# 청사진의 target_reader 가 사용하는 ‘행동 의도’는 이 3종으로만 한정.
PRIMARY_INTENTS = ("정보탐색", "구매결정", "문제해결")
OVERALL_TONES = ("정보형", "후기형", "비교형")
ENDING_TYPES = ("summary", "cta", "recommendation")
PARAGRAPH_TYPES = ("llm_generated", "fact_based")
FACT_TYPES = ("statistic", "claim", "fact", "definition")


# ── 데이터 구조 (v13 spec 정확히 정렬) ────────────────
@dataclass
class TargetReader:
    """v13 spec — 타깃 독자.

    persona         : 한 문장 페르소나
    knowledge_level : beginner | intermediate | expert
    primary_intent  : 정보탐색 | 구매결정 | 문제해결
    """
    persona: str
    knowledge_level: str = "beginner"
    primary_intent: str = "정보탐색"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ending:
    """v13 spec — 글 끝맺음 방향."""
    type: str = "summary"          # summary | cta | recommendation
    direction: str = ""             # 마무리에서 유도할 행동/추천 방향

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Fact:
    """v13 spec — fact_based 단락에 들어가는 정규화 fact 한 건."""
    type: str = "fact"              # statistic | claim | fact | definition
    content: str = ""
    entity: str = ""                # 출처 기관/사람
    year: Optional[int] = None
    source_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ParagraphBlock:
    """v13 spec — body_paragraphs[*].

    paragraph_id              : "p1", "p2", ...
    type                      : llm_generated | fact_based
    h2_subtitle               : H2 제목
    core_message              : 이 단락이 다룰 핵심 주장/주제
    assigned_keywords         : LLM 이 단락 주제 분석으로 도출한 서브 키워드 (SEO)
    image_search_keywords_en  : 이미지 검색 영문 키워드
    image_alt_text_ko         : 한국어 alt text 초안
    facts                     : fact_based 일 때만. Phase 2 에서 채워짐
    """

    __slots__ = (
        "paragraph_id", "type", "h2_subtitle", "core_message",
        "assigned_keywords", "image_search_keywords_en",
        "image_alt_text_ko", "facts",
    )

    def __init__(self, paragraph_id=None, type=None, h2_subtitle=None,
                  core_message="", assigned_keywords=None,
                  image_search_keywords_en=None, image_alt_text_ko="",
                  facts=None,
                  # ── legacy kwargs (후방호환) ──
                  section_index=None, title=None,
                  paragraph_type=None, description=None,
                  facts_required=None,
                  # positional 처리 — 첫 두 개는 v13 인자 (paragraph_id, type)
                  # 또는 legacy (section_index 정수, "fact_based" 등) 둘 다 가능
                  ):
        # paragraph_id 결정
        if paragraph_id is None and section_index is not None:
            paragraph_id = f"p{int(section_index)}"
        if paragraph_id is None:
            paragraph_id = "p1"
        self.paragraph_id = str(paragraph_id)

        # type 결정
        if type is None and paragraph_type is not None:
            type = paragraph_type
        self.type = type or "fact_based"

        # h2_subtitle 결정
        if h2_subtitle is None and title is not None:
            h2_subtitle = title
        self.h2_subtitle = h2_subtitle or ""

        # core_message 결정
        if not core_message and description:
            core_message = description
        self.core_message = core_message or ""

        # assigned_keywords 결정
        if assigned_keywords is None and facts_required is not None:
            assigned_keywords = facts_required
        self.assigned_keywords = list(assigned_keywords or [])
        self.image_search_keywords_en = list(image_search_keywords_en or [])
        self.image_alt_text_ko = image_alt_text_ko or ""
        self.facts = list(facts or [])

    def to_dict(self) -> dict:
        d = {
            "paragraph_id": self.paragraph_id,
            "type": self.type,
            "h2_subtitle": self.h2_subtitle,
            "core_message": self.core_message,
            "assigned_keywords": list(self.assigned_keywords),
            "image_search_keywords_en": list(self.image_search_keywords_en),
            "image_alt_text_ko": self.image_alt_text_ko,
        }
        # fact_based 일 때만 facts 포함
        if self.type == "fact_based":
            d["facts"] = [f.to_dict() if hasattr(f, "to_dict") else f
                            for f in self.facts]
        return d

    def __repr__(self) -> str:
        return (
            f"ParagraphBlock(id={self.paragraph_id!r}, type={self.type!r}, "
            f"h2={self.h2_subtitle!r})"
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, ParagraphBlock):
            return NotImplemented
        return all(getattr(self, k) == getattr(other, k) for k in self.__slots__)

    # ── 후방호환 alias (읽기 전용) ──
    @property
    def section_index(self) -> int:
        try:
            return int(str(self.paragraph_id).lstrip("pP"))
        except ValueError:
            return 0

    @property
    def title(self) -> str:
        return self.h2_subtitle

    @property
    def paragraph_type(self) -> str:
        return self.type

    @property
    def description(self) -> str:
        return self.core_message

    @property
    def facts_required(self) -> List[str]:
        return list(self.assigned_keywords)


@dataclass
class CommercialElements:
    """수익형 콘텐츠의 ‘돈 되는 포인트’ — 글이 만들어진 뒤에는 만들기 어렵다.

    recommendations    : 글에서 추천으로 다룰 구체 항목 (캐릭터/상품/전략 등)
    comparison_points  : 비교 단락·표에 들어갈 비교 축
    cta_candidates     : 글 안에 자연스럽게 박을 CTA 문구 후보
    """
    recommendations: List[str] = field(default_factory=list)
    comparison_points: List[str] = field(default_factory=list)
    cta_candidates: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_empty(self) -> bool:
        return not (self.recommendations or self.comparison_points or self.cta_candidates)


@dataclass
class BlueprintResult:
    """v13 spec — collector Phase 1 산출물.

    spec 정렬: title / intro / short_conclusion / target_reader / overall_tone /
              ending / body_paragraphs (= paragraphs).
    OSMU 추가: summary_embedding / commercial_elements / source / raw_signals.
    """
    keyword: str
    title: str
    target_reader: TargetReader
    paragraphs: List[ParagraphBlock]          # = body_paragraphs
    intro: str
    short_conclusion: str
    overall_tone: str = "정보형"               # 정보형 | 후기형 | 비교형
    ending: Ending = field(default_factory=Ending)
    summary_embedding: Optional[List[float]] = None
    commercial_elements: CommercialElements = field(default_factory=CommercialElements)
    source: str = "rule"
    raw_signals: dict = field(default_factory=dict)

    # ── 직렬화 ──
    def paragraph_blueprint_json(self) -> List[dict]:
        return [p.to_dict() for p in self.paragraphs]

    @property
    def body_paragraphs(self) -> List[ParagraphBlock]:
        """v13 spec 키 그대로 노출."""
        return self.paragraphs

    def to_dict(self) -> dict:
        """v13 spec 그대로의 JSON dict."""
        return {
            "title": self.title,
            "intro": self.intro,
            "short_conclusion": self.short_conclusion,
            "target_reader": self.target_reader.to_dict(),
            "overall_tone": self.overall_tone,
            "ending": self.ending.to_dict(),
            "body_paragraphs": self.paragraph_blueprint_json(),
            # OSMU 추가 메타
            "keyword": self.keyword,
            "summary_embedding": self.summary_embedding,
            "commercial_elements": self.commercial_elements.to_dict(),
            "source": self.source,
            "raw_signals": dict(self.raw_signals),
        }

    def short(self) -> str:
        n_fact = sum(1 for p in self.paragraphs if p.paragraph_type == "fact_based")
        ce = self.commercial_elements
        return (f"title='{self.title}' / sections={len(self.paragraphs)} "
                f"(fact={n_fact}, llm={len(self.paragraphs) - n_fact}) "
                f"reader={self.target_reader.knowledge_level} "
                f"recs={len(ce.recommendations)} cmp={len(ce.comparison_points)} "
                f"cta={len(ce.cta_candidates)} "
                f"src={self.source}")

    # 임베딩 입력용 텍스트 — title + intro + short_conclusion (v9 정의)
    def embedding_input(self) -> str:
        return f"{self.title}\n{self.intro}\n{self.short_conclusion}".strip()


# ── 룰 모드 — domain profile 기반 폴백 ──────────────────
# v13 spec 의 3종 primary_intent (정보탐색/구매결정/문제해결) 로 매핑.
_PRIMARY_INTENT_BY_KEYWORD_INTENT = {
    "공략": "문제해결",
    "방법": "문제해결",
    "팁":   "문제해결",
    "추천": "구매결정",
    "비교": "구매결정",
    "리뷰": "구매결정",
    "구매": "구매결정",
    "순위": "구매결정",
    "정보": "정보탐색",
}
_KNOWLEDGE_LEVEL_BY_KEYWORD_INTENT = {
    "공략": "beginner",
    "방법": "beginner",
    "팁":   "beginner",
    "추천": "intermediate",
    "비교": "intermediate",
    "리뷰": "intermediate",
    "구매": "intermediate",
    "순위": "intermediate",
    "정보": "beginner",
}
_TONE_BY_PRIMARY_INTENT = {
    "정보탐색": "정보형",
    "구매결정": "비교형",
    "문제해결": "정보형",
}
_ENDING_BY_PRIMARY_INTENT = {
    "정보탐색": ("summary",        "다음에 시도할 만한 콘텐츠 안내"),
    "구매결정": ("recommendation", "독자 상황에 맞는 1순위 추천"),
    "문제해결": ("cta",            "구체 행동 1가지로 마무리"),
}

# 이미지 검색어 — 도메인별 기본 영문 키워드 (개별 단락에서 override 가능)
_DEFAULT_IMG_QUERIES_BY_DOMAIN = {
    "game":    ["video game", "gameplay screenshot", "gaming setup"],
    "finance": ["finance chart", "investment growth", "stock market"],
    "diet":    ["healthy meal", "fitness", "balanced diet"],
    "it":      ["modern laptop", "tech gadget", "smartphone"],
    "beauty":  ["skincare products", "korean beauty", "cosmetics flatlay"],
    "travel":  ["travel landscape", "tourist landmark", "airport"],
    "food":    ["korean food", "cooking ingredients", "homemade dish"],
    "general": ["minimalist desk", "team collaboration", "checklist"],
}


def _rule_target_reader(ctx: KeywordContext) -> TargetReader:
    """v13 spec 정렬 — knowledge_level 영문 / primary_intent 3종."""
    persona = (
        f"‘{ctx.keyword}’ 검색으로 들어온 {ctx.inferred_topic} 도메인 독자 — "
        f"의도는 ‘{ctx.intent_hint}’ 입니다."
    )
    return TargetReader(
        persona=persona,
        knowledge_level=_KNOWLEDGE_LEVEL_BY_KEYWORD_INTENT.get(
            ctx.intent_hint, "beginner"),
        primary_intent=_PRIMARY_INTENT_BY_KEYWORD_INTENT.get(
            ctx.intent_hint, "정보탐색"),
    )


def _rule_paragraphs(ctx: KeywordContext) -> List[ParagraphBlock]:
    """v13 spec ParagraphBlock 그대로 채움.

    fact_based / llm_generated 분류 룰:
      · 1번 (도입·개요) : llm_generated
      · 마지막 (마무리·정리·체크리스트) : llm_generated
      · 그 사이 모두 : fact_based
    """
    profile = profile_for(ctx.keyword)
    titles = list(profile.section_titles) or [
        "1. 핵심 개념과 정의",
        "2. 실제 활용 사례",
        "3. 자주 묻는 질문",
    ]
    img_defaults = _DEFAULT_IMG_QUERIES_BY_DOMAIN.get(
        (ctx.domain or "general").lower(),
        _DEFAULT_IMG_QUERIES_BY_DOMAIN["general"],
    )
    n = len(titles)
    blocks: List[ParagraphBlock] = []
    for i, t in enumerate(titles, start=1):
        if i == 1 or i == n:
            ptype = "llm_generated"
            assigned: List[str] = []
        else:
            ptype = "fact_based"
            # v13 spec: assigned_keywords 는 단락 주제 분석으로 도출한 서브 키워드
            assigned = [ctx.keyword, ctx.intent_hint]
        # 단락별 이미지 검색어 — 룰 모드는 도메인 default 사용
        img_query = [img_defaults[(i - 1) % len(img_defaults)]]
        blocks.append(ParagraphBlock(
            paragraph_id=f"p{i}",
            type=ptype,
            h2_subtitle=t,
            core_message=(profile.section_requirements[i - 1]
                            if i - 1 < len(profile.section_requirements) else ""),
            assigned_keywords=assigned,
            image_search_keywords_en=img_query,
            image_alt_text_ko=f"{ctx.keyword} {t} 관련 이미지",
        ))
    return blocks


def _rule_overall_tone(ctx: KeywordContext) -> str:
    primary = _PRIMARY_INTENT_BY_KEYWORD_INTENT.get(ctx.intent_hint, "정보탐색")
    return _TONE_BY_PRIMARY_INTENT.get(primary, "정보형")


def _rule_ending(ctx: KeywordContext) -> Ending:
    primary = _PRIMARY_INTENT_BY_KEYWORD_INTENT.get(ctx.intent_hint, "정보탐색")
    end_type, direction = _ENDING_BY_PRIMARY_INTENT.get(
        primary, ("summary", "핵심 정리"))
    return Ending(type=end_type, direction=direction)


def _rule_title(ctx: KeywordContext) -> str:
    """룰 모드 기본 제목 — 키워드 + 의도 라벨."""
    if ctx.intent_hint and ctx.intent_hint != "정보":
        return f"{ctx.keyword} — 핵심 {ctx.intent_hint} 가이드"
    return f"{ctx.keyword} 알아야 할 핵심 정리"


def _rule_intro(ctx: KeywordContext) -> str:
    """룰 모드 도입 한 줄 — 임베딩 입력으로 쓰임."""
    return ctx.topic_summary or (
        f"이 글은 ‘{ctx.keyword}’ 키워드를 찾아온 독자에게 핵심을 정리해 전달합니다."
    )


def _rule_short_conclusion(ctx: KeywordContext) -> str:
    return f"{ctx.keyword} 의 핵심을 {ctx.intent_hint} 관점으로 정리한 가이드입니다."


# ── 룰 모드 — Commercial Elements (도메인별 폴백) ────────
_COMMERCIAL_TEMPLATES = {
    # domain code → (recommendations 라벨, comparison axes, CTA 후보)
    "game": (
        ["입문자 추천 캐릭터·직업", "추천 빌드/세팅", "초보자용 모드/난이도"],
        ["캐릭터별 강점·약점", "맵·모드 별 적합도", "초보 vs 숙련자 동선"],
        ["추천 빌드 자세히 보기", "초보 가이드 영상 보러가기", "오늘의 핫한 모드 확인"],
    ),
    "finance": (
        ["수익률 상위 상품 3개", "초보자용 추천 포트폴리오", "절세 우대 계좌"],
        ["수익률 vs 변동성", "수수료·세금 비교", "1년·3년·5년 성과"],
        ["수익률 비교표 보기", "추천 상품 자세히 보기", "절세 시뮬레이터 열기"],
    ),
    "diet": (
        ["입문자 식단 1주차 메뉴", "강도별 운동 루틴 추천", "대체 식품 추천"],
        ["식단 vs 운동 효율", "단백질 출처별 비교", "1주 vs 4주 변화"],
        ["1주 식단표 받기", "추천 운동 루틴 보기", "식단 챌린지 참여"],
    ),
    "it": (
        ["가성비 추천 모델", "프로/입문자별 추천", "추천 액세서리"],
        ["성능·배터리 비교", "가격대 비교", "OS·생태계 비교"],
        ["최신 가격 확인", "비교표 더 보기", "구매처 이동"],
    ),
    "beauty": (
        ["피부타입별 추천 제품", "성분 추천", "가격대별 추천 라인업"],
        ["성분·효능 비교", "민감성 vs 일반", "가격 대비 만족도"],
        ["내 피부 진단 받기", "추천 제품 자세히 보기", "후기 모아 보기"],
    ),
    "travel": (
        ["가성비 숙소 추천", "코스별 추천 일정", "맛집/카페 추천"],
        ["성수기 vs 비수기", "일정별 예산 비교", "숙소 등급 비교"],
        ["추천 일정 자세히 보기", "숙소 가격 비교하기", "현지 가이드 모아 보기"],
    ),
    "food": (
        ["기본 레시피 추천", "대체 재료 추천", "응용 메뉴 추천"],
        ["조리법 비교", "재료 비용 비교", "보관·재가열 비교"],
        ["기본 레시피 보기", "재료 구매처 보기", "응용 메뉴 추천 받기"],
    ),
    "general": (
        ["대표 사례 3개", "추천 학습 자료", "체크리스트 가이드"],
        ["옵션 A vs B", "장단점 비교", "비용·시간 비교"],
        ["관련 가이드 더 보기", "체크리스트 받기", "다음 단계 안내"],
    ),
}


def _rule_commercial_elements(ctx: KeywordContext) -> CommercialElements:
    domain = (ctx.domain or "general").lower()
    recs, cmp_, ctas = _COMMERCIAL_TEMPLATES.get(domain, _COMMERCIAL_TEMPLATES["general"])
    return CommercialElements(
        recommendations=list(recs),
        comparison_points=list(cmp_),
        cta_candidates=list(ctas),
    )


def _rule_blueprint(ctx: KeywordContext) -> BlueprintResult:
    return BlueprintResult(
        keyword=ctx.keyword,
        title=_rule_title(ctx),
        target_reader=_rule_target_reader(ctx),
        paragraphs=_rule_paragraphs(ctx),
        overall_tone=_rule_overall_tone(ctx),
        ending=_rule_ending(ctx),
        commercial_elements=_rule_commercial_elements(ctx),
        intro=_rule_intro(ctx),
        short_conclusion=_rule_short_conclusion(ctx),
        source="rule",
    )


# ── LLM 보강 (v13 spec 정렬) ───────────────────────────
_SYSTEM_PROMPT = """당신은 한국어 수익형 블로그의 ‘콘텐츠 설계자’ 입니다.
주어진 KeywordContext 를 받아 collector Phase 1 산출물을 v13 spec 형식 그대로 설계하세요.

【 출력 — JSON 한 객체만, 다른 설명/코드블록 금지 】
{
  "title": "h1으로 들어갈 제목",
  "intro": "도입문 — 글에서 다룰 내용을 제시해 사용자가 계속 읽도록 유도",
  "short_conclusion": "짧은 결론 — 답을 암시하되 본문 안 읽으면 손해라는 신호",
  "target_reader": {
    "persona": "예: 홈카페 입문 1년 미만, 20-30대",
    "knowledge_level": "beginner | intermediate | expert",
    "primary_intent": "정보탐색 | 구매결정 | 문제해결"
  },
  "overall_tone": "정보형 | 후기형 | 비교형",
  "ending": {
    "type": "summary | cta | recommendation",
    "direction": "마무리에서 유도할 행동/추천 방향"
  },
  "body_paragraphs": [
    {
      "paragraph_id": "p1",
      "type": "llm_generated",
      "h2_subtitle": "단락 소제목 (h2 태그)",
      "core_message": "이 단락이 다룰 핵심 주장/주제",
      "assigned_keywords": ["LLM이 단락 주제 분석으로 도출한 서브 키워드"],
      "image_search_keywords_en": ["english", "search", "terms"],
      "image_alt_text_ko": "한국어 alt text 초안"
    },
    {
      "paragraph_id": "p2",
      "type": "fact_based",
      "h2_subtitle": "...",
      "core_message": "...",
      "assigned_keywords": ["..."],
      "image_search_keywords_en": ["..."],
      "image_alt_text_ko": "..."
    }
  ],
  "commercial_elements": {
    "recommendations": ["..."],
    "comparison_points": ["..."],
    "cta_candidates": ["..."]
  }
}

【 강제 룰 】
1) body_paragraphs 는 4~6개. 첫·마지막 단락은 반드시 type=llm_generated.
2) 그 사이 단락은 type=fact_based — 외부 사실/수치/비교가 필요한 정보로.
3) ‘일반 템플릿’ 금지: ‘개념’, ‘활용’, ‘결론’ 류 추상 h2_subtitle 만으로 구성 금지.
   각 h2_subtitle 은 키워드의 의도를 직접 해결하는 구체 행동/요소를 담을 것.
4) knowledge_level / primary_intent / overall_tone / ending.type 은 위 enum 중 정확히 하나.
5) commercial_elements 3개 리스트는 각각 최소 2개 이상.
6) fact_based 단락의 facts 배열은 비워두세요 — collector Phase 2 가 채웁니다.
7) 모든 텍스트는 한국어 (영문 키워드 예외: image_search_keywords_en).
8) 큰따옴표 안에 줄바꿈/제어문자 금지.
"""


def _build_user_prompt(ctx: KeywordContext) -> str:
    return (
        f"KeywordContext:\n"
        f"  keyword       = {ctx.keyword}\n"
        f"  inferred_topic= {ctx.inferred_topic}\n"
        f"  domain        = {ctx.domain}\n"
        f"  intent_hint   = {ctx.intent_hint}\n"
        f"  topic_summary = {ctx.topic_summary}\n"
    )


def _post_anthropic(api_key: str, model: str, system: str, user: str,
                    *, max_tokens: int = 1500, timeout: int = 60) -> str:
    """Anthropic Messages API 단일 호출 — interpret 의 _post_anthropic 동일 패턴."""
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
            "temperature": 0.3,
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
    raise RuntimeError("Anthropic 응답에 text 블록 없음")


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_blueprint_json(text: str) -> dict:
    if not text:
        raise ValueError("빈 응답")
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _JSON_BLOCK.search(text)
    if not m:
        raise ValueError(f"JSON 블록 없음: {text[:120]}")
    return json.loads(m.group(0))


_VALID_LEVEL = set(KNOWLEDGE_LEVELS)
_VALID_INTENT = set(PRIMARY_INTENTS)
_VALID_TONE = set(OVERALL_TONES)
_VALID_ENDING_TYPE = set(ENDING_TYPES)
_VALID_PTYPE = set(PARAGRAPH_TYPES)

# legacy 호환 — 이전 응답 형태(초보/공략/section_index 등)도 받아들임
_LEGACY_LEVEL = {"초보": "beginner", "중급": "intermediate", "전문가": "expert"}
_LEGACY_INTENT_TO_V13 = {
    "공략": "문제해결", "방법": "문제해결", "팁": "문제해결",
    "추천": "구매결정", "비교": "구매결정", "리뷰": "구매결정",
    "구매": "구매결정", "순위": "구매결정",
    "정보": "정보탐색",
}


def _str_list(v) -> List[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()]


def _normalize_blueprint_obj(obj: dict, ctx: KeywordContext) -> BlueprintResult:
    """LLM 응답 dict → BlueprintResult (v13 spec).

    legacy 응답 형태(paragraphs/title/description 등)도 받아 v13 키로 자동 변환.
    """
    title = (obj.get("title") or "").strip()
    if not title:
        raise ValueError("title 누락")

    # ── target_reader ──
    tr = obj.get("target_reader") or {}
    persona = (tr.get("persona") or "").strip()
    level = (tr.get("knowledge_level") or "").strip()
    intent = (tr.get("primary_intent") or "").strip()
    # legacy 한국어 → v13 영문
    level = _LEGACY_LEVEL.get(level, level)
    if level not in _VALID_LEVEL:
        level = _KNOWLEDGE_LEVEL_BY_KEYWORD_INTENT.get(ctx.intent_hint, "beginner")
    # legacy 9종 키워드 의도 → v13 3종 행동 의도
    intent = _LEGACY_INTENT_TO_V13.get(intent, intent)
    if intent not in _VALID_INTENT:
        intent = _PRIMARY_INTENT_BY_KEYWORD_INTENT.get(ctx.intent_hint, "정보탐색")
    if not persona:
        persona = f"‘{ctx.keyword}’ 를 찾는 {ctx.inferred_topic} 도메인 독자"
    target = TargetReader(persona=persona, knowledge_level=level, primary_intent=intent)

    # ── overall_tone / ending ──
    tone = (obj.get("overall_tone") or "").strip()
    if tone not in _VALID_TONE:
        tone = _TONE_BY_PRIMARY_INTENT.get(intent, "정보형")
    ending_obj = obj.get("ending") or {}
    end_type = (ending_obj.get("type") or "").strip()
    if end_type not in _VALID_ENDING_TYPE:
        end_type, _direction_default = _ENDING_BY_PRIMARY_INTENT.get(
            intent, ("summary", "핵심 정리"))
    end_direction = (ending_obj.get("direction") or "").strip() or "핵심 정리"
    ending = Ending(type=end_type, direction=end_direction)

    # ── body_paragraphs (v13 키) — paragraphs 도 후방호환 ──
    raw_paragraphs = obj.get("body_paragraphs") or obj.get("paragraphs") or []
    if not isinstance(raw_paragraphs, list) or len(raw_paragraphs) < 3:
        raise ValueError(f"body_paragraphs 부족: {len(raw_paragraphs) if isinstance(raw_paragraphs, list) else 'not_list'}")
    paragraphs: List[ParagraphBlock] = []
    for i, p in enumerate(raw_paragraphs, start=1):
        if not isinstance(p, dict):
            continue
        # 필드명 v13 우선 + legacy 폴백
        pid = (p.get("paragraph_id") or f"p{i}").strip()
        ptype = (p.get("type") or p.get("paragraph_type") or "").strip()
        if ptype not in _VALID_PTYPE:
            ptype = "fact_based"
        h2 = (p.get("h2_subtitle") or p.get("title") or "").strip() or f"섹션 {i}"
        core = (p.get("core_message") or p.get("description") or "").strip()
        assigned = _str_list(p.get("assigned_keywords") or p.get("facts_required"))
        img_kw = _str_list(p.get("image_search_keywords_en"))
        img_alt = (p.get("image_alt_text_ko") or "").strip()
        # facts — fact_based 때만, raw 응답에 들어있으면 그대로 파싱
        facts_raw = p.get("facts") or []
        facts: List[Fact] = []
        if isinstance(facts_raw, list):
            for f in facts_raw:
                if isinstance(f, dict):
                    facts.append(Fact(
                        type=str(f.get("type") or "fact"),
                        content=str(f.get("content") or "").strip(),
                        entity=str(f.get("entity") or "").strip(),
                        year=(int(f["year"]) if str(f.get("year") or "").isdigit() else None),
                        source_url=str(f.get("source_url") or "").strip(),
                    ))
        paragraphs.append(ParagraphBlock(
            paragraph_id=pid,
            type=ptype,
            h2_subtitle=h2,
            core_message=core,
            assigned_keywords=assigned,
            image_search_keywords_en=img_kw,
            image_alt_text_ko=img_alt,
            facts=facts,
        ))
    if len(paragraphs) < 3:
        raise ValueError(f"유효 paragraph 개수 부족: {len(paragraphs)}")

    intro = (obj.get("intro") or _rule_intro(ctx)).strip()
    conc = (obj.get("short_conclusion") or _rule_short_conclusion(ctx)).strip()

    # ── commercial_elements ──
    ce_obj = obj.get("commercial_elements") or {}
    commercial = CommercialElements(
        recommendations=_str_list(ce_obj.get("recommendations")),
        comparison_points=_str_list(ce_obj.get("comparison_points")),
        cta_candidates=_str_list(ce_obj.get("cta_candidates")),
    )

    return BlueprintResult(
        keyword=ctx.keyword,
        title=title,
        target_reader=target,
        paragraphs=paragraphs,
        overall_tone=tone,
        ending=ending,
        intro=intro,
        short_conclusion=conc,
        commercial_elements=commercial,
        source="llm",
    )


# ── 공개 API ───────────────────────────────────────────
def _blueprint_model_default() -> str:
    """infra-5: env 또는 코드 default."""
    return os.environ.get("OSMU_ANTHROPIC_MODEL_BLUEPRINT", "claude-sonnet-4-6")


def generate_blueprint(value: Union[KeywordContext, str],
                       *, use_llm: Optional[bool] = None,
                       api_key: Optional[str] = None,
                       model: Optional[str] = None) -> BlueprintResult:
    """KeywordContext → BlueprintResult.

    Args:
      value   : KeywordContext 또는 str. str 이면 룰 기반 정규화로 ctx 생성.
      use_llm : True/False. None 이면 OSMU_USE_LLM_BLUEPRINT 환경변수 따름 (default off).
      api_key : 명시 키. 미지정 시 ANTHROPIC_API_KEY.
      model   : Claude 모델명. blueprint 는 구조 결정이라 Sonnet 추천.

    Returns:
      BlueprintResult — source 필드: 'rule' | 'llm' | 'llm_fallback_rule'
    """
    ctx = value if isinstance(value, KeywordContext) else KeywordContext.coerce(value)
    if not ctx.keyword:
        raise ValueError("keyword 가 비어 있어 blueprint 생성 불가")

    base = _rule_blueprint(ctx)

    if use_llm is None:
        use_llm = os.getenv("OSMU_USE_LLM_BLUEPRINT", "0").strip() in {"1", "true", "TRUE", "yes"}
    if os.getenv("OSMU_DISABLE_LLM_BLUEPRINT", "").strip() in {"1", "true", "yes"}:
        use_llm = False

    if not use_llm:
        return base

    key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        log.info("[blueprint] ANTHROPIC_API_KEY 없음 → 룰 결과 사용")
        return replace(base, source="llm_fallback_rule",
                        raw_signals={**base.raw_signals, "llm_skip": "no_api_key"})

    model = model or _blueprint_model_default()
    user_prompt = _build_user_prompt(ctx)
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            text = _post_anthropic(key, model, _SYSTEM_PROMPT, user_prompt)
            obj = _parse_blueprint_json(text)
            result = _normalize_blueprint_obj(obj, ctx)
            log.info("[blueprint] LLM 보강 성공: %s", result.short())
            return replace(
                result,
                raw_signals={**result.raw_signals,
                              "llm_model": model,
                              "llm_raw": text[:500]},
            )
        except Exception as e:
            last_err = e
            log.warning("[blueprint] LLM %d차 실패: %s", attempt, e)
            time.sleep(0.8)

    log.warning("[blueprint] LLM 보강 최종 실패 → 룰 결과 사용 (last_err=%s)", last_err)
    return replace(base, source="llm_fallback_rule",
                    raw_signals={**base.raw_signals,
                                  "llm_skip": f"call_failed: {last_err}"})
