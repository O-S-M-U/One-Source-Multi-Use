"""ImageProvider 구현체 — 모두 ImageItem 리스트 반환.

  · UnsplashImageProvider  : UnsplashClient 위임 (자격증명 필요)
  · PicsumImageProvider    : 폴백 (자격증명 불필요, 항상 동작)
  · ChainedImageProvider   : 여러 Provider 순차 시도
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import List, Optional

from .interfaces import BaseImageProvider, ImageItem
from .keyword_translator import keyword_to_slug, make_alt_text, make_filename
from .unsplash_client import UnsplashClient

log = logging.getLogger(__name__)


class UnsplashImageProvider(BaseImageProvider):
    name = "unsplash"

    def __init__(self, client: Optional[UnsplashClient] = None,
                 access_key: Optional[str] = None):
        self.client = client or UnsplashClient(access_key=access_key)

    def search(self, query, *, count=3, slug="", alt_keyword="") -> List[ImageItem]:
        return self.client.fetch(query, count=count,
                                  slug=slug or None,
                                  alt_keyword=alt_keyword or None)


class PicsumImageProvider(BaseImageProvider):
    """폴백 — picsum.photos 의 seed 기반 결정적 이미지."""
    name = "picsum"

    def __init__(self, width: int = 1200, height: int = 675):
        self.width = width
        self.height = height

    def search(self, query, *, count=3, slug="", alt_keyword="") -> List[ImageItem]:
        slug_prefix = slug or keyword_to_slug(query)
        alt_kw = alt_keyword or query
        seed_base = urllib.parse.quote(slug_prefix or "osmu")[:30]
        items: List[ImageItem] = []
        for i in range(1, count + 1):
            url = f"https://picsum.photos/seed/{seed_base}-{i}/{self.width}/{self.height}"
            items.append(ImageItem(
                url=url,
                filename=make_filename(slug_prefix, i, "jpg"),
                alt=make_alt_text(alt_kw, i),
                width=self.width,
                height=self.height,
                source="picsum",
            ))
        return items


class ChainedImageProvider(BaseImageProvider):
    """여러 Provider 순서대로 시도. 첫 번째로 충분한 결과 반환한 곳을 사용.

    `min_required` 만큼 안 채워지면 다음 Provider 로 보충 (혼합).
    """
    name = "chained"

    def __init__(self, providers: List[BaseImageProvider], min_required: int = 2):
        self.providers = providers
        self.min_required = min_required

    def search(self, query, *, count=3, slug="", alt_keyword="") -> List[ImageItem]:
        collected: List[ImageItem] = []
        seen_urls: set[str] = set()
        for p in self.providers:
            need = count - len(collected)
            if need <= 0:
                break
            items = p.search(query, count=need, slug=slug, alt_keyword=alt_keyword)
            for it in items:
                if it.url and it.url not in seen_urls:
                    seen_urls.add(it.url)
                    collected.append(it)
                    if len(collected) >= count:
                        break
            if len(collected) >= self.min_required:
                # 충분히 채워졌으면 다음 Provider 로 가지 않아도 OK
                if len(collected) >= count:
                    break
        # 파일명에 인덱스 일관 적용 — provider 가 제각기 매긴 인덱스 무시하고 재정렬
        slug_prefix = slug or keyword_to_slug(query)
        alt_kw = alt_keyword or query
        for i, it in enumerate(collected, start=1):
            ext = (it.filename.split(".")[-1] if "." in it.filename else "jpg").lower()
            it.filename = make_filename(slug_prefix, i, ext)
            it.alt = make_alt_text(alt_kw, i)
        return collected[:count]
