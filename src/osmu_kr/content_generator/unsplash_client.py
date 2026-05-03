"""UnsplashClient — 이미지 수급 전용 클라이언트.

[ 책임 ]
  · keyword_translator 로 한글 → 영어 검색 키워드 후보 N개 생성
  · Unsplash /search/photos 호출 (자격증명 없으면 빈 결과)
  · 해상도 / 관련도 필터링
  · 파일명 규칙(슬러그-번호.확장자) 적용
  · 2~3개 ImageItem 반환 (정책)

다른 외부 API들과 동등한 수준의 독립성 — 다른 모듈은 이 클래스의 이 메서드만 호출한다.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional
from urllib.parse import urlparse

from .interfaces import ImageItem
from .keyword_classifier import profile_for
from .keyword_translator import (
    caption_for_role,
    keyword_to_slug,
    make_alt_text,
    make_filename,
    role_for_index,
    translate_to_english_queries,
)

log = logging.getLogger(__name__)

UNSPLASH_API = "https://api.unsplash.com/search/photos"
DEFAULT_TIMEOUT = 10
MIN_WIDTH = 800             # 해상도 필터
MIN_HEIGHT = 450
MIN_IMAGES = 2              # 정책: 최소 2장
MAX_IMAGES = 3              # 정책: 최대 3장


def _ext_from_url(url: str) -> str:
    """URL 의 확장자 추출. 없으면 'jpg'."""
    try:
        path = urlparse(url).path
        m = re.search(r"\.([a-zA-Z0-9]+)$", path)
        if m:
            ext = m.group(1).lower()
            if ext in ("jpg", "jpeg", "png", "webp", "gif"):
                return ext
    except Exception:
        pass
    return "jpg"


def _is_relevant(description: Optional[str], alt_description: Optional[str],
                  query_tokens: list[str]) -> bool:
    """간단한 관련도 체크 — 결과의 description/alt 에 query 토큰이 하나라도 포함되면 OK.

    Unsplash 에서 받은 결과는 일반적으로 query 와 충분히 관련되므로,
    이 필터는 ‘심하게 무관한’ 케이스만 걸러낸다 (예: 빈 description + 무의미 사진).
    """
    if not query_tokens:
        return True
    text = " ".join(filter(None, [description, alt_description])).lower()
    if not text:
        return True   # 메타 없으면 통과 (Unsplash 결과 자체가 query 매칭이라)
    return any(tok in text for tok in query_tokens)


class UnsplashClient:
    """Unsplash API 어댑터 — keyword 입력 → ImageItem 리스트 반환."""

    def __init__(self, access_key: Optional[str] = None,
                 *, timeout: int = DEFAULT_TIMEOUT,
                 min_width: int = MIN_WIDTH, min_height: int = MIN_HEIGHT):
        self.access_key = access_key or os.getenv("UNSPLASH_ACCESS_KEY", "")
        self.timeout = timeout
        self.min_width = min_width
        self.min_height = min_height

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_key)

    # ── 단일 query 검색 ──────────────────────────────
    def _search_one_query(self, query: str, *, per_page: int = 5) -> list[dict]:
        if not self.has_credentials:
            return []
        try:
            import requests
        except ImportError:
            return []
        try:
            r = requests.get(
                UNSPLASH_API,
                headers={
                    "Accept-Version": "v1",
                    "Authorization": f"Client-ID {self.access_key}",
                },
                params={
                    "query": query,
                    "per_page": per_page,
                    "orientation": "landscape",
                    "content_filter": "high",
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json().get("results", []) or []
        except Exception as e:
            log.warning("[unsplash] '%s' 검색 실패: %s", query, e)
            return []

    # ── 공개 API: keyword → ImageItem 리스트 ────────────
    def fetch(self, keyword: str, *, count: int = 3,
               slug: Optional[str] = None,
               alt_keyword: Optional[str] = None) -> List[ImageItem]:
        """keyword → 영어 변환 → 검색 → 필터 → ImageItem N개.

        Args:
            keyword: 한글/혼합 키워드 ("직장인 다이어트 식단")
            count: 원하는 이미지 수 (자동으로 [2, 3] 범위로 클램프)
            slug: 파일명 prefix (없으면 keyword_to_slug() 자동)
            alt_keyword: alt 에 쓸 키워드 (없으면 keyword 그대로)

        Returns:
            ImageItem 리스트. 자격증명 없거나 결과 없으면 빈 리스트.
        """
        n_target = max(MIN_IMAGES, min(MAX_IMAGES, count))
        slug_prefix = slug or keyword_to_slug(keyword)
        alt_kw = alt_keyword or keyword

        if not self.has_credentials:
            log.info("[unsplash] UNSPLASH_ACCESS_KEY 없음 → 빈 결과 반환")
            return []

        # 1) 도메인 분류 → 도메인별 image_query_templates 우선 사용
        profile = profile_for(keyword)
        domain_queries = profile.image_query_templates
        # 2) keyword 자체의 영어 변환도 함께 (보조)
        translated = translate_to_english_queries(keyword, max_queries=2)

        log.info("[unsplash] '%s' → 도메인=%s, 보조 쿼리=%s",
                  keyword, profile.domain.value, translated)

        # 각 role 별로 다른 검색어 사용 → 다양성 확보
        items: List[ImageItem] = []
        seen_ids: set[str] = set()
        roles_needed = ["concept", "example", "comparison", "summary"][:n_target]

        for i, role in enumerate(roles_needed, start=1):
            # role 별 쿼리: 도메인 템플릿 + 키워드 변환 + 키워드 그대로
            role_queries = list(domain_queries.get(role, []))
            # 키워드 영어 변환 결과를 첫 후보 앞에 추가 (도메인 검색어가 더 우선)
            role_queries.extend(translated)
            # 매핑되지 않으면 영어 변환만으로
            if not role_queries:
                role_queries = translated[:] or [keyword]

            picked = None
            query_tokens = [t for q in role_queries for t in q.split()]
            for q in role_queries:
                results = self._search_one_query(q, per_page=5)
                for r in results:
                    _id = r.get("id")
                    if not _id or _id in seen_ids:
                        continue
                    w = int(r.get("width") or 0)
                    h = int(r.get("height") or 0)
                    if w and h and (w < self.min_width or h < self.min_height):
                        continue
                    if not _is_relevant(r.get("description"), r.get("alt_description"),
                                        query_tokens):
                        continue
                    picked = r
                    seen_ids.add(_id)
                    break
                if picked:
                    break

            if not picked:
                continue

            urls = picked.get("urls") or {}
            url = urls.get("regular") or urls.get("small") or urls.get("full") or ""
            if not url:
                continue
            ext = _ext_from_url(url)
            items.append(ImageItem(
                url=url,
                filename=make_filename(slug_prefix, i, ext),
                alt=make_alt_text(alt_kw, i, role=role),
                width=int(picked.get("width") or 0),
                height=int(picked.get("height") or 0),
                source="unsplash",
                role=role,
                caption=caption_for_role(alt_kw, role),
            ))

        if not items:
            log.warning("[unsplash] 도메인=%s 검색 결과 0건", profile.domain.value)
            return []
        return items
