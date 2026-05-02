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
def _format_image_brief(images: Sequence[ImageItem]) -> str:
    """프롬프트용 이미지 메타 요약."""
    lines = []
    for i, img in enumerate(images, 1):
        lines.append(
            f"  {i}. URL: {img.url}\n"
            f"     filename: {img.filename}\n"
            f"     추천 alt: \"{img.alt}\""
        )
    return "\n".join(lines)


def _post_insert_images(html: str, images: Sequence[ImageItem]) -> str:
    """휴리스틱 폴백 전용 — H2 사이에 이미지 분산 삽입.

    AnthropicWriter 는 사용하지 않는다 (요구사항: Claude 가 위치 결정).
    """
    if not images:
        return html
    img_html = lambda im: (
        f'<figure>'
        f'<img src="{im.url}" alt="{im.alt}" data-filename="{im.filename}" loading="lazy" />'
        f'</figure>'
    )
    parts = re.split(r"(<h2[^>]*>.*?</h2>)", html, flags=re.IGNORECASE | re.DOTALL)
    if len(parts) <= 1:
        for im in images:
            html += img_html(im)
        return html
    out = []
    img_idx = 0
    for chunk in parts:
        if chunk.lower().startswith("<h2") and img_idx < len(images):
            out.append(img_html(images[img_idx]))
            img_idx += 1
        out.append(chunk)
    while img_idx < len(images):
        out.append(img_html(images[img_idx]))
        img_idx += 1
    return "".join(out)


# ── HTML 검증 ─────────────────────────────────────────
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?>", re.IGNORECASE)
_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.IGNORECASE)
_ALT_RE = re.compile(r'alt=["\']([^"\']*)["\']', re.IGNORECASE)


def validate_html_structure(html: str, *, expected_image_count: int = 2) -> List[str]:
    """HTML 구조 검증 — 문제점 리스트 반환. 비면 OK."""
    issues: List[str] = []
    h = (html or "").lower()
    if "<h1" not in h:
        issues.append("missing_h1")
    if "<h2" not in h:
        issues.append("missing_h2")
    if "<p" not in h:
        issues.append("missing_p")

    img_tags = _IMG_TAG_RE.findall(html or "")
    if len(img_tags) < expected_image_count:
        issues.append(f"insufficient_images:{len(img_tags)}/{expected_image_count}")
    for tag in img_tags:
        if not _SRC_RE.search(tag):
            issues.append("img_missing_src")
        if not _ALT_RE.search(tag):
            issues.append("img_missing_alt")
    return issues


def repair_missing_images(html: str, images: Sequence[ImageItem]) -> str:
    """LLM 응답에 이미지가 빠져 있을 때 — 휴리스틱 분산 삽입으로 보강."""
    img_count = len(_IMG_TAG_RE.findall(html or ""))
    if img_count >= max(2, len(images)):
        return html
    return _post_insert_images(html or "", images)


# ── Anthropic 프롬프트 ────────────────────────────────
ANTHROPIC_SYSTEM_PROMPT = """당신은 수익형 한국어 블로그 전문 작가입니다.
SEO 최적화된 한국어 블로그 포스트를 HTML 형식으로 작성하세요.

[ 글 구조 규칙 ]
1. <h1> 제목에 타겟 키워드를 자연스럽게 포함하세요.
2. 글 전체 길이는 2,000~2,500자 수준으로 작성하세요.
3. <h2> 소제목 3~4개로 본문을 구조화하세요. 필요 시 <h3> 도 사용.
4. 단락은 <p> 로 감싸고, 핵심 정보는 <ul><li> 리스트로 정리하세요.
5. 사실 정보는 raw_content 에서 가져오되, 표절이 아닌 본인 표현으로 재작성.
6. 마지막 <h2> 는 ‘마무리 정리’ 또는 ‘요약’ 으로 끝내세요.

[ 이미지 삽입 규칙 — 매우 중요 ]
사용자가 제공한 이미지들을 글의 흐름에 맞게 적절한 위치에 삽입해야 합니다.
규칙을 반드시 지키세요:
A. 이미지는 글의 ‘초반(소개 직후)’, ‘중반(핵심 설명 사이)’, ‘후반(요약 직전)’ 에 자연스럽게 분산.
B. 반드시 <img> 태그 사용. <figure><img .../></figure> 로 감싸도 좋음.
C. src 속성에는 ‘제공된 URL’ 을 그대로 사용. 다른 URL 만들어내지 마세요.
D. alt 속성에는 ‘추천 alt’ 값을 그대로 쓰거나, 키워드를 포함한 자연 문장으로 작성.
E. data-filename 속성에 제공된 filename 을 함께 기록 (발행 단계에서 식별자로 사용됨).
F. 자동 일괄 추가(append at end)가 아니라 본문 ‘흐름 안에 정확한 위치’ 에 배치.

[ 출력 형식 ]
- <h1> 으로 시작하는 순수 HTML 만 출력하세요.
- markdown 이나 ``` 코드펜스 금지.
- 설명 문구나 머리말 없이 HTML 만 반환.
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
        image_brief = _format_image_brief(norm_images) if norm_images else "(이미지 없음 — 본문에 <img> 태그 추가하지 마세요)"
        sources_block = "\n".join(f"- {u}" for u in sources) if sources else "(없음)"

        user_prompt = (
            f"타겟 키워드: {keyword}\n"
            f"글 톤: {tone}\n\n"
            f"[참고 자료 — raw_content]\n{snippet}\n\n"
            f"[출처 URL]\n{sources_block}\n\n"
            f"[제공 이미지 — 본문 흐름에 맞춰 직접 위치 배치]\n{image_brief}\n\n"
            f"위 raw_content 를 참고해 SEO HTML 블로그 글을 작성하되, "
            f"제공된 이미지들을 글 본문 안 적절한 위치에 <img> 태그로 직접 삽입하세요."
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


# ── Heuristic Writer (폴백) ─────────────────────────
class HeuristicWriter(BaseWriter):
    """LLM 자격증명 없을 때의 안전 폴백 — 자체 템플릿 + 사후 이미지 삽입."""

    name = "heuristic"

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
        sentences = [s for s in re.split(r"(?<=[\.\?\!])\s+", snippet) if len(s) > 10]
        head = " ".join(sentences[:3]) or f"{kw} 관련 내용을 정리합니다."
        body_chunks = [sentences[i:i + 4] for i in range(0, min(len(sentences), 16), 4)]

        h2_titles = [
            f"{kw} 의 기본 개념",
            f"{kw} 활용 방법",
            f"{kw} 선택 시 주의사항",
            "마무리 정리",
        ]

        parts = [
            f"<h1>{kw} — 정리된 핵심 가이드</h1>",
            f"<p>{head}</p>",
        ]
        for i, title in enumerate(h2_titles):
            parts.append(f"<h2>{title}</h2>")
            chunk = body_chunks[i] if i < len(body_chunks) else []
            if chunk:
                parts.append("<p>" + " ".join(chunk) + "</p>")
            else:
                parts.append(f"<p>{kw} 와 관련해 알아두면 좋은 내용을 더 정리해드립니다.</p>")
            if i == 1:
                parts.append("<ul>"
                             f"<li>{kw} 의 핵심 포인트 정리</li>"
                             f"<li>주의해야 할 점 체크</li>"
                             f"<li>실제 활용 사례</li>"
                             "</ul>")
        if sources:
            parts.append("<p><small>출처: " +
                          ", ".join(f'<a href="{u}">{u}</a>' for u in sources) +
                          "</small></p>")
        html = "\n".join(parts)
        return _post_insert_images(html, norm_images)
