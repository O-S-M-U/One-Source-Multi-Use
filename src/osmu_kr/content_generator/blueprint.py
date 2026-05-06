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


# ── 데이터 구조 ─────────────────────────────────────────
@dataclass
class TargetReader:
    """이 글의 타깃 독자.

    persona         : 한 문장 페르소나 (예: ‘처음 데드바이데이라이트를 시작하는 직장인 게이머’)
    knowledge_level : 초보 | 중급 | 전문가
    primary_intent  : 공략 | 추천 | 비교 | 리뷰 | 구매 | 방법 | 순위 | 팁 | 정보
    """
    persona: str
    knowledge_level: str
    primary_intent: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParagraphBlock:
    """blueprint 의 한 단락 (= 결국 글의 H2 섹션).

    section_index   : 1부터 시작
    title           : H2 제목 (실제 본문에 쓰임)
    paragraph_type  : 'fact_based' | 'llm_generated'
    description     : 이 단락에서 무엇을 다뤄야 하는지 짧은 설명
    facts_required  : fact_based 일 때만 — 어떤 사실/수치를 가져와야 하는지 키워드 리스트
    """
    section_index: int
    title: str
    paragraph_type: str
    description: str
    facts_required: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


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
    """collector Phase 1 산출물 묶음 (4종 + commercial + 메타).

    summary_embedding 은 별도 단계에서 채워진다 — 여기선 None.
    commercial_elements 는 같은 LLM 호출에서 함께 생성 (4단계 신규).
    """
    keyword: str
    title: str
    target_reader: TargetReader
    paragraphs: List[ParagraphBlock]
    intro: str
    short_conclusion: str
    summary_embedding: Optional[List[float]] = None
    commercial_elements: CommercialElements = field(default_factory=CommercialElements)
    source: str = "rule"
    raw_signals: dict = field(default_factory=dict)

    # ── 직렬화 ──
    def paragraph_blueprint_json(self) -> List[dict]:
        return [p.to_dict() for p in self.paragraphs]

    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "title": self.title,
            "target_reader": self.target_reader.to_dict(),
            "paragraph_blueprint": self.paragraph_blueprint_json(),
            "intro": self.intro,
            "short_conclusion": self.short_conclusion,
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
_KNOWLEDGE_LEVEL_BY_INTENT = {
    "공략": "초보",
    "방법": "초보",
    "팁":   "초보",
    "추천": "중급",
    "비교": "중급",
    "리뷰": "중급",
    "구매": "중급",
    "순위": "중급",
    "정보": "초보",
}


def _rule_target_reader(ctx: KeywordContext) -> TargetReader:
    """domain + intent 만으로 만드는 default 타깃 독자."""
    persona = (
        f"‘{ctx.keyword}’ 검색으로 들어온 {ctx.inferred_topic} 도메인 독자 — "
        f"의도는 ‘{ctx.intent_hint}’ 입니다."
    )
    return TargetReader(
        persona=persona,
        knowledge_level=_KNOWLEDGE_LEVEL_BY_INTENT.get(ctx.intent_hint, "초보"),
        primary_intent=ctx.intent_hint,
    )


def _rule_paragraphs(ctx: KeywordContext) -> List[ParagraphBlock]:
    """domain_profile.section_titles 를 기반으로 단락 청사진을 만든다.

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
    n = len(titles)
    blocks: List[ParagraphBlock] = []
    for i, t in enumerate(titles, start=1):
        if i == 1 or i == n:
            ptype = "llm_generated"
            facts: List[str] = []
        else:
            ptype = "fact_based"
            facts = [ctx.keyword, ctx.intent_hint]
        blocks.append(ParagraphBlock(
            section_index=i,
            title=t,
            paragraph_type=ptype,
            description=(profile.section_requirements[i - 1]
                          if i - 1 < len(profile.section_requirements) else ""),
            facts_required=facts,
        ))
    return blocks


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
        commercial_elements=_rule_commercial_elements(ctx),
        intro=_rule_intro(ctx),
        short_conclusion=_rule_short_conclusion(ctx),
        source="rule",
    )


# ── LLM 보강 ───────────────────────────────────────────
_SYSTEM_PROMPT = """당신은 한국어 수익형 블로그의 ‘콘텐츠 설계자’ 입니다.
주어진 KeywordContext 를 받아 collector Phase 1 산출물을 설계하세요.

【 출력 — JSON 한 객체만, 다른 설명/코드블록 금지 】
{
  "title": "한 줄 글 제목 (h1, 60자 이내)",
  "target_reader": {
    "persona": "한 문장 페르소나",
    "knowledge_level": "초보 | 중급 | 전문가 중 하나",
    "primary_intent": "공략 | 추천 | 비교 | 리뷰 | 구매 | 방법 | 순위 | 팁 | 정보 중 하나"
  },
  "paragraphs": [
    {
      "section_index": 1,
      "title": "H2 제목",
      "paragraph_type": "fact_based 또는 llm_generated",
      "description": "이 단락에서 다뤄야 할 핵심을 한 줄로",
      "facts_required": ["fact_based 일 때 검색 필요 키워드들", "..."]
    },
    ...
  ],
  "intro": "글 첫 문단 한 문장 (임베딩 입력용)",
  "short_conclusion": "글 마지막 한 문장 (임베딩 입력용)",
  "commercial_elements": {
    "recommendations": ["글에서 추천으로 다룰 구체 항목 3~6개 (캐릭터/상품/전략 등)"],
    "comparison_points": ["비교 단락·표에 들어갈 비교 축 3~5개"],
    "cta_candidates": ["글에 자연스럽게 박을 CTA 문구 후보 3~5개"]
  }
}

