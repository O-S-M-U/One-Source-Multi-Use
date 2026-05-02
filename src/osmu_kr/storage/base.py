"""Storage 추상 인터페이스."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import ContentRecord, KeywordPoolItem, ResearchHistoryRecord


class BaseStorage(ABC):
    name: str = "base"

    @abstractmethod
    def list_pool(self) -> List[KeywordPoolItem]: ...
    @abstractmethod
    def get_pool(self, keyword_id: str) -> Optional[KeywordPoolItem]: ...
    @abstractmethod
    def upsert_pool(self, item: KeywordPoolItem) -> None: ...
    @abstractmethod
    def delete_pool(self, keyword_id: str) -> bool: ...
    @abstractmethod
    def replace_pool(self, items: List[KeywordPoolItem]) -> None: ...

    @abstractmethod
    def list_content(self) -> List[ContentRecord]: ...
    @abstractmethod
    def append_content(self, record: ContentRecord) -> None: ...

    def replace_content(self, records: List[ContentRecord]) -> None:
        existing = self.list_content()
        if existing:
            raise NotImplementedError(
                f"{type(self).__name__} 가 replace_content 를 직접 구현해야 합니다."
            )
        for r in records:
            self.append_content(r)

    # ── research_history (분석 이력) — 옵션. 백엔드가 미지원이면 no-op ──
    def append_history(self, record: ResearchHistoryRecord) -> None:
        """분석 시점별 스냅샷 누적. 미구현 백엔드는 silent no-op."""
        pass

    def list_history(self) -> List[ResearchHistoryRecord]:
        """미구현 백엔드는 빈 리스트."""
        return []

    def find_pool_by_keyword(self, keyword: str) -> Optional[KeywordPoolItem]:
        kw = (keyword or "").strip().lower()
        for item in self.list_pool():
            if item.keyword.strip().lower() == kw:
                return item
        return None
