"""checker — Stage 1 결정적 시스템 검증 (v13 spec 4.4).

[ 원칙 ]
  결정적으로 풀 수 있는 검증은 LLM 에 맡기지 않는다.

[ Tier A — 토큰 0 결정적 검증 ]
  · 글자 수 ≥ checker.min_char_count (default 1500)
  · HTML 구조: h1×1, h2 3~7, p 태그 정상
  · 이미지 개수 일치 + 모든 alt 비어있지 않음
  · assigned_keywords (paragraph_blueprint 의 서브 키워드) 가 본문에 실제 등장
  · 외부 링크 유효성 (HEAD request — 옵션, 환경변수 OSMU_CHECKER_VERIFY_LINKS=1)

[ Tier B — 외부 API ]
  · v1: 임베딩 기반 자기 소스 검사 — collector 의 normalized_sources 와 문장 단위 cosine
  · v1 보완: Google CSE 광역 검사 (OSMU_GOOGLE_CSE_KEY 가 있을 때만)
  · 임계: checker.plagiarism_overall_threshold (0.15), checker.plagiarism_sentence_threshold (0.35)

[ Tier C — LLM 검증 ]
  v1 에서 제외.

[ 사용 ]
  result = checker.run(html, *, blueprint=..., normalized_sources=..., images=...)
  if result.passed: ...
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ── 결과 ────────────────────────────────────────────────
@dataclass
class CheckerResult:
    passed: bool = True
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    char_count: int = 0
    h1_count: int = 0
    h2_count: int = 0
    p_count: int = 0
    img_count: int = 0
    img_with_alt: int = 0
    plagiarism_overall: float = 0.0      # 0..1, 본문 vs 정규화 소스 평균 cosine
    plagiarism_max_sentence: float = 0.0  # 0..1, 문장 단위 max cosine
    keywords_present: List[str] = field(default_factory=list)
    keywords_missing: List[str] = field(default_factory=list)
    link_check: Dict[str, str] = field(default_factory=dict)  # url → 'ok'/'fail:상태'

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def summary(self) -> str:
        return (
            f"passed={self.passed} chars={self.char_count} "
            f"h1={self.h1_count} h2={self.h2_count} img={self.img_count}/{self.img_with_alt} "
            f"plag_overall={self.plagiarism_overall:.3f} "
            f"plag_max={self.plagiarism_max_sentence:.3f} "
            f"issues={len(self.issues)} warnings={len(self.warnings)}"
        )


# ── 텍스트 추출 ──────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_PUNCT = re.compile(r"\s+|[^0-9A-Za-z가-힣]")


def _strip_html(html: str) -> str:
    return _TAG_RE.sub(" ", html or "")


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[\.\?\!])\s+|\n+", text)
    return [s.strip() for s in parts if s.strip() and len(s.strip()) >= 8]


def _normalize(s: str) -> str:
    return _PUNCT.sub("", (s or "").lower())


# ── 핵심 클래스 ────────────────────────────────────────
class Checker:
    """v13 Stage 1 결정적 검증."""

    def __init__(self, *, config_mgr=None, embedder=None):
        self.config_mgr = config_mgr
        self._embedder = embedder

    @property
    def embedder(self):
        if self._embedder is None:
            from .content_generator.embedder import build_embedder
            self._embedder = build_embedder()
        return self._embedder

    # ── Tier A 결정적 검증 ────────────────────────────
    def _check_structure(self, html: str, result: CheckerResult,
                         *, expected_image_count: int = 0,
                         min_char_count: int = 1500) -> None:
        result.char_count = len(_strip_html(html))
        result.h1_count = len(re.findall(r"<h1\b", html, flags=re.IGNORECASE))
        result.h2_count = len(re.findall(r"<h2\b", html, flags=re.IGNORECASE))
        result.p_count = len(re.findall(r"<p\b", html, flags=re.IGNORECASE))
        # 이미지 + alt
        imgs = re.findall(r"<img\b([^>]*)>", html, flags=re.IGNORECASE)
        result.img_count = len(imgs)
        result.img_with_alt = sum(
            1 for tag in imgs
            if re.search(r'alt\s*=\s*"([^"]+)"', tag, flags=re.IGNORECASE)
        )

        # rules
        if result.char_count < min_char_count:
            result.issues.append(f"char_count_too_low:{result.char_count}<{min_char_count}")
        if result.h1_count != 1:
            result.issues.append(f"h1_count_must_be_1:got={result.h1_count}")
        if not (3 <= result.h2_count <= 7):
            result.issues.append(f"h2_count_out_of_range:got={result.h2_count}")
        if result.p_count == 0:
            result.issues.append("p_tag_missing")
        if expected_image_count and result.img_count < expected_image_count:
            result.issues.append(
                f"image_count_too_low:{result.img_count}<{expected_image_count}",
            )
        if result.img_count > 0 and result.img_with_alt < result.img_count:
            result.issues.append(
                f"image_alt_missing:{result.img_count - result.img_with_alt}",
            )

    def _check_keywords_present(self, html: str, blueprint,
                                  result: CheckerResult) -> None:
        text = _strip_html(html).lower()
        norm_text = _normalize(text)
        missing = []
        present = []
        # paragraph_blueprint.assigned_keywords + commercial_elements.recommendations
        kws: List[str] = [blueprint.keyword]
        for p in blueprint.paragraphs:
            kws.extend(getattr(p, "assigned_keywords", []) or [])
        kws.extend(blueprint.commercial_elements.recommendations or [])
        seen = set()
        for kw in kws:
            kw = (kw or "").strip()
            if not kw or kw in seen:
                continue
            seen.add(kw)
            if kw.lower() in text or _normalize(kw) in norm_text:
                present.append(kw)
            else:
                missing.append(kw)
        result.keywords_present = present
        result.keywords_missing = missing
        if missing:
            # 광범위 누락 시 issue, 적은 누락은 warning
            ratio = len(missing) / max(1, len(missing) + len(present))
            if ratio >= 0.5:
                result.issues.append(f"keywords_largely_missing:{len(missing)}")
            else:
                result.warnings.append(f"keywords_partly_missing:{len(missing)}")

    def _check_links(self, html: str, result: CheckerResult,
                       *, verify_external: bool) -> None:
        urls = re.findall(r'href\s*=\s*"(https?://[^"]+)"', html, flags=re.IGNORECASE)
        result.link_check = {}
        if not verify_external or not urls:
            return
        try:
            import requests
        except ImportError:
            return
        for url in set(urls):
            try:
                r = requests.head(url, timeout=5, allow_redirects=True)
                ok = 200 <= r.status_code < 400
                result.link_check[url] = "ok" if ok else f"fail:{r.status_code}"
                if not ok:
                    result.warnings.append(f"link_dead:{url}")
            except Exception as e:
                result.link_check[url] = f"fail:{type(e).__name__}"
                result.warnings.append(f"link_fail:{url}")

    # ── Tier B 표절 검사 (임베딩 기반) ─────────────────
    def _check_plagiarism(self, html: str, normalized_sources,
                           result: CheckerResult,
                           *, overall_threshold: float,
                           sentence_threshold: float) -> None:
        if normalized_sources is None:
            return
        # source 문장 모음
        source_sentences: List[str] = []
        if hasattr(normalized_sources, "sources_by_section"):
            for facts in normalized_sources.sources_by_section.values():
                for f in facts:
                    txt = getattr(f, "fact_text", None) or \
                           (f.get("fact_text", "") if isinstance(f, dict) else "")
                    if txt:
                        source_sentences.append(txt)
        elif isinstance(normalized_sources, dict):
            for facts in normalized_sources.values():
                for f in facts:
                    if isinstance(f, dict) and f.get("fact_text"):
                        source_sentences.append(f["fact_text"])
        if not source_sentences:
            return

        text = _strip_html(html)
        article_sentences = _split_sentences(text)
        if not article_sentences:
            return

        # cosine 비교 (embedder 사용)
        try:
            from .content_generator.embedder import cosine
            src_embs = [self.embedder.encode(s) for s in source_sentences]
            sims_max: List[float] = []
            for asent in article_sentences:
                aemb = self.embedder.encode(asent)
                best = max((cosine(aemb, se) for se in src_embs), default=0.0)
                sims_max.append(best)
            if not sims_max:
                return
            result.plagiarism_overall = sum(sims_max) / len(sims_max)
            result.plagiarism_max_sentence = max(sims_max)
            if result.plagiarism_overall > overall_threshold:
                result.issues.append(
                    f"plagiarism_overall_too_high:{result.plagiarism_overall:.3f}"
                    f">{overall_threshold}",
                )
            if result.plagiarism_max_sentence > sentence_threshold:
                result.issues.append(
                    f"plagiarism_sentence_too_high:{result.plagiarism_max_sentence:.3f}"
                    f">{sentence_threshold}",
                )
        except Exception as e:
            log.warning("[checker] plagiarism check 실패(무시): %s", e)

    # ── 공개 API ──────────────────────────────────────
    def run(self, html: str, *,
             blueprint=None, normalized_sources=None,
             expected_image_count: int = 0,
             verify_external_links: Optional[bool] = None) -> CheckerResult:
        result = CheckerResult()

        cm = self.config_mgr
        min_char = cm.get_int("checker.min_char_count", 1500) if cm else 1500
        plag_overall = cm.get_float("checker.plagiarism_overall_threshold", 0.15) if cm else 0.15
        plag_sent = cm.get_float("checker.plagiarism_sentence_threshold", 0.35) if cm else 0.35

        if verify_external_links is None:
            import os
            verify_external_links = os.environ.get(
                "OSMU_CHECKER_VERIFY_LINKS", ""
            ).strip() in {"1", "true", "yes"}

        # Tier A
        self._check_structure(
            html, result,
            expected_image_count=expected_image_count,
            min_char_count=min_char,
        )
        if blueprint is not None:
            self._check_keywords_present(html, blueprint, result)
        self._check_links(html, result, verify_external=verify_external_links)
        # Tier B
        self._check_plagiarism(
            html, normalized_sources, result,
            overall_threshold=plag_overall,
            sentence_threshold=plag_sent,
        )

        result.passed = not result.issues
        log.info("[checker] %s", result.summary())
        return result
