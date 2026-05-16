"""Writer — raw_content + 이미지 → SEO HTML 콘텐츠.

[ 두 가지 구현체 ]
  · AnthropicWriter  : Claude API. 이미지 위치 결정도 Claude 가 함 (사후 삽입 X).
  · HeuristicWriter  : 자격증명 없을 때 폴백. 본 모듈의 _post_insert_images() 사용.

[ 핵심 정책 ]
  · Anthropic 호출 1회 retry → 실패 시 RuntimeError raise (Generator 가 폴백 결정).
  · 생성된 HTML 은 validate_html_structure() 로 검증 — 문제 발견 시 issues 리스트 반환.
  · ‘Claude 가 이미지 위치를 직접 결정’하도록 프롬프트에 명시 — 사후 삽입은 휴리스틱 폴백 한정.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import List, Optional, Sequence

from .interfaces import BaseWriter, ImageItem
from .keyword_classifier import DomainProfile, profile_for

log = logging.getLogger(__name__)


# ── 공용 유틸 ─────────────────────────────────────────
ROLE_GUIDE = {
    "concept":    "‘기본 개념’ 섹션 안 또는 직후",
    "example":    "‘활용 방법/실제 사례’ 섹션 안",
    "comparison": "‘선택 기준/주의사항’ 섹션 안",
    "summary":    "‘핵심 정리’ 섹션 직전 또는 직후",
}


def _format_image_brief(images: Sequence[ImageItem]) -> str:
    """프롬프트용 이미지 메타 요약 — 각 이미지에 ‘어디에 배치할지’ 까지 지시."""
    if not images:
        return "(이미지 없음 — 본문에 <img> 태그를 추가하지 마세요)"
    lines = []
    for i, img in enumerate(images, 1):
        place = ROLE_GUIDE.get(img.role, "글 흐름에 맞는 위치")
        lines.append(
            f"  {i}. role: {img.role or '(미지정)'} → 배치 권장: {place}\n"
            f"     URL: {img.url}\n"
            f"     filename: {img.filename}\n"
            f"     추천 alt: \"{img.alt}\"\n"
            f"     추천 figcaption: \"{img.caption or img.alt}\""
        )
    return "\n".join(lines)


def _post_insert_images(html: str, images: Sequence[ImageItem]) -> str:
    """휴리스틱 폴백 전용 — H2 사이에 이미지 분산 삽입 (figure + figcaption + role).

    AnthropicWriter 는 사용하지 않는다 (요구사항: Claude 가 위치 결정).
    """
    if not images:
        return html

    def _figure(im: ImageItem) -> str:
        cap = im.caption or im.alt
        return (
            "<figure>"
            f'<img src="{im.url}" alt="{im.alt}" data-filename="{im.filename}" '
            f'data-role="{im.role or ""}" loading="lazy" />'
            f"<figcaption>{cap}</figcaption>"
            "</figure>"
        )

    parts = re.split(r"(<h2[^>]*>.*?</h2>)", html, flags=re.IGNORECASE | re.DOTALL)
    if len(parts) <= 1:
        for im in images:
            html += _figure(im)
        return html
    out = []
    img_idx = 0
    for chunk in parts:
        if chunk.lower().startswith("<h2") and img_idx < len(images):
            out.append(_figure(images[img_idx]))
            img_idx += 1
        out.append(chunk)
    while img_idx < len(images):
        out.append(_figure(images[img_idx]))
        img_idx += 1
    return "".join(out)


# ── 절대 금지 표현 ────────────────────────────────────
BANNED_PHRASES = (
    "외부 검색이",
    "외부 검색을",
    "데이터가 부족",
    "정보가 부족",
    "정보가 충분하지",
    "기본 가이드",
    "참고 자료가 제한",
    "임시로 작성",
    "정확한 최신 정보는 공식 출처",
    "일시적으로 어려",
)


# ── HTML 검증 ─────────────────────────────────────────
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?>", re.IGNORECASE)
_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.IGNORECASE)
_ALT_RE = re.compile(r'alt=["\']([^"\']*)["\']', re.IGNORECASE)


_H2_RE = re.compile(r"<h2[^>]*>", re.IGNORECASE)
_P_RE = re.compile(r"<p[^>]*>", re.IGNORECASE)


def validate_html_structure(html: str, *, expected_image_count: int = 2,
                              min_h2: int = 3, min_p: int = 5) -> List[str]:
    """HTML 구조 + 내용 품질 검증 — 문제점 리스트 반환. 비면 OK."""
    issues: List[str] = []
    h = (html or "").lower()
    if "<h1" not in h:
        issues.append("missing_h1")

    h2_count = len(_H2_RE.findall(html or ""))
    if h2_count < min_h2:
        issues.append(f"insufficient_h2:{h2_count}/{min_h2}")

    p_count = len(_P_RE.findall(html or ""))
    if p_count < min_p:
        issues.append(f"insufficient_p:{p_count}/{min_p}")

    img_tags = _IMG_TAG_RE.findall(html or "")
    if len(img_tags) < expected_image_count:
        issues.append(f"insufficient_images:{len(img_tags)}/{expected_image_count}")
    for tag in img_tags:
        if not _SRC_RE.search(tag):
            issues.append("img_missing_src")
        if not _ALT_RE.search(tag):
            issues.append("img_missing_alt")

    # 금지 표현 검사
    for phrase in BANNED_PHRASES:
        if phrase in (html or ""):
            issues.append(f"banned_phrase:{phrase[:12]}")
    return issues


def strip_banned_phrases(html: str) -> str:
    """금지 표현이 들어간 문장(<p> 단위) 자동 제거."""
    if not html:
        return html
    cleaned = html
    # 단순 문자열 치환 — 문제 표현이 포함된 <p>...</p> 통째로 빈 문자열로
    p_pattern = re.compile(r"<p[^>]*>.*?</p>", re.IGNORECASE | re.DOTALL)
    def _filter_p(m: re.Match) -> str:
        block = m.group(0)
        for phrase in BANNED_PHRASES:
            if phrase in block:
                return ""
        return block
    cleaned = p_pattern.sub(_filter_p, cleaned)
    return cleaned


def repair_missing_images(html: str, images: Sequence[ImageItem]) -> str:
    """LLM 응답에 이미지가 빠져 있을 때 — 휴리스틱 분산 삽입으로 보강."""
    img_count = len(_IMG_TAG_RE.findall(html or ""))
    if img_count >= max(2, len(images)):
        return html
    return _post_insert_images(html or "", images)


# ── Anthropic 시스템 프롬프트 — 도메인 적응형 ────────
ANTHROPIC_SYSTEM_PROMPT_BASE = """당신은 한국어 수익형 블로그 전문 작가입니다.
이 글은 단순 정보 글이 아니라, ‘검색 유입 → 체류 → 클릭 → 수익’으로 이어지는 구조를
만들어야 합니다. 즉, 독자의 실제 검색 의도를 명확히 충족시키고, 자연스럽게 다음 내용을
계속 읽고 싶게 흐름을 설계해야 합니다.

