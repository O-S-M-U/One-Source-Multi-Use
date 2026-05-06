"""phase2 — collector Phase 2: fact_based 단락별 normalized_sources 매핑.

[ 위치 — 파이프라인 ]
  Phase 1 (blueprint + embedding + commercial) → ★ Phase 2 (facts) ★ → contents_maker

[ 책임 ]
  Phase 1 의 paragraph_blueprint 에서 paragraph_type='fact_based' 인 단락만
  골라서, 단락별로 ‘facts_required’ 키워드들을 검색·크롤링·정제 → fact 배열로 정규화.
  결과물은 v9 spec 의 contents.normalized_sources 와 동일한 구조.

[ 산출물 — Phase2Result ]
  · sources_by_section : Dict[section_index → List[FactItem]]
  · total_facts        : 전체 fact 개수 (모든 fact_based 단락 합산)
  · issues             : 도메인 미스매치 / 최소 facts 미달 등 게이트 결과 — 비어있으면 통과
  · meta               : 디버그용 (단락별 검색 쿼리·소스 수 등)

[ 도메인 관련성 체크 ]
  · 게임 도메인인데 facts 안에 ‘게임/플레이/캐릭터/맵/모드/공략/빌드’ 같은 도메인 마커가
    한 번도 안 잡히면 → 'domain_mismatch' issue.
  · 도메인 마커는 keyword_classifier 의 *_TERMS 사전을 그대로 재사용.

[ 최소 facts 강제 ]
  · 단락당 최소 N개 (기본 3) — 모자라면 'insufficient_facts:section=X' issue.
  · Phase2Config.min_facts_per_section 으로 조정 가능.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .blueprint import BlueprintResult, ParagraphBlock
from .collector import _clean_paragraph, _dedup_sentences, _split_sentences
from .interfaces import BaseCrawler, CrawledPage
from .keyword_classifier import (
    BEAUTY_TERMS, DIET_TERMS, FINANCE_TERMS, FOOD_TERMS, GAME_TERMS,
    IT_TERMS, TRAVEL_TERMS,
)

log = logging.getLogger(__name__)


# ── 데이터 구조 ─────────────────────────────────────────
@dataclass
class FactItem:
    """단락별 정규화 fact 한 건.

    fact_text  : LLM 이 본문 작성 시 인용할 수 있는 ‘정제된 한 문장’
    source_url : 원 URL
    source_title: 원문 제목 (필수 아님)
    query      : 이 fact 를 가져오게 한 검색 쿼리
    """
    fact_text: str
    source_url: str
    source_title: str = ""
    query: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Phase2Result:
    keyword: str
    sources_by_section: Dict[int, List[FactItem]] = field(default_factory=dict)
    total_facts: int = 0
    issues: List[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "sources_by_section": {
                str(k): [it.to_dict() for it in v]
                for k, v in self.sources_by_section.items()
            },
            "total_facts": self.total_facts,
            "issues": list(self.issues),
            "meta": dict(self.meta),
        }

    def short(self) -> str:
        return (f"keyword='{self.keyword}' / fact_sections={len(self.sources_by_section)} "
                f"/ total_facts={self.total_facts} / issues={self.issues or '-'}")


@dataclass
class Phase2Config:
    facts_per_query: int = 3            # 한 query → 가져올 fact 문장 수 상한
    min_facts_per_section: int = 3      # fact_based 단락당 최소 fact 개수
    min_total_facts: int = 6            # 전체 최소
    pages_per_query: int = 2            # 한 query → Firecrawl 페이지 수


# ── 도메인 마커 ──────────────────────────────────────────
_DOMAIN_TERMS = {
    "game": GAME_TERMS,
    "finance": FINANCE_TERMS,
    "diet": DIET_TERMS,
    "it": IT_TERMS,
    "beauty": BEAUTY_TERMS,
    "travel": TRAVEL_TERMS,
    "food": FOOD_TERMS,
}


def _normalize(text: str) -> str:
    return (text or "").replace(" ", "").lower()


def _domain_relevance(domain: str, facts: List[FactItem]) -> float:
    """0..1 비율 — 전체 fact 중 도메인 마커 단어가 등장한 fact 의 비율."""
    terms = _DOMAIN_TERMS.get(domain or "", ())
    if not terms or not facts:
        return 1.0  # general 도메인이거나 fact 없을 땐 체크 무의미
    hit = 0
    for f in facts:
        norm = _normalize(f.fact_text)
        raw = (f.fact_text or "").lower()
        if any(_normalize(t) in norm or t.lower() in raw for t in terms):
            hit += 1
    return hit / len(facts)


# ── 본 처리 ──────────────────────────────────────────────
class Phase2Collector:
    """blueprint + crawler → Phase2Result.

    crawler 는 Phase 1 과 같은 BaseCrawler 인터페이스를 사용한다.
    """

    def __init__(self, crawler: BaseCrawler, *, config: Optional[Phase2Config] = None):
        self.crawler = crawler
        self.cfg = config or Phase2Config()

    # ── 한 단락 처리 ──
    def _facts_for_section(self, section: ParagraphBlock,
                           keyword: str) -> List[FactItem]:
        out: List[FactItem] = []
        seen_norm: set = set()
        # facts_required 가 없으면 단락 title 자체를 쿼리로
        queries = list(section.facts_required) or [section.title]
        # 키워드 자체를 첫 쿼리에 결합해 도메인을 잡기 쉽게
        head = (keyword or "").strip()
        if head:
            queries = [f"{head} {q}".strip() for q in queries] + [head]
        for q in queries:
            try:
                pages: List[CrawledPage] = self.crawler.search_and_scrape(
                    q, limit=self.cfg.pages_per_query,
                )
            except Exception as e:
                log.warning("[phase2] section=%s query=%r 크롤 실패: %s",
                            section.section_index, q, e)
                continue
            for page in pages:
                if not page or not page.content or page.error:
                    continue
                cleaned = _clean_paragraph(page.content)
                if not cleaned:
                    continue
                sentences = _dedup_sentences(_split_sentences(cleaned))
                # 너무 짧은 문장 제외 — 한국어 기준 24자
                useful = [s for s in sentences if len(s) >= 24][: self.cfg.facts_per_query]
                for s in useful:
                    norm = re.sub(r"\s+|[^0-9A-Za-z가-힣]", "", s).lower()
                    if not norm or norm in seen_norm:
                        continue
                    seen_norm.add(norm)
                    out.append(FactItem(
                        fact_text=s,
                        source_url=page.url,
                        source_title=getattr(page, "title", "") or "",
                        query=q,
                    ))
        return out

    # ── 전체 ──
    def run(self, blueprint: BlueprintResult, *,
            domain: str = "") -> Phase2Result:
        result = Phase2Result(keyword=blueprint.keyword)
        targets = [p for p in blueprint.paragraphs if p.paragraph_type == "fact_based"]
        if not targets:
            log.info("[phase2] fact_based 단락 0개 — Phase2 건너뜀")
            return result

        log.info("[phase2] 시작: keyword='%s' / fact_sections=%d / domain='%s'",
                  blueprint.keyword, len(targets), domain)

        all_facts: List[FactItem] = []
        for section in targets:
            facts = self._facts_for_section(section, blueprint.keyword)
            result.sources_by_section[section.section_index] = facts
            all_facts.extend(facts)
            if len(facts) < self.cfg.min_facts_per_section:
                result.issues.append(
                    f"insufficient_facts:section={section.section_index}:got={len(facts)}",
                )

        result.total_facts = len(all_facts)
        if result.total_facts < self.cfg.min_total_facts:
            result.issues.append(f"total_facts_too_low:got={result.total_facts}")

        # 도메인 관련성
        if domain:
            ratio = _domain_relevance(domain, all_facts)
            result.meta["domain_relevance_ratio"] = round(ratio, 3)
            if ratio < 0.2:
                result.issues.append(f"domain_mismatch:ratio={ratio:.2f}")

        log.info("[phase2] 완료: %s", result.short())
        return result
