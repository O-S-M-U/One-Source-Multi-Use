"""FirecrawlClient — Firecrawl API/MCP 어댑터.

[ 두 가지 진입점 ]
  · REST  (기본) : FIRECRAWL_API_KEY 만 있으면 직접 https://api.firecrawl.dev 호출
  · MCP   (옵션) : 외부에서 ‘firecrawl_search’/‘firecrawl_scrape’ 호출 함수를 주입

자격증명/네트워크 오류는 모두 graceful fallback — 빈 결과 반환 + 에러 로그.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Callable, List, Optional

from .interfaces import BaseCrawler, CrawledPage

log = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    """간단한 HTML 태그 제거 + 공백 정리."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class FirecrawlClient(BaseCrawler):
    name = "firecrawl"
    BASE_URL = "https://api.firecrawl.dev/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        timeout: int = 20,
        mcp_search: Optional[Callable[[str, int], list]] = None,
        mcp_scrape: Optional[Callable[[str], dict]] = None,
    ):
        self.api_key = api_key or os.getenv("FIRECRAWL_API_KEY", "")
        self.timeout = timeout
        # MCP 어댑터 (선택) — 외부에서 함수 주입 시 그쪽 사용
        self._mcp_search = mcp_search
        self._mcp_scrape = mcp_scrape

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key) or self._mcp_search is not None

    # ── 검색 ────────────────────────────────────────
    def search(self, query: str, *, limit: int = 5) -> List[str]:
        # 1) MCP 어댑터 우선
        if self._mcp_search:
            try:
                results = self._mcp_search(query, limit) or []
                urls = [r.get("url") if isinstance(r, dict) else str(r) for r in results]
                return [u for u in urls if u][:limit]
            except Exception as e:
                log.warning("[firecrawl/mcp] search 실패: %s", e)

        # 2) REST API
        if not self.api_key:
            log.info("[firecrawl] FIRECRAWL_API_KEY 없음 → 검색 폴백")
            return []

        try:
            import requests
        except ImportError:
            log.warning("[firecrawl] requests 미설치 → 검색 폴백")
            return []

        try:
            r = requests.post(
                f"{self.BASE_URL}/search",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "limit": limit},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("data") or data.get("results") or []
            urls = []
            for it in items:
                if isinstance(it, dict):
                    url = it.get("url") or it.get("link")
                    if url:
                        urls.append(url)
            return urls[:limit]
        except Exception as e:
            log.warning("[firecrawl] search 실패: %s", e)
            return []

    # ── 스크랩 ──────────────────────────────────────
    def scrape(self, url: str) -> CrawledPage:
        # 1) MCP 어댑터
        if self._mcp_scrape:
            try:
                data = self._mcp_scrape(url) or {}
                title = data.get("title", "") if isinstance(data, dict) else ""
                content = data.get("markdown") or data.get("content") or ""
                return CrawledPage(url=url, title=title, content=_strip_html(content))
            except Exception as e:
                log.warning("[firecrawl/mcp] scrape 실패 [%s]: %s", url, e)

        # 2) REST API
        if not self.api_key:
            return CrawledPage(url=url, error="firecrawl_no_credentials")

        try:
            import requests
        except ImportError:
            return CrawledPage(url=url, error="requests_missing")

        try:
            r = requests.post(
                f"{self.BASE_URL}/scrape",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            payload = data.get("data") or data
            md = payload.get("markdown") or payload.get("content") or ""
            metadata = payload.get("metadata") or {}
            title = metadata.get("title") or metadata.get("ogTitle") or ""
            return CrawledPage(url=url, title=title, content=_strip_html(md))
        except Exception as e:
            log.warning("[firecrawl] scrape 실패 [%s]: %s", url, e)
            return CrawledPage(url=url, error=str(e))

    # ── 검색 + 스크랩 묶음 ─────────────────────────────
    def search_and_scrape(self, query: str, *, limit: int = 3) -> List[CrawledPage]:
        urls = self.search(query, limit=max(limit, 5))
        pages: List[CrawledPage] = []
        for url in urls:
            page = self.scrape(url)
            pages.append(page)
            if len([p for p in pages if not p.error and p.content]) >= limit:
                break
            time.sleep(0.3)  # 속도 제어
        return pages
