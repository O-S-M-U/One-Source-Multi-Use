"""Collector — keyword 기반 raw_content 생성.

[ 동작 ]
  1. crawler.search_and_scrape(query, limit) — 최소 3개 페이지 확보 시도
  2. 본문 정제 (광고/네비/반복 텍스트 제거)
  3. 문장 단위 dedup — 출처가 다르더라도 동일 문장이면 1번만 포함
  4. 자연스러운 문맥을 위해 ‘출처 기준 단락’ 으로 묶어서 반환

URL 부족 시에도 가능한 범위에서 raw_content 를 구성한다(요구사항 §7).

[ 입력 구조 — 1단계 정리 ]
collect() 는 이제 str 과 KeywordContext 둘 다 받는다.
str 이 들어오면 KeywordContext.coerce() 로 즉시 정규화하고,
모듈 진입 직후 로그로 “키워드 + 추론 토픽 + intent” 를 한 줄로 찍는다.
이렇게 해서 ‘데드바이데이라이트 = 게임’ 정보가 다음 단계까지 살아있게 만든다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Union

from .interfaces import BaseCrawler, CrawledPage
from .keyword_context import KeywordContext

log = logging.getLogger(__name__)


@dataclass
class RawContent:
    keyword: str
    sources: List[str] = field(default_factory=list)        # URL
    pages: List[CrawledPage] = field(default_factory=list)
    text: str = ""                                          # 합쳐진 raw 본문
    char_count: int = 0
    error: str = ""
    context: Optional[KeywordContext] = None                # ★ 추론된 도메인/의도 컨텍스트

    def is_empty(self) -> bool:
        return not self.text or self.char_count < 200


# ── 본문 정제 패턴 ──────────────────────────────────────
NOISE_PATTERNS = [
    r"\b(쿠키|cookie)[^\.\n]{0,40}(설정|policy)\b",
    r"무단\s*전재.*?금지",
    r"copyright.*?reserved",
    r"이메일\s*문의.*",
    r"광고\s*문의.*",
    r"\[?구독\]?\s*하기",
    r"좋아요\s*수\s*\d+",
    r"댓글\s*\d+",
]
NOISE_REGEX = [re.compile(p, re.IGNORECASE) for p in NOISE_PATTERNS]


def _clean_paragraph(text: str) -> str:
    for r in NOISE_REGEX:
        text = r.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_sentences(text: str) -> List[str]:
    # 한국어/영문 모두 대응 — 마침표/물음표/느낌표/줄바꿈 분리
    parts = re.split(r"(?<=[\.\?\!])\s+|\n+", text)
    return [s.strip() for s in parts if s.strip()]


def _dedup_sentences(sentences: List[str]) -> List[str]:
    """공백·기호 제거 후 정규화 키로 dedup. 짧은 단편은 제외(8자 미만)."""
    seen = set()
    out = []
    for s in sentences:
        if len(s) < 8:
            continue
        norm = re.sub(r"\s+|[^0-9A-Za-z가-힣]", "", s).lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(s)
    return out


class Collector:
    """크롤러 추상화 위에서 keyword → raw 데이터를 만든다."""

    def __init__(self, crawler: BaseCrawler, *, min_sources: int = 3, search_limit: int = 5):
        self.crawler = crawler
        self.min_sources = min_sources
        self.search_limit = search_limit

    def collect(self, keyword: Union[str, KeywordContext],
                *, limit: int = 3) -> RawContent:
        # ── 입력 정규화: str / KeywordContext / None 모두 처리 ──
        ctx = KeywordContext.coerce(keyword)
        kw = ctx.keyword
        if not kw:
            log.info("[collector] 입력 컨텍스트: (빈 키워드) → 즉시 종료")
            return RawContent(keyword="", error="empty_keyword", context=ctx)

        # 진입 로그 — 완료 기준 (‘게임 관련 키워드’ 힌트가 보이는지 확인)
        log.info(
            "[collector] 입력 컨텍스트: keyword='%s' / inferred_topic='%s 관련 키워드' / "
            "intent_hint='%s' / domain='%s'",
            ctx.keyword, ctx.inferred_topic, ctx.intent_hint, ctx.domain,
        )

        try:
            pages = self.crawler.search_and_scrape(kw, limit=max(limit, self.min_sources))
        except Exception as e:
            log.warning("[collector] crawler 실패: %s", e)
            return RawContent(keyword=kw, error=f"crawl_failed: {e}", context=ctx)

        usable = [p for p in pages if p.content and not p.error]
        if not usable:
            return RawContent(keyword=kw, pages=pages,
                                error="no_usable_pages", context=ctx)

        # 정제 + 단락별 결합
        merged_sentences: List[str] = []
        sources: List[str] = []
        for page in usable[:limit] if limit else usable:
            cleaned = _clean_paragraph(page.content)
            if not cleaned:
                continue
            sources.append(page.url)
            merged_sentences.extend(_split_sentences(cleaned))

        deduped = _dedup_sentences(merged_sentences)
        text = " ".join(deduped)

        return RawContent(
            keyword=kw,
            sources=sources,
            pages=usable,
            text=text,
            char_count=len(text),
            error="" if text else "empty_after_clean",
            context=ctx,
        )
