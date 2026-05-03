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


# ── Anthropic 시스템 프롬프트 — 내용 중심 재설계 ──────
ANTHROPIC_SYSTEM_PROMPT = """당신은 한국어 SEO 블로그 전문 작가입니다.
사용자가 제공하는 ‘raw_content’ 는 실제로 검색·크롤링된 자료입니다.
임의로 일반론을 만들어내지 말고, 이 자료를 본인 표현으로 재구성·확장해 정보 밀도 높은 글을 작성하세요.

[ 절대 금지 표현 ]
다음 표현 또는 그와 비슷한 의미의 문장은 본문에 절대 포함하지 마세요:
  · "외부 검색이 어렵", "데이터가 부족", "정보가 충분하지 않", "기본 가이드"
  · "참고 자료가 제한적", "임시로 작성", "추정", "일시적으로"
이런 표현은 콘텐츠 신뢰도를 떨어뜨립니다. 자료가 부족하면 자연스럽게 본인의 일반 지식으로 채우되,
가짜 통계나 출처 미상의 인용은 만들지 마세요.

[ 글 구조 — 섹션별 ‘내용’ 강제 규칙 ]

▶ <h1> 제목
   - 타겟 키워드를 자연스럽게 포함
   - 클릭하고 싶은 후킹 (숫자 / 의문형 / 약속형 중 하나)

▶ 도입부 <p>  (1문단, 80~120자)
   - “왜 지금 이 주제를 알아야 하는가?” 또는 “이 글에서 무엇을 얻는가?”

▶ <h2> 1. {keyword} 의 기본 개념과 핵심
   - 정의 한 문장 + 왜 중요한지(배경) + 간단한 예시 1개
   - 최소 2~3 문단. 각 문단은 60자 이상 + 구체적 설명/사례 포함.

▶ <h2> 2. {keyword} 활용 방법 — 실제 시나리오
   - 최소 2개 이상의 구체적 사용 사례
   - 각 사례에 ‘누가/언제/어떻게/결과’ 중 최소 3가지 요소 포함
   - 가능하면 raw_content 의 표현·수치를 활용
   - 최소 2~3 문단

▶ <h2> 3. {keyword} 선택·판단 시 주의사항
   - 자주 하는 실수 또는 오해 1~2개
   - 비교 기준이 있으면 함께 (예: A vs B, 어떤 경우에 어느 쪽?)
   - 최소 2~3 문단

▶ <h2> 4. 핵심 정리
   - <ul><li> 3~5개로 핵심 포인트 정리
   - 마무리 한 문단 — 행동 유도(다음 단계 / 체크포인트)

[ 문단 품질 기준 ]
- 각 <p> 는 60자 이상.
- 각 문단은 ‘구체적 설명’ 또는 ‘실제 사례’ 또는 ‘수치/기준’ 중 최소 1가지를 포함.
- “중요합니다 / 알아두면 좋습니다” 식의 추상적 일반론만으로 채우지 마세요.
- raw_content 에서 직접 인용할 때는 짧게 (한 문장 이내) + 본인 표현으로 풀어쓰기.

[ 이미지 사용 규칙 — 강제 ]
사용자가 제공한 이미지마다 ‘role’ 이 명시돼 있습니다. 반드시 그 role 에 맞는 섹션에 배치하세요.
  · role="concept"     → ‘1. 기본 개념’ 섹션 마지막 단락 직후
  · role="example"     → ‘2. 활용 방법’ 섹션 첫 사례 직후
  · role="comparison"  → ‘3. 주의사항’ 섹션 안
  · role="summary"     → ‘4. 핵심 정리’ 직전 또는 직후

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


class AnthropicWriter(BaseWriter):
    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None,
                 *, model: str = "claude-sonnet-4-5",
                 max_tokens: int = 4096, temperature: float = 0.7,
                 timeout: int = 60):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model
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
        # images 가 ImageItem 리스트인지 확인 — str 리스트가 들어오면 변환 (후방호환)
        norm_images: List[ImageItem] = []
        for img in (images or []):
            if isinstance(img, ImageItem):
                norm_images.append(img)
            elif isinstance(img, str):
                norm_images.append(ImageItem(url=img, filename="", alt=keyword))

        snippet = (raw_content or "")[:6000]
        image_brief = _format_image_brief(norm_images)
        sources_block = "\n".join(f"- {u}" for u in sources) if sources else "(없음)"

        user_prompt = (
            f"타겟 키워드: {keyword}\n"
            f"글 톤: {tone}\n\n"
            f"━━ [ 1. raw_content — 반드시 이 내용을 본인 표현으로 재구성·확장하세요 ] ━━\n"
            f"{snippet}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"[ 2. 출처 URL ]\n{sources_block}\n\n"
            f"[ 3. 제공 이미지 — 각 이미지의 role 에 맞춰 정확한 섹션에 배치 ]\n{image_brief}\n\n"
            f"[ 작성 지침 요약 ]\n"
            f"- 위 raw_content 를 ‘재료’로 삼아 ‘기본 개념 → 활용 사례 → 주의사항 → 핵심 정리’ 순서로 글 작성.\n"
            f"- 각 섹션 최소 2~3 문단, 각 문단에 구체적 설명/사례/수치 중 1개 이상 포함.\n"
            f"- 절대 금지 표현 ('외부 검색이 어렵', '데이터 부족' 등) 사용 금지.\n"
            f"- 이미지는 role 매핑대로 정확한 섹션에 <figure><img/></figure> 로 삽입."
        )

        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                html = self._call_anthropic(ANTHROPIC_SYSTEM_PROMPT, user_prompt)
                if not html or "<h1" not in html.lower():
                    raise RuntimeError("응답 형식 위반 — <h1> 누락")
                # 코드펜스 정리
                html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html.strip(),
                                flags=re.IGNORECASE)
                # 보강: Claude 가 이미지를 빼먹은 경우 휴리스틱 보강
                html = repair_missing_images(html, norm_images)
                return html
            except Exception as e:
                last_err = e
                log.warning("[writer.anthropic] %d차 시도 실패: %s", attempt, e)
                time.sleep(1.5)

        raise RuntimeError(f"Anthropic 호출 최종 실패: {last_err}")


# ── Heuristic Writer (폴백) — 신뢰도 보강 ─────────────
class HeuristicWriter(BaseWriter):
    """LLM 자격증명 없을 때 폴백 — raw_content 기반 + 금지 표현 제거 + 다단 구조."""

    name = "heuristic"

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        if not text:
            return []
        # 한국어/영문 모두 — 종결부호 및 줄바꿈 기준
        parts = re.split(r"(?<=[\.\?\!])\s+|\n+", text)
        return [s.strip() for s in parts if len(s.strip()) > 10]

    @staticmethod
    def _chunks(seq: List[str], size: int) -> List[List[str]]:
        return [seq[i:i + size] for i in range(0, len(seq), size)]

    def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적"):
        sources = sources or []
        norm_images: List[ImageItem] = []
        for img in (images or []):
            if isinstance(img, ImageItem):
                norm_images.append(img)
            elif isinstance(img, str):
                norm_images.append(ImageItem(url=img, filename="", alt=keyword))

        kw = (keyword or "").strip() or "키워드"
        snippet = (raw_content or "").strip()
        sentences = self._split_sentences(snippet)
        # 4개 섹션에 분배
        section_size = max(3, (len(sentences) // 4) or 3) if sentences else 3
        section_chunks = self._chunks(sentences, section_size)
        # 부족하면 placeholder 문장으로 보강 (단, 금지 표현 안 쓰는 일반 안내)
        while len(section_chunks) < 4:
            section_chunks.append([])

        intro = (sentences[0]
                 if sentences
                 else f"{kw}을(를) 처음 접하는 독자도 핵심을 빠르게 이해할 수 있도록 정리한 가이드입니다.")

        parts = [
            f"<h1>{kw} — 핵심 가이드 한 번에 정리</h1>",
            f"<p>{intro}</p>",
        ]

        # 1. 기본 개념
        parts.append(f"<h2>1. {kw} 의 기본 개념과 핵심</h2>")
        s1 = section_chunks[0]
        if s1:
            parts.append("<p>" + " ".join(s1[:3]) + "</p>")
            if len(s1) > 3:
                parts.append("<p>" + " ".join(s1[3:6]) + "</p>")
        else:
            parts.append(
                f"<p>{kw} 은(는) 이름만으로는 모호하게 느껴질 수 있지만, 실제로는 분명한 정의와 "
                f"활용 방식을 가지고 있습니다. 이 글에서는 핵심 개념을 한 단락으로 정리하고, "
                f"왜 이 주제가 지금 시점에 의미 있는지 함께 살펴봅니다.</p>"
            )
            parts.append(
                f"<p>특히 {kw} 은(는) 처음 접할 때 비슷한 개념과 헷갈리기 쉬우므로, "
                f"공통점과 차이점을 함께 짚어두면 다음 섹션의 활용 방법을 이해하기가 한결 수월해집니다.</p>"
            )

        # 2. 활용 방법
        parts.append(f"<h2>2. {kw} 활용 방법 — 실제 시나리오</h2>")
        s2 = section_chunks[1]
        if s2:
            parts.append("<p>" + " ".join(s2[:3]) + "</p>")
            if len(s2) > 3:
                parts.append("<p>" + " ".join(s2[3:6]) + "</p>")
        else:
            parts.append(
                f"<p>실제 환경에서 {kw} 을(를) 어떻게 쓰는지가 더 와닿는 경우가 많습니다. "
                f"대표적으로 두 가지 시나리오를 살펴봅니다 — 첫째, 처음 도입할 때 부담을 줄이는 방식이고, "
                f"둘째, 어느 정도 익숙해진 뒤 효율을 높이는 방식입니다.</p>"
            )
            parts.append(
                f"<p>두 시나리오는 출발점이 다르지만, 공통적으로 ‘작게 시작해서 빠르게 검증’ 한다는 "
                f"흐름을 따릅니다. 처음에는 가장 단순한 형태로 적용해보고, 결과가 보이는 만큼 점진적으로 "
                f"확장하는 것이 안전합니다.</p>"
            )
        parts.append("<ul>"
                      f"<li>처음 도입할 때 — 적은 비용으로 작은 단위부터 시도</li>"
                      f"<li>익숙해진 뒤 — 자동화/체계화로 반복 작업 줄이기</li>"
                      f"<li>한 단계 더 — 데이터/측정 기반으로 의사결정 보강</li>"
                      "</ul>")

        # 3. 주의사항
        parts.append(f"<h2>3. {kw} 선택·판단 시 주의사항</h2>")
        s3 = section_chunks[2]
        if s3:
            parts.append("<p>" + " ".join(s3[:3]) + "</p>")
            if len(s3) > 3:
                parts.append("<p>" + " ".join(s3[3:6]) + "</p>")
        else:
            parts.append(
                f"<p>{kw} 을(를) 도입했다가 의도와 다른 결과가 나오는 경우는 대부분 ‘초기 가정’ 단계에서 "
                f"놓친 부분이 있을 때 발생합니다. 가장 흔한 실수는 한 가지 지표만 보고 결정하는 것이며, "
                f"이런 결정은 당장은 효율적으로 보여도 시간이 지날수록 비용이 누적됩니다.</p>"
            )
            parts.append(
                f"<p>또 하나 자주 헷갈리는 부분은 비슷한 대안과의 차이입니다. {kw} 은(는) 만능이 아니라 "
                f"적합한 상황과 그렇지 않은 상황이 분명히 있으며, 자신의 환경 — 규모, 인력, 예산, 시점 — "
                f"에 어느 쪽이 더 맞는지 비교해보고 선택하는 편이 후회를 줄여줍니다.</p>"
            )

        # 4. 핵심 정리
        parts.append("<h2>4. 핵심 정리</h2>")
        parts.append("<ul>"
                      f"<li>{kw} 은(는) 정의와 활용 맥락을 함께 이해해야 흐름이 잡힘</li>"
                      f"<li>처음에는 작은 단위로 시작해 결과를 보면서 점진적으로 확장</li>"
                      f"<li>한 가지 지표만 보지 말고 비교 기준을 함께 두기</li>"
                      f"<li>비슷한 대안과 차이를 짚고 자신의 환경에 맞춰 선택</li>"
                      "</ul>")
        parts.append(
            f"<p>여기까지의 내용을 바탕으로 본인 환경에서 {kw} 을(를) 어떻게 적용할지 "
            f"구체적인 첫 단계를 그려보세요. 작은 실험을 빠르게 해보는 것만으로도 글에서 읽은 내용보다 "
            f"훨씬 많은 인사이트를 얻을 수 있습니다.</p>"
        )

        if sources:
            parts.append(
                "<p><small>참고: " +
                ", ".join(f'<a href="{u}">출처 {i+1}</a>' for i, u in enumerate(sources)) +
                "</small></p>"
            )

        html = "\n".join(parts)
        return _post_insert_images(html, norm_images)