사용자가 제공하는 ‘raw_content’ 는 실제로 검색·크롤링된 자료입니다.
임의로 일반론을 만들어내지 말고, 이 자료를 본인 표현으로 재구성·확장해 정보 밀도 높은 글을 작성하세요.

[ 절대 금지 표현 ]
다음 표현 또는 그와 비슷한 의미의 문장은 본문에 절대 포함하지 마세요:
  · "외부 검색이 어렵", "데이터가 부족", "정보가 충분하지 않", "기본 가이드"
  · "참고 자료가 제한적", "임시로 작성", "추정", "일시적으로"
  · "이 글은 ~의 정의, 활용, 주의사항을 정리합니다" 같은 ‘목차 안내문’ 그대로 노출
이런 표현은 콘텐츠 신뢰도를 떨어뜨립니다. 자료가 부족하면 자연스럽게 본인의 일반 지식으로 채우되,
가짜 통계나 출처 미상의 인용은 만들지 마세요.

[ 문단 품질 기준 ]
- 각 <p> 는 60자 이상.
- 각 문단은 ‘구체적 설명’ 또는 ‘실제 사례’ 또는 ‘수치/기준’ 중 최소 1가지를 포함.
- “중요합니다 / 알아두면 좋습니다” 식의 추상적 일반론만으로 채우지 마세요.
- raw_content 에서 직접 인용할 때는 짧게 (한 문장 이내) + 본인 표현으로 풀어쓰기.

[ 이미지 사용 규칙 — 강제 ]
사용자가 제공한 이미지마다 ‘role’ 이 명시돼 있습니다. 반드시 그 role 에 맞는 섹션에 배치하세요.
  · role="concept"     → 첫 번째 핵심 섹션(개요) 직후
  · role="example"     → 실제 사례 / 활용 시나리오 섹션 안
  · role="comparison"  → 비교 또는 주의사항 섹션 안
  · role="summary"     → 마지막 정리 섹션 직전 또는 직후

각 이미지는 다음 마크업으로:
  <figure>
    <img src="제공_URL_그대로" alt="제공_alt_그대로" data-filename="제공_filename" data-role="제공_role" loading="lazy" />
    <figcaption>제공된 figcaption 또는 본인이 자연스럽게 쓴 한 줄 설명</figcaption>
  </figure>

규칙:
A. src 에는 ‘제공된 URL’ 만. 새 URL 만들어내지 마세요.
B. 자동 일괄 추가가 아니라 본문 흐름 안 정확한 위치에 배치.
C. 이미지 직전·직후 단락에 그 이미지가 무엇을 보여주는지 한 문장이라도 언급해 자연스럽게 연결.

