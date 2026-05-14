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
    """Tistory 자동 발행 — Playwright 우회 (실 환경에서만).

    [ 보안 가드 ]
      · OSMU_PUBLISH_REAL=1 환경변수 필수 (의도치 않은 실 발행 방지)
      · account.cookie_path 의 쿠키 파일 필수

    [ 발행 흐름 (v1 단순) ]
      1) 쿠키 로드 + browser context 생성
      2) https://{blog_id}.tistory.com/manage/newpost 접속
      3) 제목 / 본문 (raw HTML) 입력
      4) ‘발행’ 클릭 → 결과 URL 추출
      5) 실패 시 쿠키 만료 의심 → notify_cookie_expired

    [ 운영 노트 ]
      · Tistory 가 자주 DOM 셀렉터를 바꾸므로, 실제 운영 시 셀렉터는
        config 또는 별도 모듈로 분리해 빠르게 수정 가능하게 두는 게 좋다.
      · headed 모드 권장 (headless 는 봇 탐지 트리거).
    """
    name = "tistory_playwright"

    # ── DOM 셀렉터 default — config 로 override 가능 ──
    # 실제 운영하면서 Tistory 가 selector 를 바꾸면 config 만 갱신:
    #   osmu-kr config-set --key tistory.selector.title --value "input[placeholder='제목']"
    DEFAULT_SELECTORS = {
        "title": [
            "#title",
            'input[name="title"]',
            'input[placeholder*="제목"]',
        ],
        "body": [
            "textarea#content",
            'textarea[name="content"]',
            ".tox-edit-area iframe",
        ],
        "publish": [
            "button#publish",
            "button.btn-publish",
            'button:has-text("공개 발행")',
            'button:has-text("발행")',
        ],
        "success_url_pattern": "**/entry/**",
    }

    def __init__(self, *, headless: bool = False, slow_mo: int = 100,
                 timeout: int = 30_000, config_mgr=None):
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout
        self.config_mgr = config_mgr

    def _selectors(self, role: str) -> list:
        """DB config 우선 (콤마 구분 문자열) → DEFAULTS 폴백."""
        if self.config_mgr is not None:
            raw = self.config_mgr.get(f"tistory.selector.{role}")
            if raw:
                if isinstance(raw, list):
                    return list(raw)
                return [s.strip() for s in str(raw).split(",") if s.strip()]
        return list(self.DEFAULT_SELECTORS.get(role, []))

    def _success_url_pattern(self) -> str:
        if self.config_mgr is not None:
            v = self.config_mgr.get("tistory.success_url_pattern")
            if v:
                return str(v)
        return self.DEFAULT_SELECTORS["success_url_pattern"]

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
            from playwright.sync_api import sync_playwright
        except ImportError:
            return PublishResult(
                success=False, error="playwright 미설치 — pip install playwright && playwright install chromium",
            )

        import json
        try:
            with open(account.cookie_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception as e:
            return PublishResult(
                success=False, error=f"cookie 파싱 실패: {e}",
            )

        url = ""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=self.headless, slow_mo=self.slow_mo,
                )
                try:
                    context = browser.new_context()
                    context.add_cookies(cookies)
                    page = context.new_page()
                    page.set_default_timeout(self.timeout)

                    blog_id = account.blog_id or "myblog"
                    write_url = f"https://{blog_id}.tistory.com/manage/newpost"
                    page.goto(write_url)

                    # 로그인 페이지로 리다이렉트되면 쿠키 만료
                    if "auth.kakao.com" in page.url or "login" in page.url.lower():
                        try:
                            from .notifications import notify_cookie_expired
                            notify_cookie_expired(
                                blog_id=account.blog_id,
                                login_id=account.login_id,
                                account_id=account.id,
                            )
                        except Exception:
                            pass
                        return PublishResult(
                            success=False,
                            error=f"쿠키 만료 의심 — 재로그인 필요 (redirected to {page.url})",
                        )

                    # 제목 입력 — config 또는 DEFAULTS 셀렉터
                    for sel in self._selectors("title"):
                        try:
                            page.fill(sel, title)
                            break
                        except Exception:
                            continue

                    # 본문 — config 또는 DEFAULTS 셀렉터
                    body_inserted = False
                    for sel in self._selectors("body"):
                        try:
                            page.fill(sel, html)
                            body_inserted = True
                            break
                        except Exception:
                            continue
                    if not body_inserted:
                        page.evaluate(
                            "(html) => { const ta=document.querySelector('textarea'); "
                            "if (ta) ta.value=html; }",
                            html,
                        )

                    # 발행 버튼
                    clicked = False
                    for sel in self._selectors("publish"):
                        try:
                            page.click(sel, timeout=5000)
                            clicked = True
                            break
                        except Exception:
                            continue
                    if not clicked:
                        return PublishResult(
                            success=False,
                            error="발행 버튼 클릭 실패 — tistory.selector.publish config 확인",
                        )

                    # 발행 완료 후 URL 패턴
                    page.wait_for_url(self._success_url_pattern(), timeout=15_000)
                    url = page.url
                finally:
                    browser.close()
        except Exception as e:
            return PublishResult(success=False,
                                   error=f"playwright 흐름 실패: {type(e).__name__}: {e}")

        if not url:
            return PublishResult(success=False, error="발행 URL 추출 실패")
        return PublishResult(
            success=True, platform_url=url,
            published_at=to_iso(now_utc()),
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
