"""ImageProvider 구현체 — 모두 ImageItem (role 포함) 리스트 반환.

  · UnsplashImageProvider  : UnsplashClient 위임 (자격증명 필요)
  · PicsumImageProvider    : 폴백 (자격증명 불필요, 항상 동작) — 명시적 opt-in 권장
  · ChainedImageProvider   : 여러 Provider 순차 시도, role 보존
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import List, Optional

from .interfaces import BaseImageProvider, ImageItem
from .keyword_translator import (
    caption_for_role,
    keyword_to_slug,
    make_alt_text,
    make_filename,
    role_for_index,
)
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
    """폴백 — picsum.photos 의 seed 기반 결정적 이미지.

    실 운영에서는 ChainedImageProvider 의 fallback 으로만 사용 권장.
    Generator 에서 ‘require_real_images=True’ 가 켜지면 이 Provider 는 우회된다.
    """
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
            role = role_for_index(i)
            items.append(ImageItem(
                url=url,
                filename=make_filename(slug_prefix, i, "jpg"),
                alt=make_alt_text(alt_kw, i, role=role),
                width=self.width,
                height=self.height,
                source="picsum",
                role=role,
                caption=caption_for_role(alt_kw, role),
            ))
        return items


class ChainedImageProvider(BaseImageProvider):
    """여러 Provider 를 순서대로 시도해 ImageItem 을 모은다.

    role/filename/alt 는 ‘최종 인덱스 기준’ 으로 일관 재배치한다 — 어떤 Provider 가
    부분 결과를 줘도 ‘1번=concept, 2번=example, ...’ 매핑이 깨지지 않는다.
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
            if len(collected) >= count:
                break

        # 메타데이터 재정렬: 1번=concept, 2번=example ...
        slug_prefix = slug or keyword_to_slug(query)
        alt_kw = alt_keyword or query
        for i, it in enumerate(collected, start=1):
            ext = (it.filename.split(".")[-1] if "." in it.filename else "jpg").lower()
            role = role_for_index(i)
            it.filename = make_filename(slug_prefix, i, ext)
            it.role = role
            it.alt = make_alt_text(alt_kw, i, role=role)
            it.caption = caption_for_role(alt_kw, role)
        return collected[:count]