[ 출력 형식 ]
- <h1> 으로 시작하는 순수 HTML 만 출력. 코드펜스(```) 금지. 설명 문구 금지.
- 마지막에 메타 디스크립션이나 별도 주석 추가하지 말고 HTML 만.
"""


def _build_system_prompt(profile: DomainProfile, keyword: str) -> str:
    """도메인 프로파일을 받아 ‘이 키워드에 특화된’ 시스템 프롬프트 조립."""
    intents = "\n".join(f"   - {x}" for x in profile.search_intents)
    sections = "\n".join(
        f"▶ <h2> {title}\n   필수 요소: {req}"
        for title, req in zip(profile.section_titles, profile.section_requirements)
    )
    extra_kw = (", ".join(profile.extra_keywords)
                if profile.extra_keywords else "(자유롭게 본문 어휘 사용)")

    return (
        ANTHROPIC_SYSTEM_PROMPT_BASE
        + f"\n\n[ 이 키워드의 도메인 — 매우 중요 ]\n"
          f"키워드: {keyword}\n"
          f"도메인: {profile.name_ko}\n"
          f"도메인 정의: {profile.description_ko}\n\n"
          f"[ 독자의 검색 의도 — 이 글이 답해야 할 질문들 ]\n"
          f"{intents}\n\n"
          f"[ 글 구조 — 도메인에 맞춘 섹션을 반드시 따르세요 ]\n"
          f"{sections}\n\n"
          f"[ 본문에 자연스럽게 녹여야 할 도메인 어휘 ]\n{extra_kw}\n\n"
          f"[ 글 작성 규칙 ]\n"
          f"- <h1> 제목은 키워드를 자연스럽게 포함하고 클릭 후킹(숫자/의문형/약속형 중 하나)\n"
          f"- 도입부 1문단(80~120자)에서 ‘이 글로 무엇을 얻는가’ 분명히\n"
          f"- 각 <h2> 섹션은 위 ‘필수 요소’를 반드시 포함, 최소 2~3 문단\n"
          f"- 각 섹션에 구체적 팁/사례/수치/이름을 최소 2개 이상\n"
          f"- 마지막 섹션은 ‘다음에 무엇을 할지’ 행동 유도로 마무리\n"
    )


class AnthropicWriter(BaseWriter):
    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None,
                 *, model: Optional[str] = None,
                 max_tokens: int = 4096, temperature: float = 0.7,
                 timeout: int = 60):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        # infra-5: env / config 우선, 없으면 코드 default
        self.model = (
            model
            or os.environ.get("OSMU_ANTHROPIC_MODEL_WRITER")
            or "claude-sonnet-4-6"
        )
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key)

    def _call_anthropic(self, system: str, user: str) -> str:
        try:
            import requests
        except ImportError as e:
            raise RuntimeError("requests 미설치") from e

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        return "".join(
            blk.get("text", "")
            for blk in data.get("content", [])
            if blk.get("type") == "text"
        ).strip()

    def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적"):
        if not self.has_credentials:
            raise RuntimeError("ANTHROPIC_API_KEY 가 설정돼 있지 않습니다.")

        sources = sources or []
        norm_images: List[ImageItem] = []
        for img in (images or []):
            if isinstance(img, ImageItem):
                norm_images.append(img)
            elif isinstance(img, str):
                norm_images.append(ImageItem(url=img, filename="", alt=keyword))

        # 도메인 분류 → 시스템 프롬프트 도메인 적응
        profile = profile_for(keyword)
        system_prompt = _build_system_prompt(profile, keyword)
        log.info("[writer.anthropic] keyword='%s' domain=%s",
                  keyword, profile.domain.value)

        snippet = (raw_content or "")[:6000]
        image_brief = _format_image_brief(norm_images)
        sources_block = "\n".join(f"- {u}" for u in sources) if sources else "(없음)"

        user_prompt = (
            f"타겟 키워드: {keyword}\n"
            f"도메인: {profile.name_ko} ({profile.domain.value})\n"
            f"글 톤: {tone}\n\n"
            f"━━ [ 1. raw_content — 반드시 이 내용을 본인 표현으로 재구성·확장하세요 ] ━━\n"
            f"{snippet}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"[ 2. 출처 URL ]\n{sources_block}\n\n"
            f"[ 3. 제공 이미지 — 각 이미지의 role 에 맞춰 정확한 섹션에 배치 ]\n{image_brief}\n\n"
            f"[ 작성 지침 ]\n"
            f"- 위 raw_content 를 ‘재료’로 삼아 시스템 프롬프트의 도메인 섹션 구조 그대로 작성.\n"
            f"- 각 섹션 ‘필수 요소’ 를 빠뜨리지 말 것. 추상적 일반론으로 채우지 말 것.\n"
            f"- raw_content 에 도메인 정보가 부족하면 일반 상식을 활용하되, "
            f"가짜 통계·출처 미상 인용 금지.\n"
            f"- 절대 금지 표현 사용 금지.\n"
            f"- 이미지는 role 매핑대로 정확한 섹션에 <figure><img/></figure> 로 삽입."
        )

        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                html = self._call_anthropic(system_prompt, user_prompt)
                if not html or "<h1" not in html.lower():
                    raise RuntimeError("응답 형식 위반 — <h1> 누락")
                html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html.strip(),
                                flags=re.IGNORECASE)
                html = repair_missing_images(html, norm_images)
                return html
            except Exception as e:
                last_err = e
                log.warning("[writer.anthropic] %d차 시도 실패: %s", attempt, e)
                time.sleep(1.5)

        raise RuntimeError(f"Anthropic 호출 최종 실패: {last_err}")

    # ── v13 진입점 — 청사진 + facts 기반 ────────────────
    def write_from_blueprint(self, blueprint, normalized_sources=None, *,
                              images=None, tone="전문적"):
        """v13 spec 3.5: collector 청사진을 ‘충실히 HTML 변환’.

        - fact_based 단락은 facts 만 컨텍스트로 (raw 소스 비노출).
        - llm_generated 단락은 keyword + core_message + 글 맥락만으로 자유 생성.
        - commercial_elements 는 본문에 자연스럽게 녹이도록 안내.
        """
        if not self.has_credentials:
            raise RuntimeError("ANTHROPIC_API_KEY 가 설정돼 있지 않습니다.")

        norm_images: List[ImageItem] = []
        for img in (images or []):
            if isinstance(img, ImageItem):
                norm_images.append(img)
            elif isinstance(img, str):
                norm_images.append(ImageItem(url=img, filename="", alt=blueprint.keyword))

        # 단락 타입별 컨텍스트 구성
        para_blocks: List[str] = []
        for p in blueprint.paragraphs:
            head = (
                f"### 단락 {p.section_index} — {p.title}\n"
                f"  · type: {p.paragraph_type}\n"
                f"  · core_message: {p.description or '(없음)'}\n"
            )
            if p.paragraph_type == "fact_based" and normalized_sources is not None:
                facts = []
                if hasattr(normalized_sources, "sources_by_section"):
                    facts = normalized_sources.sources_by_section.get(p.section_index, [])
                elif isinstance(normalized_sources, dict):
                    facts = (normalized_sources.get(p.section_index, []) or
                              normalized_sources.get(str(p.section_index), []))
                if facts:
                    head += "  · facts (이 단락에서만 인용):\n"
                    for f in facts[:6]:
                        txt = (getattr(f, "fact_text", None)
                                or (f.get("fact_text", "") if isinstance(f, dict) else ""))
                        if txt:
                            head += f"      - {txt}\n"
                else:
                    head += "  · facts: (없음 — 일반 상식으로 보강)\n"
            else:
                head += "  · facts: (해당 없음 — 자유 생성 단락)\n"
            para_blocks.append(head)

        ce = blueprint.commercial_elements
        commercial_brief = (
            f"  · 추천: {', '.join(ce.recommendations[:5]) or '(없음)'}\n"
            f"  · 비교축: {', '.join(ce.comparison_points[:5]) or '(없음)'}\n"
            f"  · CTA 후보: {', '.join(ce.cta_candidates[:5]) or '(없음)'}\n"
        )
        image_brief = _format_image_brief(norm_images)

        profile = profile_for(blueprint.keyword)
        system_prompt = _build_system_prompt(profile, blueprint.keyword)
        log.info("[writer.anthropic.v13] keyword='%s' domain=%s sections=%d",
                  blueprint.keyword, profile.domain.value, len(blueprint.paragraphs))

        user_prompt = (
            f"타겟 키워드: {blueprint.keyword}\n"
            f"도메인: {profile.name_ko}\n"
            f"글 톤: {tone}\n"
            f"타겟 독자: {blueprint.target_reader.persona} "
            f"(레벨={blueprint.target_reader.knowledge_level}, "
            f"의도={blueprint.target_reader.primary_intent})\n\n"
            f"━━ [ 글 메타 — 그대로 사용할 것 ] ━━\n"
            f"제목(h1): {blueprint.title}\n"
            f"도입문: {blueprint.intro}\n"
            f"짧은 결론: {blueprint.short_conclusion}\n\n"
            f"━━ [ 단락 청사진 — 순서·제목·타입 그대로 따를 것 ] ━━\n"
            + "\n".join(para_blocks) + "\n"
            f"━━ [ 수익 포인트 (commercial) — 본문에 자연스럽게 녹일 것 ] ━━\n"
            + commercial_brief + "\n"
            f"━━ [ 이미지 — role 매핑대로 정확한 섹션에 ] ━━\n{image_brief}\n\n"
            f"[ 작성 지침 ]\n"
            f"- fact_based 단락은 위에 제공된 facts 만 인용. 추가 ‘없는 사실’ 만들지 말 것.\n"
            f"- llm_generated 단락은 keyword + core_message + 전체 글 맥락만으로 자연 생성.\n"
            f"- 단락 순서/제목은 청사진 그대로. 5~7개 h2 유지.\n"
            f"- 추천/비교/CTA 는 본문 중·후반에 자연스럽게 박을 것 (광고 티 안 나게).\n"
            f"- 절대 금지 표현 사용 금지. 가짜 통계·출처 미상 인용 금지."
        )

        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                html = self._call_anthropic(system_prompt, user_prompt)
                if not html or "<h1" not in html.lower():
                    raise RuntimeError("응답 형식 위반 — <h1> 누락")
                html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html.strip(),
                                flags=re.IGNORECASE)
                html = repair_missing_images(html, norm_images)
                return html
            except Exception as e:
                last_err = e
                log.warning("[writer.anthropic.v13] %d차 실패: %s", attempt, e)
                time.sleep(1.5)
        raise RuntimeError(f"Anthropic 호출 최종 실패: {last_err}")


# ── Heuristic Writer (폴백) — 도메인 적응형 ─────────
# Generator 가 보내는 ‘fallback 시드’ 를 본문에 그대로 노출하지 않기 위한 마커.
FALLBACK_SEED_MARKER = "__OSMU_FALLBACK_SEED__"


class HeuristicWriter(BaseWriter):
    """LLM 자격증명 없을 때 폴백 — 도메인 프로파일 기반 템플릿 생성."""

    name = "heuristic"

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        if not text:
            return []
        parts = re.split(r"(?<=[\.\?\!])\s+|\n+", text)
        return [s.strip() for s in parts if len(s.strip()) > 10]

    @staticmethod
    def _chunks(seq: List[str], size: int) -> List[List[str]]:
        return [seq[i:i + size] for i in range(0, len(seq), size)]

    @staticmethod
    def _is_fallback_seed(raw: str) -> bool:
        """raw_content 가 Generator 의 fallback 시드인지 식별."""
        if not raw:
            return True
        return FALLBACK_SEED_MARKER in raw

    def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적"):
        sources = sources or []
        norm_images: List[ImageItem] = []
        for img in (images or []):
            if isinstance(img, ImageItem):
                norm_images.append(img)
            elif isinstance(img, str):
                norm_images.append(ImageItem(url=img, filename="", alt=keyword))

        kw = (keyword or "").strip() or "키워드"
        # 도메인 분류 → 프로파일별 다른 본문 생성
        profile = profile_for(kw)

        # raw_content 가 ‘fallback 시드’ 면 본문에 그대로 인용하지 않는다 (요구: 시드 노출 금지)
        snippet = "" if self._is_fallback_seed(raw_content) else (raw_content or "").strip()
        sentences = self._split_sentences(snippet)
        section_chunks = self._chunks(sentences, max(3, (len(sentences) // 4) or 3))
        while len(section_chunks) < len(profile.section_titles):
            section_chunks.append([])

        # 도메인별 도입부 — fallback 시드 노출 금지
        intro = self._domain_intro(profile, kw)

        parts = [
            f"<h1>{kw} — {self._domain_title_suffix(profile)}</h1>",
            f"<p>{intro}</p>",
        ]

        for idx, (title, requirement) in enumerate(
            zip(profile.section_titles, profile.section_requirements)
        ):
            parts.append(f"<h2>{title}</h2>")
            chunk = section_chunks[idx] if idx < len(section_chunks) else []
            if chunk:
                parts.append("<p>" + " ".join(chunk[:3]) + "</p>")
                if len(chunk) > 3:
                    parts.append("<p>" + " ".join(chunk[3:6]) + "</p>")
            # raw_content 가 부족해도 도메인별 placeholder 단락 추가
            for body in self._domain_section_filler(profile, kw, idx, requirement):
                parts.append(f"<p>{body}</p>")
            # 활용 섹션엔 리스트 추가
            if "초보자" in title or "활용" in title or "처음 시작" in title:
                parts.append(self._domain_list(profile, kw))

        if sources:
            parts.append(
                "<p><small>참고: " +
                ", ".join(f'<a href="{u}">출처 {i+1}</a>' for i, u in enumerate(sources)) +
                "</small></p>"
            )

        html = "\n".join(parts)
        return _post_insert_images(html, norm_images)

    # ── 도메인별 헬퍼 ──────────────────────────────
    @staticmethod
    def _domain_intro(profile: DomainProfile, kw: str) -> str:
        # 도메인별 ‘why this article matters’ 한 단락
        d = profile.domain.value
        if d == "game":
            return (f"{kw} 을(를) 막 시작하려는 분이라면, 처음 한 시간 안에 알아두면 "
                    f"훨씬 덜 헤매는 정보가 있습니다. 이 글에서는 게임의 큰 그림부터 "
                    f"실전에서 바로 쓸 수 있는 팁까지 단계별로 정리합니다.")
        if d == "finance":
            return (f"{kw} 에 관심이 생겼다면, 무엇보다 먼저 ‘구조’ 와 ‘리스크’ 를 "
                    f"이해해야 손해를 줄일 수 있습니다. 이 글에서는 작동 원리부터 "
                    f"실제 진입 전 체크포인트까지 한 번에 정리합니다.")
        if d == "diet":
            return (f"{kw} 을(를) 시도하기 전에, 어떤 원리로 작동하고 누구에게 잘 맞는지 "
                    f"알아두면 시행착오를 크게 줄일 수 있습니다. 실제 식단·루틴 예시와 "
                    f"꾸준히 하기 위한 팁까지 함께 정리합니다.")
        if d == "it":
            return (f"{kw} 을(를) 검토 중이라면, 스펙 외에 ‘어떤 사용자에게 어떤 점이 "
                    f"좋은지’ 가 가장 중요합니다. 이 글에서는 핵심 기능부터 비슷한 대안 "
                    f"비교, 구매 전 체크포인트까지 정리합니다.")
        if d == "beauty":
            return (f"{kw} 을(를) 살까 고민 중이라면, 성분과 피부 타입별 적합도를 먼저 "
                    f"확인해야 후회 없이 쓸 수 있습니다. 사용 순서와 흔한 부작용까지 "
                    f"함께 정리합니다.")
        if d == "travel":
            return (f"{kw} 을(를) 계획하고 있다면, 코스·숙소·예산을 먼저 그려두면 "
                    f"여행 전체 만족도가 달라집니다. 일정 길이별 추천 코스와 시즌 팁까지 "
                    f"한 번에 정리했습니다.")
        if d == "food":
            return (f"{kw} 을(를) 처음 만들거나 좀 더 잘 만들고 싶다면, 재료 비율과 "
                    f"순서만 잘 잡아도 결과가 확실히 달라집니다. 기본 레시피부터 "
                    f"실수 회피까지 함께 정리합니다.")
        return (f"{kw} 을(를) 처음 접하는 독자도 핵심을 빠르게 이해할 수 있도록 "
                f"개념·활용·주의사항·정리 흐름으로 한 글에 모았습니다.")

    @staticmethod
    def _domain_title_suffix(profile: DomainProfile) -> str:
        d = profile.domain.value
        if d == "game":
            return "한눈에 보는 입문 가이드"
        if d == "finance":
            return "구조부터 진입 전 체크리스트까지"
        if d == "diet":
            return "원리부터 실천 루틴까지"
        if d == "it":
            return "스펙·실사용·구매 가이드 한 번에"
        if d == "beauty":
            return "성분·피부타입·루틴 정리"
        if d == "travel":
            return "코스·숙소·예산 가이드"
        if d == "food":
            return "기본 레시피와 실패 회피 팁"
        return "핵심 가이드 한 번에 정리"

    @staticmethod
    def _domain_section_filler(profile: DomainProfile, kw: str,
                                idx: int, requirement: str) -> List[str]:
        """raw_content 가 부족할 때 섹션을 채울 한국어 placeholder 단락들.

        도메인별로 각 섹션 idx 가 ‘무엇’에 해당하는지 알고, 그에 맞는 한 단락을 만든다.
        ‘추상적 일반론’ 으로 가지 않도록 도메인 어휘를 적극 사용.
        """
        d = profile.domain.value

        if d == "game":
            templates = [
                # 0. 게임 기본 개요
                [f"{kw} 은(는) 진영(예: 살인마/생존자)이 분리된 비대칭 게임이거나, "
                 f"클래스·캐릭터를 골라 협동·경쟁하는 형태로 분류할 수 있습니다. "
                 f"가장 먼저 ‘승리 조건’ 과 ‘핵심 시스템(예: 매칭 시간, 라운드 길이)’ 을 파악하면 "
                 f"이후 캐릭터 선택과 전략이 훨씬 쉬워집니다."],
                # 1. 초보자 필수 가이드
                [f"{kw} 을(를) 처음 켰다면, 먼저 튜토리얼 또는 봇 매치를 한두 번 돌려 "
                 f"기본 조작과 UI 를 익히는 것이 가장 안전합니다. 그 다음에는 ‘단순한 캐릭터’ 또는 "
                 f"‘기본 빌드’ 로 시작해, 시스템 이해도가 쌓일 때마다 새로운 캐릭터·모드를 시도하세요."],
                # 2. 핵심 플레이 팁과 전략
                [f"{kw} 의 실전에서 가장 큰 차이를 만드는 건 ‘위치 선정’ 과 ‘리소스 우선순위’ 입니다. "
                 f"맵 정보·소리·아이템 위치 같은 간접 단서를 익혀두면, 같은 캐릭터를 써도 결과가 크게 달라집니다.",
                 f"또한 캐릭터별 핵심 스킬의 쿨다운과 사용 타이밍을 외워두면, 한 번의 합에서 이기는 확률이 크게 올라갑니다."],
                # 3. 자주 하는 실수와 회피법
                [f"초보가 가장 자주 하는 실수는 ‘싸움부터 거는 것’ 입니다. {kw} 은(는) 직접 교전보다 "
                 f"목표(생존·구출·점령) 를 먼저 끝내는 쪽이 이기는 구조이며, 무리한 교전은 시간만 낭비됩니다.",
                 f"또 하나는 캐릭터를 너무 자주 바꾸는 것입니다. 한 캐릭터를 30~50판 정도 깊이 익히면 "
                 f"그제야 그 캐릭터의 진짜 강점이 보이기 시작합니다."],
                # 4. 추천 세팅·빌드·다음 단계
                [f"입문 단계에서는 ‘안정형’ 빌드 — 즉 생존력을 높이거나 정보를 얻는 옵션 — 가 가장 효율적입니다. "
                 f"한 시즌이 끝날 때마다 전적을 돌아보고, 부족했던 부분(예: 정보 수집 / 위치 선정 / 합 타이밍) "
                 f"하나만 정해서 다음 시즌에 보완하는 식으로 성장 사이클을 만들면 좋습니다."],
            ]
        elif d == "finance":
            templates = [
                [f"{kw} 은(는) 기초 자산이 무엇이고, 어디에서 거래되며, 누가 운용하는지가 핵심입니다. "
                 f"이 세 가지를 먼저 정리해두면 이후 수익률·세금·수수료를 비교할 때 훨씬 명확해집니다."],
                [f"수익률은 ‘평균’ 보다 ‘변동 범위’ 를 함께 봐야 합니다. 최근 3~5년 동안 가장 좋았던 해와 "
                 f"가장 나빴던 해의 수익률 폭을 함께 확인하면, 본인이 감내할 수 있는 리스크인지 판단이 가능해집니다."],
                [f"비슷한 대안과 비교할 때는 ‘수익률·리스크·세금·접근성’ 4가지 축으로 비교하면 충분합니다. "
                 f"한 축만 보고 결정하면 다른 축에서 손해를 보기 쉽습니다."],
                [f"초보 투자자라면 ‘시작 금액을 적게, 분할로 진입’ 이 가장 안전합니다. "
                 f"한 번에 큰 금액을 넣지 말고, 정해진 일정에 따라 매수하면 평단가가 자연스럽게 평균에 수렴합니다."],
                [f"흔한 실수는 단기 수익률에 흔들려 자주 사고파는 것이며, 거래 비용·세금이 누적되면 장기 수익률을 크게 갉아먹습니다. "
                 f"체크리스트로 ‘진입 사유 / 청산 조건 / 손절 기준’ 을 미리 정해두는 것이 좋습니다."],
            ]
        elif d == "diet":
            templates = [
                [f"{kw} 은(는) 일반적으로 ‘칼로리 적자’ 또는 ‘혈당 안정화’ 를 통해 체중·체지방 변화를 만듭니다. "
                 f"한 달 -2kg 정도가 무리 없는 일반적 범위이며, 이보다 빠른 감량은 근손실 위험이 커집니다."],
                [f"하루 식단을 예로 들면, 아침에 단백질 20g + 식이섬유, 점심에 균형 잡힌 한식, 저녁에 가벼운 식사 "
                 f"패턴이 무난합니다. 운동은 주 3~4회, 회당 30~40분 정도부터 시작하면 부담이 적습니다."],
                [f"흔한 식품 → 대체식품 매칭이 도움 됩니다 — 흰 쌀 → 잡곡밥, 라면 → 두부면, "
                 f"단 음료 → 무가당 차/탄산수, 디저트 → 그릭요거트+베리 같은 식입니다."],
                [f"임산부·당뇨·심혈관 질환 등 기저질환이 있다면 단순 다이어트가 아니라 의료적 가이드가 우선입니다. "
                 f"체중이 갑자기 빠지거나 에너지가 너무 떨어지면 일시적으로 칼로리 섭취를 늘려야 합니다."],
                [f"꾸준히 하기 위한 핵심은 ‘완벽보다 회복력’ 입니다. 하루 어겼더라도 다음 끼니부터 다시 정상 패턴으로 "
                 f"돌아오면 되며, 1주차 / 4주차에 사진과 체중을 함께 기록하면 변화를 객관적으로 확인할 수 있습니다."],
            ]
        elif d == "it":
            templates = [
                [f"{kw} 의 핵심 기능을 3가지로 압축하면 ‘성능·생태계·가격’ 의 균형입니다. "
                 f"같은 가격대 경쟁 제품 대비 어디서 차별화되는지 — 예: 배터리 시간, 화면 품질, 호환 액세서리 — 를 명확히 짚으면 결정이 쉬워집니다."],
                [f"라인업은 보통 입문/메인/프로 세 단계로 나뉘며, 각각 가격대와 출시 연도가 다릅니다. "
                 f"같은 라인업 안에서도 작년 모델은 가격이 크게 떨어지므로 ‘가격 대비 만족도’ 측면에서 충분히 매력적인 선택지가 됩니다."],
                [f"실사용 시나리오로 보면 — 대학생이라면 가벼운 무게와 배터리, 디자이너라면 색재현과 화면 크기, "
                 f"개발자라면 메모리·발열 — 같이 페르소나별로 우선순위가 다릅니다."],
                [f"비교 대상과의 차이는 단순 스펙표가 아니라 ‘생태계’ 까지 봐야 합니다. 액세서리·소프트웨어·AS 망 같은 요소가 "
                 f"수년간의 사용 경험을 좌우합니다."],
                [f"구매 전 체크할 7가지: 보증 기간 / 무이자 할부 / 액세서리 호환 / 트레이드인 가능성 / 색상 옵션 / "
                 f"실 발열·소음 후기 / 본인 환경의 SW 호환성. 첫 1주일은 데이터 이전과 기본 세팅에 집중하는 것이 좋습니다."],
            ]
        elif d == "beauty":
            templates = [
                [f"{kw} 의 핵심 성분은 보습/진정/미백 등 어떤 효과를 노리는지에 따라 달라집니다. "
                 f"히알루론산은 보습, 나이아신아마이드는 미백+진정 — 식으로 성분과 효과를 매칭해 두면 선택이 쉬워집니다."],
                [f"건성/지성/복합/민감성 4타입별로 적합한 제형이 다릅니다. 지성은 가벼운 토너·세럼 위주, "
                 f"건성은 크림과 오일 — 같이 본인 피부에 맞춰야 트러블이 줄어듭니다."],
                [f"기본 루틴은 ‘세안 → 토너 → 에센스/세럼 → 크림 → (아침엔) 선크림’ 순서이며, "
                 f"각 단계 사이 1~2분 간격을 두면 흡수가 더 잘 됩니다."],
                [f"가격대별 추천을 보면, 입문은 드럭스토어 브랜드, 중간대는 한국 더마 브랜드, 프리미엄은 럭셔리 라인입니다. "
                 f"신제품일수록 패치테스트가 필수이며, 성분표에서 본인이 알레르기 있는 성분을 확인하는 것이 안전합니다."],
                [f"흔한 부작용으로는 따끔거림, 붉어짐, 트러블 폭발이 있습니다. 새 제품을 추가할 때는 한 번에 하나씩, "
                 f"최소 2주 이상 사용해본 뒤 효과·부작용을 판단하는 것이 좋습니다."],
            ]
        elif d == "travel":
            templates = [
                [f"{kw} 은(는) 위치·분위기·대표 명소가 명확한 여행지입니다. 무엇을 보러 가는지를 "
                 f"먼저 정해두면 코스 짜기가 훨씬 쉬워집니다."],
                [f"1박 2일이라면 핵심 명소 2~3곳에 집중, 2박 3일이면 외곽 일정까지 포함하는 식으로 "
                 f"일정 길이별로 우선순위를 다르게 잡는 것이 좋습니다."],
                [f"숙소는 위치 / 가격 / 분위기 중 두 가지를 우선시하는 것이 일반적이며, "
                 f"현지 음식은 평이 높은 곳 1군데 + 모험으로 1군데 정도 섞으면 만족도가 높습니다."],
                [f"1인 평균 예산은 시즌과 항공권에 크게 좌우됩니다. 비수기에는 동일 일정이 30~50% 저렴해질 수 있어, "
                 f"가능하다면 비수기 출발을 우선 검토해보세요."],
                [f"현지 매너·환전·통신은 사전에 점검해두면 도착 직후 시행착오가 줄어듭니다. "
                 f"특히 환전은 출국 전 50~70%만 환전하고, 나머지는 현지 ATM/카드를 활용하는 방식이 가성비가 좋습니다."],
            ]
        elif d == "food":
            templates = [
                [f"{kw} 은(는) 재료 비율과 조리 순서만 잘 잡으면 집에서도 충분히 가게 못지않은 결과가 나옵니다. "
                 f"먼저 ‘기본 비율’ 을 외워두면 응용이 자유로워집니다."],
                [f"기본 레시피는 1인분 기준으로 재료와 시간을 명시해두면 실수가 줄어듭니다. "
                 f"강불·중불·약불의 시점만 정확히 지켜도 결과가 크게 달라집니다."],
                [f"대체 재료가 있으면 활용도가 훨씬 높아집니다. 비싼 재료를 쓸 수 없다면 "
                 f"향과 풍미가 비슷한 흔한 재료로 충분히 대체 가능합니다."],
                [f"흔한 실수는 ‘간을 마지막에 한 번에 넣기’ 입니다. 처음에 약하게 잡고 단계마다 확인하면 "
                 f"짜거나 싱겁게 되는 일이 거의 없습니다."],
                [f"보관은 밀폐 후 냉장 1~2일이 적당하며, 재가열할 때는 처음 조리 때보다 약한 불에서 천천히 데워야 "
                 f"식감이 살아납니다. 어울리는 사이드/술 한 잔 곁들이면 만족도가 두 배가 됩니다."],
            ]
        else:
            templates = [
                [f"{kw} 은(는) 정의와 활용 맥락을 함께 이해해야 흐름이 잡힙니다. "
                 f"정의 한 문장 + 왜 중요한지(배경) + 간단한 예시 1개를 차례로 짚으면 머리에 남기 쉽습니다."],
                [f"실제 환경에서 {kw} 을(를) 어떻게 쓰는지가 더 와닿습니다. 처음 도입할 때 부담을 줄이는 방식과 "
                 f"익숙해진 뒤 효율을 높이는 방식 — 두 가지 시나리오를 함께 익혀두면 좋습니다."],
                [f"가장 흔한 실수는 한 가지 지표만 보고 결정하는 것입니다. 비슷한 대안과의 차이를 함께 짚어두면 "
                 f"자신의 환경에 맞는 선택을 할 수 있습니다."],
                [f"여기까지를 본인 환경에 어떻게 적용할지 ‘첫 번째 행동’ 한 가지를 정해보세요. "
                 f"작은 실험을 빠르게 해보는 것만으로도 글에서 읽은 내용보다 훨씬 많은 인사이트를 얻습니다."],
            ]

        # idx 가 templates 범위를 벗어나면 마지막 템플릿 재사용
        if idx < len(templates):
            return templates[idx]
        return templates[-1]

    @staticmethod
    def _domain_list(profile: DomainProfile, kw: str) -> str:
        """도메인별 핵심 리스트 한 덩어리."""
        d = profile.domain.value
        if d == "game":
            return ("<ul>"
                    f"<li>첫 매치 전에 — 튜토리얼/봇 매치로 조작 익히기</li>"
                    f"<li>1~10시간 — 단일 캐릭터로 시스템 이해 쌓기</li>"
                    f"<li>10시간 이후 — 빌드·맵 정보·합 타이밍까지 공부</li>"
                    "</ul>")
        if d == "finance":
            return ("<ul>"
                    f"<li>진입 전 — 기초자산·운용사·세금 구조 확인</li>"
                    f"<li>진입 시 — 분할 매수 + 손절 기준 명문화</li>"
                    f"<li>유지 중 — 분기 1회 리밸런싱·수수료 점검</li>"
                    "</ul>")
        if d == "diet":
            return ("<ul>"
                    f"<li>1주차 — 패턴 정착 (식사 시간·운동 시간 고정)</li>"
                    f"<li>4주차 — 사진·체중·근력 기록으로 객관 비교</li>"
                    f"<li>12주차 — 지속 가능한 ‘일상 모드’ 로 전환</li>"
                    "</ul>")
        return ("<ul>"
                f"<li>처음 — 작은 단위로 시도, 결과 보면서 확장</li>"
                f"<li>중간 — 자동화/체계화로 반복 작업 줄이기</li>"
                f"<li>다음 — 데이터/측정 기반 의사결정</li>"
                "</ul>")
