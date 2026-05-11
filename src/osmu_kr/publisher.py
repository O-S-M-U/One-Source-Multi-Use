"""publisher (v13 spec §5) — 승인된 글의 어뷰징 게이트 + Tistory 자동 발행.

[ 책임 ]
  · 발행 정책 게이트 4종:
      1) 일일 발행 상한 (publisher.daily_limit, default 2)
      2) 작성-발행 텀 최소 (publisher.min_draft_minutes, default 30분)
      3) 직전 발행과 유사도 쿨다운 (publisher.similarity_cooldown_threshold/days)
      4) 발행 시간 분산 (사용자 활동 시간대 안 랜덤 윈도우) — v1 단순 sleep
  · 실 발행은 BasePublisher 구현체(MockPublisher / TistoryPlaywrightPublisher) 가 담당.
  · keyword_usages.status = published, contents.published_at + platform_url 기록.

[ Mock vs Real ]
  · Mock: 테스트·드라이런 — 외부 호출 없이 가짜 URL 반환.
  · Tistory: Playwright 우회 (OSMU_PUBLISH_REAL=1 가드 + cookie_path 필수).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .config_manager import ConfigManager
from .models import (
    ContentRecord, KeywordUsage, USAGE_PUBLISHED, USAGE_FAILED,
    from_iso, now_utc, to_iso,
)
from .storage.base import BaseStorage

log = logging.getLogger(__name__)


# ── 결과 ───────────────────────────────────────────────
@dataclass
class PublishResult:
    success: bool = False
    platform_url: str = ""
    published_at: str = ""
    error: str = ""
    blocked_reason: str = ""    # 게이트에서 차단됐을 때 사유

    def summary(self) -> str:
        if self.success:
            return f"✅ published {self.platform_url} @ {self.published_at}"
        if self.blocked_reason:
            return f"⏸ blocked: {self.blocked_reason}"
        return f"❌ failed: {self.error}"


class PublishBlocked(Exception):
    """발행 게이트에서 차단됨 — 정책 위반."""


# ── BasePublisher ──────────────────────────────────────
class BasePublisher:
    """발행 백엔드 추상 — 실 발행만 책임."""
    name: str = "base"

    def publish(self, *, title: str, html: str,
                  account, contents_id: str = "") -> PublishResult:
        raise NotImplementedError


class MockPublisher(BasePublisher):
    """테스트·드라이런용. 외부 호출 없이 가짜 URL 반환."""
    name = "mock"

    def __init__(self, *, base_url: str = "https://example.tistory.com"):
        self.base_url = base_url

    def publish(self, *, title, html, account, contents_id=""):
        url = f"{self.base_url}/{contents_id or 'draft'}"
        return PublishResult(
            success=True, platform_url=url,
            published_at=to_iso(now_utc()),
        )


class TistoryPlaywrightPublisher(BasePublisher):
    """Tistory 자동 발행 — Playwright 우회 (실 환경에서만)."""
    name = "tistory_playwright"

    def publish(self, *, title, html, account, contents_id=""):
        if os.environ.get("OSMU_PUBLISH_REAL", "").strip() not in {"1", "true", "yes"}:
            return PublishResult(
                success=False,
                error="OSMU_PUBLISH_REAL 가 설정되지 않아 실 발행 차단(드라이런).",
            )
        if not account or not account.cookie_path:
            return PublishResult(success=False, error="account.cookie_path 가 없음")
        if not os.path.isfile(account.cookie_path):
            return PublishResult(
                success=False,
                error=f"cookie 파일 없음: {account.cookie_path} — 재로그인 필요",
            )
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            return PublishResult(
                success=False, error="playwright 미설치 — pip install playwright"
            )
        # 실제 Playwright 자동화는 본 메서드를 override 또는 내부 구현 추가.
        # v1 시작점은 인터페이스만 — 구체 구현은 운영 페이즈에서 채움.
        return PublishResult(
            success=False,
            error="Tistory Playwright 발행 흐름은 운영 단계에서 구현 — 현재는 인터페이스만 제공.",
        )


# ── Publisher (게이트 + 백엔드 호출) ───────────────────
class Publisher:
    """발행 정책 게이트의 단일 진입점."""

    def __init__(self, storage: BaseStorage,
                 *, backend: Optional[BasePublisher] = None,
                 config_mgr: Optional[ConfigManager] = None,
                 embedder=None):
        self.storage = storage
        self.backend = backend or MockPublisher()
        self.config_mgr = config_mgr or ConfigManager(storage)
        self._embedder = embedder

    @property
    def embedder(self):
        if self._embedder is None:
            from .content_generator.embedder import build_embedder
            self._embedder = build_embedder()
        return self._embedder

    # ── 게이트 ────────────────────────────────────────
    def _check_daily_limit(self, account) -> Optional[str]:
        limit = self.config_mgr.get_int("publisher.daily_limit", 2)
        today_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        count = 0
        for u in self.storage.list_usages():
            if u.status != USAGE_PUBLISHED:
                continue
            if account and u.blog_id and u.blog_id != account.blog_id:
                continue
            if not u.published_at:
                continue
            try:
                pub_dt = from_iso(u.published_at)
            except Exception:
                continue
            if pub_dt >= today_start:
                count += 1
        if count >= limit:
            return f"daily_limit_exceeded:{count}/{limit}"
        return None

    def _check_min_draft_age(self, record: ContentRecord) -> Optional[str]:
        min_minutes = self.config_mgr.get_int("publisher.min_draft_minutes", 30)
        if min_minutes <= 0 or not record.created_at:
            return None
        try:
            created_dt = from_iso(record.created_at)
        except Exception:
            return None
        age_min = (now_utc() - created_dt).total_seconds() / 60.0
        if age_min < min_minutes:
            return f"draft_too_fresh:{age_min:.1f}min<{min_minutes}min"
        return None

    def _check_similarity_cooldown(self, record: ContentRecord,
                                     account) -> Optional[str]:
        """직전 발행 글과 summary_embedding cosine ≥ threshold 면 차단."""
        thr = self.config_mgr.get_float(
            "publisher.similarity_cooldown_threshold", 0.85,
        )
        days = self.config_mgr.get_float(
            "publisher.similarity_cooldown_days", 3.0,
        )
        if not record.summary_embedding_json:
            return None
        try:
            import json
            from .content_generator.embedder import cosine
            my_vec = json.loads(record.summary_embedding_json)
        except Exception:
            return None

        cutoff = now_utc() - timedelta(days=days)
        # 같은 blog 에서 최근 발행된 contents 의 summary_embedding 비교
        recent_kids = set()
        for u in self.storage.list_usages():
            if u.status != USAGE_PUBLISHED or not u.published_at:
                continue
            if account and u.blog_id and u.blog_id != account.blog_id:
                continue
            try:
                pub_dt = from_iso(u.published_at)
            except Exception:
                continue
            if pub_dt >= cutoff:
                recent_kids.add(u.contents_id)

        if not recent_kids:
            return None
        for other in self.storage.list_content():
            if other.id not in recent_kids:
                continue
            if not other.summary_embedding_json:
                continue
            try:
                ovec = json.loads(other.summary_embedding_json)
            except Exception:
                continue
            sim = cosine(my_vec, ovec)
            if sim >= thr:
                return f"similarity_cooldown:sim={sim:.3f}>={thr} (vs {other.id})"
        return None

    # ── 공개 API ──────────────────────────────────────
    def publish(self, content_id: str, *,
                  account=None, skip_gates: bool = False) -> PublishResult:
        record = next((r for r in self.storage.list_content() if r.id == content_id), None)
        if record is None:
            return PublishResult(success=False,
                                  error=f"content_id={content_id} 없음")
        if not record.refined_post:
            return PublishResult(success=False,
                                  error="refined_post 가 비어있음")

        # 활성 계정 결정
        if account is None:
            account = self.storage.get_active_account(platform="tistory")
        if account is None:
            return PublishResult(success=False, error="활성 Tistory 계정 없음")

        # 게이트
        if not skip_gates:
            for gate, fn in (
                ("daily_limit", lambda: self._check_daily_limit(account)),
                ("min_draft", lambda: self._check_min_draft_age(record)),
                ("similarity", lambda: self._check_similarity_cooldown(record, account)),
            ):
                blocked = fn()
                if blocked:
                    log.warning("[publisher] %s 차단: %s", gate, blocked)
                    return PublishResult(success=False, blocked_reason=blocked)

        # 발행
        title = record.title or record.title_final or record.keyword
        result = self.backend.publish(
            title=title, html=record.refined_post,
            account=account, contents_id=record.id,
        )

        # contents + keyword_usages 갱신
        if result.success:
            self.storage.update_content(
                record.id,
                platform_url=result.platform_url,
                published_at=result.published_at,
                status="발행완료",
            )
            # 해당 usage 를 published 로 업데이트
            for u in self.storage.list_usages():
                if u.contents_id == record.id:
                    u.status = USAGE_PUBLISHED
                    u.published_at = result.published_at
                    u.blog_id = account.blog_id
                    self.storage.upsert_usage(u)
                    break
        else:
            self.storage.update_content(
                record.id,
                error_log=(record.error_log + f" | publish_failed: {result.error}").strip(" |"),
                status="실패" if result.error else "발행차단",
            )
        log.info("[publisher] %s", result.summary())
        return result
