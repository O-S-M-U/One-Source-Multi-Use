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

    def delete_content(self, content_id: str) -> bool:
        """content_db 에서 id 가 일치하는 레코드를 삭제. 성공 시 True.

        기본 구현: list_content → filter → replace_content. 모든 백엔드에서 동작.
        효율적인 직접 삭제가 필요한 경우 백엔드별로 override 가능.
        """
        if not content_id:
            return False
        records = self.list_content()
        kept = [r for r in records if r.id != content_id]
        if len(kept) == len(records):
            return False
        self.replace_content(kept)
        return True

    def update_content(self, content_id: str, **fields) -> bool:
        """content_db 의 특정 record 일부 필드를 in-place 갱신. 성공 시 True.

        id / created_at 같은 영구 식별자는 유지된다 (인자로 주더라도 무시).
        모든 백엔드는 list_content + replace_content 만 있으면 자동 동작.
        """
        if not content_id:
            return False
        records = self.list_content()
        protected = {"id", "created_at"}
        changed = False
        for r in records:
            if r.id == content_id:
                for k, v in fields.items():
                    if k in protected:
                        continue
                    if hasattr(r, k):
                        setattr(r, k, v)
                        changed = True
                break
        if not changed:
            return False
        self.replace_content(records)
        return True

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
