"""콘텐츠 생성 모듈 — 추상 인터페이스.

확장성 보장:
  · BaseCrawler       — Firecrawl / Playwright / 네이버 자체 크롤러 등 교체 가능
  · BaseWriter        — Anthropic / OpenAI / 휴리스틱 등 교체 가능
  · BaseImageProvider — Unsplash / Pixabay / placeholder 등 교체 가능

기존 osmu_kr 본체 코드는 수정 없음 — 본 모듈만의 추상 계층이다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

# ImageItem 은 아래에서 정의하므로 forward reference 회피용 placeholder 없음


# ── 데이터 모델 ─────────────────────────────────────────
@dataclass
class CrawledPage:
    """크롤링된 단일 페이지."""
    url: str
    title: str = ""
    content: str = ""
    error: Optional[str] = None


@dataclass
class ImageItem:
    """본문 삽입용 이미지 — URL + 파일명 + 역할(role) + 메타.

    파일명 규칙: ${slug}-${idx}.${ext}
      예) "직장인 다이어트 식단" → "office-diet-meal-1.jpg" / "office-diet-meal-2.jpg"

    role 은 콘텐츠 안에서 이 이미지가 무슨 의미로 쓰이는지 명시한다.
    Writer 가 글 구조와 매핑할 때 사용한다.
      · "concept"     — 개념 설명 섹션. 1번 이미지에 권장
      · "example"     — 실제 활용 사례 섹션. 2번 이미지에 권장
      · "comparison"  — 비교/주의사항 섹션. 3번 이미지에 권장
      · "summary"     — 마무리 정리. 추가 이미지가 있을 때

    실제 파일 저장이 아니라 ‘발행/검토 단계의 식별자’ 로 사용된다 (Slack/티스토리 발행 시).
    """
    url: str
    filename: str
    alt: str = ""
    width: int = 0
    height: int = 0
    source: str = ""        # 'unsplash' / 'picsum' / ...
    role: str = ""          # 'concept' / 'example' / 'comparison' / 'summary'
    caption: str = ""       # 본문 figcaption 으로 사용할 수 있는 짧은 설명

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "filename": self.filename,
            "alt": self.alt,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "role": self.role,
            "caption": self.caption,
        }


@dataclass
class GenerationResult:
    """Generator.generate() 의 반환 객체."""
    keyword: str
    refined_post: str
    original_source: List[str]
    image_urls: List[ImageItem]      # ★ ImageItem 리스트로 변경
    raw_content: str = ""
    status: str = "generated"
    error_log: str = ""
    record_id: str = ""
    html_issues: List[str] = field(default_factory=list)

    def to_summary(self) -> str:
        return (
            f"keyword='{self.keyword}' status={self.status} "
            f"sources={len(self.original_source)} images={len(self.image_urls)} "
            f"html_len={len(self.refined_post)}"
            + (f" issues={','.join(self.html_issues)}" if self.html_issues else "")
        )


# ── 추상 계층 ──────────────────────────────────────────
class BaseCrawler(ABC):
    """검색 + 페이지 크롤링."""

    name: str = "base_crawler"

    @abstractmethod
    def search(self, query: str, *, limit: int = 5) -> List[str]:
        """검색 결과 URL 목록 반환. 실패 시 빈 리스트."""

    @abstractmethod
    def scrape(self, url: str) -> CrawledPage:
        """단일 페이지 본문 추출. 실패 시 CrawledPage(url=..., error=...)."""

    def search_and_scrape(self, query: str, *, limit: int = 3) -> List[CrawledPage]:
        """검색 → 각 URL 본문 추출. 기본 구현 — 백엔드별로 override 가능."""
        urls = self.search(query, limit=limit)
        pages: List[CrawledPage] = []
        for url in urls:
            pages.append(self.scrape(url))
        return pages


class BaseWriter(ABC):
    """글 본문 HTML 생성기.

    [ 두 진입점 ]
      1) write_from_blueprint(blueprint, normalized_sources, images)  ★ v13 권장
         - collector Phase 1 청사진 + Phase 2 정규화 facts 를 받아 ‘충실히 HTML 변환’.
         - fact_based 단락 → facts 만 컨텍스트로 (raw 소스 비노출 — 표절 방어).
         - llm_generated 단락 → keyword + core_message + 글 맥락만으로 자유 생성.

      2) write(keyword, raw_content, sources, images)                 (legacy 호환)
         - 기존 raw_content 직접 입력 — collector phase 가 비활성·실패한 경우의 폴백.

    [ 기본 동작 ]
      구현체는 write() 한 가지만 의무로 구현. write_from_blueprint() 의 default
      구현은 blueprint + facts 를 raw_content 텍스트로 직렬화해 write() 에 위임 —
      기존 Writer 가 즉시 동작.
    """

    name: str = "base_writer"

    @abstractmethod
    def write(
        self,
        keyword: str,
        raw_content: str,
        *,
        sources: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        tone: str = "전문적",
    ) -> str:
        """HTML 콘텐츠를 반환. 실패 시 RuntimeError."""

    def write_from_blueprint(
        self,
        blueprint,                       # BlueprintResult — circular import 회피
        normalized_sources=None,         # Optional[Phase2Result] (또는 dict)
        *,
        images: Optional[List[ImageItem]] = None,
        tone: str = "전문적",
    ) -> str:
        """v13 진입점 — 청사진 + facts 기반.

        Default 구현: blueprint + facts 를 텍스트로 직렬화해 write() 호출.
        v13 spec 그대로 받고 싶은 구현체는 이 메서드를 override.
        """
        sources_list: List[str] = []
        # 1) phase2 의 source_url 들을 sources 로 모음
        try:
            if normalized_sources is not None:
                if hasattr(normalized_sources, "sources_by_section"):
                    for facts in normalized_sources.sources_by_section.values():
                        for f in facts:
                            url = getattr(f, "source_url", "")
                            if url and url not in sources_list:
                                sources_list.append(url)
                elif isinstance(normalized_sources, dict):
                    for facts in normalized_sources.values():
                        for f in facts:
                            url = (f.get("source_url") if isinstance(f, dict) else "") or ""
                            if url and url not in sources_list:
                                sources_list.append(url)
        except Exception:
            pass

        # 2) blueprint + facts 를 raw_content 직렬화 (legacy write() 입력)
        lines = [f"# {blueprint.title}", "",
                  f"[도입] {blueprint.intro}",
                  f"[결론] {blueprint.short_conclusion}", ""]
        for p in blueprint.paragraphs:
            lines.append(f"## {p.title}  ({p.paragraph_type})")
            if p.description:
                lines.append(f"  - 핵심: {p.description}")
            # fact_based 단락은 facts 도 같이 (있을 때만)
            if p.paragraph_type == "fact_based" and normalized_sources is not None:
                facts = []
                if hasattr(normalized_sources, "sources_by_section"):
                    facts = normalized_sources.sources_by_section.get(p.section_index, [])
                elif isinstance(normalized_sources, dict):
                    facts = normalized_sources.get(p.section_index, []) or \
                             normalized_sources.get(str(p.section_index), [])
                for f in facts[:5]:
                    txt = getattr(f, "fact_text", None) or (f.get("fact_text", "") if isinstance(f, dict) else "")
                    if txt:
                        lines.append(f"  · {txt}")
            lines.append("")
        # commercial 도 단락 끝에 안내
        ce = blueprint.commercial_elements
        if ce.recommendations:
            lines.append("[추천 항목] " + " / ".join(ce.recommendations[:5]))
        if ce.cta_candidates:
            lines.append("[CTA 후보] " + " / ".join(ce.cta_candidates[:3]))

        return self.write(
            blueprint.keyword,
            "\n".join(lines),
            sources=sources_list,
            images=images,
            tone=tone,
        )


class BaseImageProvider(ABC):
    """이미지 검색 — 반드시 ImageItem (url + filename + 메타) 리스트를 반환."""

    name: str = "base_images"

    @abstractmethod
    def search(self, query: str, *, count: int = 3,
               slug: str = "", alt_keyword: str = "") -> List[ImageItem]:
        """이미지 메타 리스트. 실패 시 빈 리스트.

        Args:
            query: 검색어 (한글 그대로 또는 영어 — 구현체가 적절히 처리)
            count: 원하는 이미지 개수 (정책상 2~3 권장)
            slug: 파일명 prefix 로 쓸 영어 슬러그 (예: 'office-diet-meal')
            alt_keyword: alt 텍스트에 포함할 키워드 (한글 OK)
        """
