"""SafetyLayer — 키워드 안전장치 계층 (7단계).

[ 책임 ]
  · status 전이의 단일 진입점 — 직접 setattr 금지, transition() 통해서만.
  · 전이 시 lifecycle 타임스탬프 자동 기록 (inprogress_locked_at / published_at /
    failed_at / archived_at).
  · 잘못된 전이 (예: archived → candidate) 는 거부.
  · 향후 7-B(임베딩 자기잠식) / 7-C(timeout) / 7-D(콘텐츠 자기잠식) 정책의 호스트.

[ 정책 — 7-A 단계 ]
  candidate  → inprogress / archived
  inprogress → published / failed / archived
  failed     → candidate / archived       (재시도 또는 영구 제외)
  published  → candidate / archived       (180일 후 재진입 또는 archive)
  archived   → (없음 — 영구 제외)

[ 사용 ]
  safety = SafetyLayer(storage)
  safety.transition(keyword_id, KSTATUS_INPROGRESS, reason="select_for_content")
  safety.transition(keyword_id, KSTATUS_PUBLISHED, reason="auto-publish ok")
"""
from __future__ import annotations

import logging
from typing import Optional

from ..models import (
    ALLOWED_TRANSITIONS, KSTATUS_ARCHIVED, KSTATUS_CANDIDATE, KSTATUS_FAILED,
    KSTATUS_INPROGRESS, KSTATUS_PUBLISHED, KeywordPoolItem,
    NEW_STATUS_SET, normalize_status, now_utc, to_iso,
)
from ..storage.base import BaseStorage

log = logging.getLogger(__name__)


class TransitionError(Exception):
    """status 전이가 정책상 허용되지 않을 때."""


class SafetyLayer:
    """키워드 안전장치 — status 전이의 단일 진입점."""

    def __init__(self, storage: BaseStorage):
        self.storage = storage

    # ── 핵심 ────────────────────────────────────────────
    def transition(self, keyword_id: str, to_status: str,
                    *, reason: str = "") -> KeywordPoolItem:
        """status 전이 + lifecycle 타임스탬프 기록.

        - 전이 자체가 ALLOWED_TRANSITIONS 에 없으면 TransitionError.
        - 같은 status 로의 전이는 no-op (의도적으로 허용 — 멱등).
        """
        target = normalize_status(to_status)
        if target not in NEW_STATUS_SET:
            raise TransitionError(f"unknown status: {to_status!r}")

        item = self.storage.get_pool(keyword_id)
        if item is None:
            raise TransitionError(f"keyword_id={keyword_id!r} 존재하지 않음")

        current = normalize_status(item.status)
        if current == target:
            log.info("[safety] %s: 이미 %s — no-op", keyword_id, target)
            return item

        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise TransitionError(
                f"전이 거부: {current} → {target} (허용 전이: {sorted(allowed)})"
            )

        # 타임스탬프 기록
        ts = to_iso(now_utc())
        item.status = target
        item.last_status_reason = (reason or "")[:200]
        item.updated_at = ts
        if target == KSTATUS_INPROGRESS:
            item.inprogress_locked_at = ts
        elif target == KSTATUS_PUBLISHED:
            item.published_at = ts
            item.inprogress_locked_at = ""    # lock 해제
        elif target == KSTATUS_FAILED:
            item.failed_at = ts
            item.inprogress_locked_at = ""
        elif target == KSTATUS_ARCHIVED:
            item.archived_at = ts
            item.inprogress_locked_at = ""
        elif target == KSTATUS_CANDIDATE:
            # 재진입 — 모든 lock/실패 흔적 클리어 (published_at 은 180일 정책에 사용되니 유지)
            item.inprogress_locked_at = ""
            item.failed_at = ""

        self.storage.upsert_pool(item)
        log.info("[safety] %s: %s → %s (%s)",
                  keyword_id, current, target, reason or "no_reason")
        return item

    # ── 편의 메서드 ─────────────────────────────────────
    def to_inprogress(self, keyword_id: str, reason: str = "") -> KeywordPoolItem:
        return self.transition(keyword_id, KSTATUS_INPROGRESS, reason=reason)

    def to_published(self, keyword_id: str, reason: str = "") -> KeywordPoolItem:
        return self.transition(keyword_id, KSTATUS_PUBLISHED, reason=reason)

    def to_failed(self, keyword_id: str, reason: str = "") -> KeywordPoolItem:
        return self.transition(keyword_id, KSTATUS_FAILED, reason=reason)

    def to_archived(self, keyword_id: str, reason: str = "") -> KeywordPoolItem:
        return self.transition(keyword_id, KSTATUS_ARCHIVED, reason=reason)

    def to_candidate(self, keyword_id: str, reason: str = "") -> KeywordPoolItem:
        return self.transition(keyword_id, KSTATUS_CANDIDATE, reason=reason)

    # ── 조회 ────────────────────────────────────────────
    def candidates(self) -> list:
        return [it for it in self.storage.list_pool()
                if normalize_status(it.status) == KSTATUS_CANDIDATE]

    def inprogress(self) -> list:
        return [it for it in self.storage.list_pool()
                if normalize_status(it.status) == KSTATUS_INPROGRESS]

    def published(self) -> list:
        return [it for it in self.storage.list_pool()
                if normalize_status(it.status) == KSTATUS_PUBLISHED]

    def archived(self) -> list:
        return [it for it in self.storage.list_pool()
                if normalize_status(it.status) == KSTATUS_ARCHIVED]
