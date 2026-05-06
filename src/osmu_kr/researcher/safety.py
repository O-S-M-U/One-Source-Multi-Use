"""SafetyLayer (v13) — 키워드 작업 lifecycle / lock / archive 단일 진입점.

[ v13 spec 정렬 ]
  - keywords.status:        active | archived (단순 존재/제외)
  - keyword_usages.status:  in_progress | published | failed (작업 lifecycle)
  - keyword_usages 가 lock 의 ‘진실의 출처’.
  - keywords.archive 는 영구 제외 신호.

[ 책임 ]
  · start_lock(keyword_id, ...)  → keyword_usages(in_progress) 신규 레코드 생성.
                                    이미 in_progress 가 있으면 LockBusy.
  · mark_published(usage_id)     → in_progress → published. 발행 시각 기록.
  · mark_failed(usage_id, ...)   → in_progress → failed. 잠금 즉시 해제.
                                    published_at 은 빈 채로 유지 (180일 카운트 미적용).
  · archive_keyword(keyword_id)  → keywords.status = archived (영구 제외).
                                    in_progress lock 이 살아 있으면 자동 failed 처리.
  · is_locked(keyword_id)        → 활성 lock 있는지 단순 조회.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..models import (
    KSTATUS_ACTIVE, KSTATUS_ARCHIVED, KeywordPoolItem, KeywordUsage,
    USAGE_ALLOWED_TRANSITIONS, USAGE_FAILED, USAGE_IN_PROGRESS, USAGE_PUBLISHED,
    normalize_status, now_utc, to_iso,
)
from ..storage.base import BaseStorage

log = logging.getLogger(__name__)


class TransitionError(Exception):
    """status 전이가 정책상 허용되지 않을 때."""


class LockBusy(Exception):
    """이미 in_progress 잠금이 있어 새 작업을 시작할 수 없음."""


class CooldownViolation(Exception):
    """v13-B 어뷰징 쿨다운 위반 — 직전 발행과 유사도가 너무 높음."""


class SafetyLayer:
    """v13 키워드 작업 lifecycle 의 단일 진입점."""

    def __init__(self, storage: BaseStorage):
        self.storage = storage

    # ── lock 상태 조회 ─────────────────────────────────
    def is_locked(self, keyword_id: str) -> bool:
        return self.storage.get_active_usage(keyword_id) is not None

    def active_usage(self, keyword_id: str) -> Optional[KeywordUsage]:
        return self.storage.get_active_usage(keyword_id)

    # ── lifecycle 전이 ─────────────────────────────────
    def start_lock(self, keyword_id: str, *,
                    account_id: str = "", blog_id: str = "",
                    contents_id: str = "", note: str = "",
                    cooldown_threshold: float = 0.85,
                    cooldown_days: float = 3.0,
                    skip_cooldown: bool = False) -> KeywordUsage:
        """candidate → in_progress lock 생성 (작업 시작).

        - keywords 가 archived 면 거부.
        - 같은 keyword_id 의 in_progress 가 이미 있으면 LockBusy.
        - v13-B: 어뷰징 쿨다운 위반 시 CooldownViolation (skip_cooldown=True 면 우회).
        """
        item = self.storage.get_pool(keyword_id)
        if item is None:
            raise TransitionError(f"keyword_id={keyword_id!r} 존재하지 않음")
        if normalize_status(item.status) == KSTATUS_ARCHIVED:
            raise TransitionError(f"archived 키워드는 작업 시작 불가 ({keyword_id})")
        if self.is_locked(keyword_id):
            raise LockBusy(f"keyword_id={keyword_id} 이미 in_progress lock 있음")

        # v13-B: 어뷰징 쿨다운 — embedding 비교 (같은 blog 직전 발행과)
        if not skip_cooldown and item.embedding_json:
            from .keyword_safety import KeywordSafety
            ks = KeywordSafety(self.storage)
            conflict = ks.check_abuse_cooldown(
                keyword_id, threshold=cooldown_threshold,
                days=cooldown_days, blog_id=blog_id,
            )
            if conflict is not None:
                raise CooldownViolation(
                    f"abuse cooldown: '{conflict.match.keyword}' (sim={conflict.match.similarity:.3f}, "
                    f"published={conflict.match.last_published}, "
                    f"{conflict.days_remaining:.1f}일 남음)"
                )

        usage = KeywordUsage(
            keyword_id=keyword_id,
            account_id=account_id, blog_id=blog_id,
            contents_id=contents_id,
            status=USAGE_IN_PROGRESS,
            started_at=to_iso(now_utc()),
            note=note[:200],
        )
        self.storage.upsert_usage(usage)
        log.info("[safety] lock 시작: keyword=%s usage=%s", keyword_id, usage.id)
        return usage

    def mark_published(self, usage_id: str, *,
                        contents_id: str = "", note: str = "") -> KeywordUsage:
        """in_progress → published. 발행 시각 기록."""
        return self._transition_usage(
            usage_id, USAGE_PUBLISHED,
            extra={"published_at": to_iso(now_utc())},
            contents_id=contents_id, note=note,
        )

    def mark_failed(self, usage_id: str, *, note: str = "") -> KeywordUsage:
        """in_progress → failed. 잠금 즉시 해제. published_at = '' 유지."""
        return self._transition_usage(
            usage_id, USAGE_FAILED,
            extra={"failed_at": to_iso(now_utc())},
            note=note,
        )

    def fail_lock_for_keyword(self, keyword_id: str, *, note: str = "") -> Optional[KeywordUsage]:
        """편의 — 해당 keyword 의 활성 lock 을 failed 로 즉시 해제."""
        usage = self.storage.get_active_usage(keyword_id)
        if usage is None:
            return None
        return self.mark_failed(usage.id, note=note)

    # ── archive ────────────────────────────────────────
    def archive_keyword(self, keyword_id: str, *, reason: str = "") -> KeywordPoolItem:
        """keywords.status = archived. 활성 lock 이 있으면 자동 failed."""
        item = self.storage.get_pool(keyword_id)
        if item is None:
            raise TransitionError(f"keyword_id={keyword_id!r} 없음")

        if normalize_status(item.status) == KSTATUS_ARCHIVED:
            log.info("[safety] %s: 이미 archived — no-op", keyword_id)
            return item

        # 활성 lock 자동 해제
        active = self.storage.get_active_usage(keyword_id)
        if active is not None:
            self.mark_failed(active.id, note=f"auto_failed_by_archive: {reason}")

        # keywords.status = archived
        ts = to_iso(now_utc())
        item.status = KSTATUS_ARCHIVED
        item.archived_at = ts
        item.last_status_reason = (reason or "archive")[:200]
        item.updated_at = ts
        self.storage.upsert_pool(item)
        log.info("[safety] archive: keyword=%s reason=%s", keyword_id, reason)
        return item

    # ── 내부 ───────────────────────────────────────────
    def _transition_usage(self, usage_id: str, target: str, *,
                           extra: Optional[dict] = None,
                           contents_id: str = "", note: str = "") -> KeywordUsage:
        usages = self.storage.list_usages()
        usage = next((u for u in usages if u.id == usage_id), None)
        if usage is None:
            raise TransitionError(f"usage_id={usage_id!r} 없음")

        current = usage.status
        if current == target:
            return usage   # 멱등
        allowed = USAGE_ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise TransitionError(
                f"usage 전이 거부: {current} → {target} (허용: {sorted(allowed)})"
            )
        usage.status = target
        if contents_id:
            usage.contents_id = contents_id
        if note:
            usage.note = (usage.note + " | " + note).strip(" |")[:300]
        for k, v in (extra or {}).items():
            setattr(usage, k, v)
        self.storage.upsert_usage(usage)
        log.info("[safety] usage %s: %s → %s", usage_id, current, target)
        return usage