【 강제 룰 】
1) paragraphs 는 4~6개. 첫 단락과 마지막 단락은 반드시 paragraph_type=llm_generated.
2) 그 사이 단락은 가급적 fact_based — 외부 사실/수치/비교가 필요한 정보로.
3) ‘일반 템플릿’ 금지: ‘개념’, ‘활용’, ‘결론’, ‘소개’, ‘마무리’ 같은 추상 H2 만으로 구성하면 안 됩니다.
   각 단락 title 은 키워드의 의도(intent)를 직접 해결하는 구체 행동/요소를 담아야 합니다.
4) target_reader.primary_intent 는 KeywordContext.intent_hint 와 동일하게.
5) commercial_elements 의 3개 리스트는 각각 최소 2개 이상 채울 것 — 추천·비교·CTA 가 비면 글이
   ‘만들어졌지만 돈이 안 되는’ 상태가 됩니다. 이 글의 도메인·의도에 맞는 구체 항목을 적으세요.
6) 모든 텍스트는 한국어. 큰따옴표 안에 줄바꿈/제어문자 넣지 말 것.
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


_VALID_LEVEL = {"초보", "중급", "전문가"}
_VALID_INTENT = {"공략", "추천", "비교", "리뷰", "구매", "방법", "순위", "팁", "정보"}
_VALID_PTYPE = {"fact_based", "llm_generated"}


def _normalize_blueprint_obj(obj: dict, ctx: KeywordContext) -> BlueprintResult:
    """LLM 응답 dict → BlueprintResult. 필드 누락/잘못된 값은 raise."""
    title = (obj.get("title") or "").strip()
    if not title:
        raise ValueError("title 누락")

    tr = obj.get("target_reader") or {}
    persona = (tr.get("persona") or "").strip()
    level = (tr.get("knowledge_level") or "").strip()
    intent = (tr.get("primary_intent") or "").strip()
    if level not in _VALID_LEVEL:
        level = _KNOWLEDGE_LEVEL_BY_INTENT.get(ctx.intent_hint, "초보")
    if intent not in _VALID_INTENT:
        intent = ctx.intent_hint or "정보"
    if not persona:
        persona = f"‘{ctx.keyword}’ 를 찾는 {ctx.inferred_topic} 도메인 독자"
    target = TargetReader(persona=persona, knowledge_level=level, primary_intent=intent)

    raw_paragraphs = obj.get("paragraphs") or []
    if not isinstance(raw_paragraphs, list) or len(raw_paragraphs) < 3:
        raise ValueError(f"paragraphs 부족: {len(raw_paragraphs) if isinstance(raw_paragraphs, list) else 'not_list'}")
    paragraphs: List[ParagraphBlock] = []
    for i, p in enumerate(raw_paragraphs, start=1):
        if not isinstance(p, dict):
            continue
        ptype = (p.get("paragraph_type") or "").strip()
        if ptype not in _VALID_PTYPE:
            ptype = "fact_based"
        facts = p.get("facts_required") or []
        if not isinstance(facts, list):
            facts = [str(facts)]
        paragraphs.append(ParagraphBlock(
            section_index=int(p.get("section_index") or i),
            title=(p.get("title") or "").strip() or f"섹션 {i}",
            paragraph_type=ptype,
            description=(p.get("description") or "").strip(),
            facts_required=[str(f).strip() for f in facts if str(f).strip()],
        ))
    if len(paragraphs) < 3:
        raise ValueError(f"유효 paragraph 개수 부족: {len(paragraphs)}")

    intro = (obj.get("intro") or _rule_intro(ctx)).strip()
    conc = (obj.get("short_conclusion") or _rule_short_conclusion(ctx)).strip()

    # commercial_elements — LLM 응답을 그대로 보존. 누락은 빈 상태로 두고
    # phase1 의 auto-fix 가 룰 폴백으로 보강 (validator 가 ‘비어있음’ 을 잡아야 시그널 남음).
    ce_obj = obj.get("commercial_elements") or {}
    def _str_list(v) -> List[str]:
        if not v:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [str(v).strip()]
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
        intro=intro,
        short_conclusion=conc,
        commercial_elements=commercial,
        source="llm",
    )


# ── 공개 API ───────────────────────────────────────────
def generate_blueprint(value: Union[KeywordContext, str],
                       *, use_llm: Optional[bool] = None,
                       api_key: Optional[str] = None,
                       model: str = "claude-sonnet-4-6") -> BlueprintResult:
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
