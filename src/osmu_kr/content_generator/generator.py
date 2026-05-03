"""Generator — 콘텐츠 생성 전체 흐름.

[ generate(keyword) 흐름 — 파이프라인 위치 ]
  Step 1. Collector → Firecrawl 검색·크롤링 → raw_content
  Step 2. ImageProvider → 영어 변환 → ImageItem 2~3개 (글 생성 ‘직전’)
  Step 3. Writer → SEO HTML  ← keyword + raw_content + 이미지(URL+filename+alt) 한 번에 전달
                              Claude 가 본문 흐름 안에 이미지 위치를 직접 결정 (사후 삽입 X)
  Step 4. HTML 검증 (validate_html_structure) → 부족하면 휴리스틱 보강
  Step 5. content_db 저장
            · refined_post = 최종 HTML
            · original_source = ‘url1, url2, url3’
            · image_urls = JSON([{url, filename, alt, ...}, ...])  ← Slack/Playwright 가 이용
            · status = 'generated'
            · error_log = 단계별 발생 경고

[ 에러 정책 ]
  · Firecrawl 실패 → fallback 텍스트로 진행
  · 이미지 0개 → 진행 가능 (error_log 기록)
  · LLM 실패 → 1회 retry → fallback_to_heuristic 로 휴리스틱 폴백 (옵션)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

from ..models import ContentRecord, now_utc, to_iso
from ..storage import build_storage
from ..storage.base import BaseStorage
from ..config import Config

from .collector import Collector, RawContent
from .firecrawl_client import FirecrawlClient
from .images import ChainedImageProvider, PicsumImageProvider, UnsplashImageProvider
from .interfaces import (
    BaseCrawler, BaseImageProvider, BaseWriter,
    GenerationResult, ImageItem,
)
from .keyword_translator import keyword_to_slug
from .writer import (
    AnthropicWriter, HeuristicWriter,
    repair_missing_images, strip_banned_phrases, validate_html_structure,
)

log = logging.getLogger(__name__)


# raw_content 가 부족할 때 Writer 에 전달할 ‘일반 시드’ — 신뢰도 저하 표현 0개.
# Writer 의 시스템 프롬프트가 이 시드를 받아 본문을 풍부하게 확장하도록 유도한다.
_FALLBACK_SEED_TEMPLATE = (
    "{keyword} 의 정의, 활용 방법, 자주 하는 실수, 비교 기준, 그리고 다음 단계로 "
    "확장하는 방법까지 한 글에서 정리합니다. 처음 접하는 독자가 핵심 흐름을 빠르게 "
    "잡을 수 있도록 개념 → 사례 → 주의사항 → 핵심 요약 순서로 구성합니다."
)


@dataclass
class GeneratorConfig:
    n_sources: int = 3
    n_images: int = 3
    min_images: int = 2                 # 정책: 최소 2장
    fallback_to_heuristic: bool = True
    pool_max_chars: int = 6000
    require_real_images: bool = False   # True 면 picsum 폴백 비활성 (Unsplash 만)
    min_h2_sections: int = 3
    min_paragraphs: int = 5


class Generator:
    def __init__(self, *, storage: Optional[BaseStorage] = None,
                 crawler: Optional[BaseCrawler] = None,
                 writer: Optional[BaseWriter] = None,
                 images: Optional[BaseImageProvider] = None,
                 config: Optional[GeneratorConfig] = None,
                 cfg: Optional[Config] = None):
        self.cfg = cfg or Config()
        self.storage = storage or build_storage(self.cfg)
        self.crawler = crawler or FirecrawlClient()
        self.writer = writer or AnthropicWriter()
        self.gencfg = config or GeneratorConfig()
        # 이미지 Provider — require_real_images=True 면 Unsplash 만 사용
        if images is not None:
            self.images = images
        elif self.gencfg.require_real_images:
            self.images = UnsplashImageProvider()
        else:
            self.images = ChainedImageProvider([
                UnsplashImageProvider(),
                PicsumImageProvider(),
            ])
        self._collector = Collector(self.crawler, min_sources=self.gencfg.n_sources)

    # ── 공개 API ─────────────────────────────────
    def generate(self, keyword: str, *, save: bool = True,
                 title_final: str = "") -> GenerationResult:
        kw = (keyword or "").strip()
        if not kw:
            raise ValueError("keyword 가 비어 있습니다.")

        log.info("▶ generate(%r) 시작", kw)
        slug = keyword_to_slug(kw)

        # ── Step 1: 검색·크롤링 ──
        raw = self._collector.collect(kw, limit=self.gencfg.n_sources)
        crawl_error = ""
        if raw.is_empty():
            crawl_error = raw.error or "raw_content_empty"
            log.warning("[generator] raw_content 없음 → 일반 시드 사용 (%s)", crawl_error)
            seed_text = _FALLBACK_SEED_TEMPLATE.format(keyword=kw)
            raw = RawContent(
                keyword=kw, sources=[], pages=[],
                text=seed_text,
                char_count=len(seed_text),
                error=crawl_error,
            )

        # ── Step 2: 이미지 (글 생성 직전) ──
        images: List[ImageItem] = []
        image_error = ""
        try:
            images = self.images.search(
                kw, count=self.gencfg.n_images,
                slug=slug, alt_keyword=kw,
            ) or []
        except Exception as e:
            image_error = f"image_search_failed: {e}"
            log.warning("[generator] %s", image_error)

        if len(images) < self.gencfg.min_images and not self.gencfg.require_real_images:
            # 부족 시 picsum 폴백 (require_real_images=False 일 때만)
            need = self.gencfg.min_images - len(images)
            try:
                fallback = PicsumImageProvider().search(
                    kw, count=max(need, 0), slug=slug, alt_keyword=kw,
                )
                images.extend(fallback)
            except Exception as e:
                image_error = (image_error + " | picsum_fallback_failed: " + str(e)).strip(" |")
        elif len(images) < self.gencfg.min_images and self.gencfg.require_real_images:
            image_error = (image_error +
                            f" | unsplash_only_yielded:{len(images)}/{self.gencfg.min_images}").strip(" |")

        if not images:
            image_error = (image_error + " | no_images_used").strip(" |")
            log.warning("[generator] 이미지 0개 — 텍스트만으로 진행")

        # ── Step 3: Writer (raw + 이미지 함께 전달) ──
        write_error = ""
        writer_used = self.writer.name
        try:
            html = self.writer.write(
                kw, raw.text[: self.gencfg.pool_max_chars],
                sources=raw.sources, images=images,
            )
        except Exception as e:
            write_error = str(e)
            log.warning("[generator] 1차 writer 실패: %s", e)
            if self.gencfg.fallback_to_heuristic:
                fallback = HeuristicWriter()
                html = fallback.write(kw, raw.text, sources=raw.sources, images=images)
                writer_used = f"{self.writer.name}→heuristic_fallback"
                write_error += " | fallback_used"
            else:
                if save:
                    self._save_failed(kw, raw, images, write_error)
                raise RuntimeError(f"콘텐츠 생성 실패: {e}") from e

        # ── Step 4a: 금지 표현 자동 제거 (신뢰도 보호) ──
        html = strip_banned_phrases(html)

        # ── Step 4b: HTML 구조 + 내용 검증 + 이미지 보강 ──
        expected_imgs = max(self.gencfg.min_images, len(images))
        issues = validate_html_structure(
            html,
            expected_image_count=expected_imgs,
            min_h2=self.gencfg.min_h2_sections,
            min_p=self.gencfg.min_paragraphs,
        )
        if any(i.startswith("insufficient_images") for i in issues) and images:
            html = repair_missing_images(html, images)
            issues = validate_html_structure(
                html,
                expected_image_count=expected_imgs,
                min_h2=self.gencfg.min_h2_sections,
                min_p=self.gencfg.min_paragraphs,
            )

        # ── Step 5: content_db 저장 ──
        record_id = ""
        full_error = " | ".join(filter(None, [
            crawl_error, image_error, write_error,
            ("html_issues:" + ",".join(issues)) if issues else "",
        ]))
        if save:
            record_id = self._save_record(
                kw, html, raw, images, status="generated",
                error_log=full_error, writer_used=writer_used,
                title_final=title_final,
            )

        return GenerationResult(
            keyword=kw,
            refined_post=html,
            original_source=raw.sources,
            image_urls=images,
            raw_content=raw.text,
            status="generated",
            error_log=full_error,
            record_id=record_id,
            html_issues=issues,
        )

    # ── 내부: content_db 저장 ──
    def _next_content_id(self) -> str:
        existing = self.storage.list_content()
        nums = []
        for r in existing:
            try:
                nums.append(int(r.id))
            except (TypeError, ValueError):
                pass
        n = (max(nums) if nums else 0) + 1
        return f"{n:03d}"

    @staticmethod
    def _images_to_json(images: List[ImageItem]) -> str:
        return json.dumps([img.to_dict() for img in images], ensure_ascii=False)

    def _save_record(self, keyword, html, raw, images, *, status,
                     error_log, writer_used, title_final="") -> str:
        rec = ContentRecord(
            id=self._next_content_id(),
            keyword=keyword,
            seed_keyword="",
            keyword_id="",
            original_source=", ".join(raw.sources),
            status=status,
            title_final=title_final,
            created_at=to_iso(now_utc()),
            raw_content=raw.text[:8000],
            refined_post=html,
            image_urls=self._images_to_json(images),    # ★ JSON 으로 저장
            error_log=error_log,
            note=f"generated by {writer_used}",
        )
        self.storage.append_content(rec)
        log.info("✅ content_db 저장: id=%s html=%d 자 images=%d",
                  rec.id, len(html), len(images))
        return rec.id

    def _save_failed(self, keyword, raw, images, err) -> str:
        return self._save_record(
            keyword, "", raw, images, status="실패",
            error_log=f"writer_failed: {err}", writer_used="failed",
        )
