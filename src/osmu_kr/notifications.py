"""notifications (next-6) — Slack 알림 단일 진입점.

[ 책임 ]
  · Slack Bot Token 으로 chat.postMessage 호출.
  · 키 없거나 호출 실패 → 콘솔 로깅 fallback (앱은 절대 멈추지 않음).
  · 알림 타입별 헬퍼:
      - notify_checker_passed(content_id, summary, preview_url=...)
      - notify_publish_done(content_id, platform_url)
      - notify_cookie_expired(blog_id, login_id, account_id)
      - notify_error(stage, message)

[ env ]
  · SLACK_BOT_TOKEN — xoxb-... (없으면 stub)
  · SLACK_CHANNEL_ID — 알림 채널 (없으면 stub)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class SlackResult:
    sent: bool = False
    channel: str = ""
    ts: str = ""
    error: str = ""


def _has_slack_credentials() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN")) and \
            bool(os.environ.get("SLACK_CHANNEL_ID"))


def post_slack_message(text: str, *, channel: Optional[str] = None,
                        blocks=None) -> SlackResult:
    """Slack chat.postMessage. 키 없으면 stub (콘솔 로깅) + sent=False."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    ch = channel or os.environ.get("SLACK_CHANNEL_ID", "")
    if not (token and ch):
        log.info("[notifications.slack:stub] ch=%s text=%s",
                  ch or "<none>", text[:200])
        return SlackResult(sent=False, channel=ch,
                            error="no_credentials")
    try:
        import requests
    except ImportError:
        log.warning("[notifications] requests 미설치 — stub 으로 전환")
        return SlackResult(sent=False, channel=ch, error="requests_missing")
    payload = {"channel": ch, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}",
                      "Content-Type": "application/json; charset=utf-8"},
            json=payload, timeout=10,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return SlackResult(sent=True, channel=ch, ts=str(data.get("ts", "")))
        return SlackResult(sent=False, channel=ch,
                            error=str(data.get("error") or r.status_code))
    except Exception as e:
        log.warning("[notifications.slack] 호출 실패: %s", e)
        return SlackResult(sent=False, channel=ch, error=str(e))


# ── 도메인 헬퍼 ─────────────────────────────────────────
def notify_checker_passed(*, content_id: str, title: str,
                            summary: str, preview_url: str = "") -> SlackResult:
    """checker Stage 1 통과 → 사람 검토 요청 (Stage 2 진입)."""
    text = (
        f"📝 *글 검토 요청* (id=`{content_id}`)\n"
        f"제목: {title}\n"
        f"검증 결과: {summary}\n"
        + (f"미리보기: {preview_url}" if preview_url else "")
    )
    return post_slack_message(text)


def notify_publish_done(*, content_id: str, title: str,
                         platform_url: str) -> SlackResult:
    text = (
        f"✅ *발행 완료* (id=`{content_id}`)\n"
        f"제목: {title}\n"
        f"URL: {platform_url}"
    )
    return post_slack_message(text)


def notify_cookie_expired(*, blog_id: str, login_id: str,
                            account_id: str) -> SlackResult:
    text = (
        f"⚠️ *Tistory 쿠키 만료* — 재로그인 필요\n"
        f"account: `{account_id}` blog: `{blog_id}` login: `{login_id}`\n"
        "→ Playwright 헤더 모드로 재로그인 후 cookie_path 갱신하세요."
    )
    return post_slack_message(text)


def notify_error(*, stage: str, message: str) -> SlackResult:
    text = f"❌ *{stage} 에러*\n```{message[:500]}```"
    return post_slack_message(text)


# ── Stage 2 진입점 — checker 결과 받아 Slack 알림 ──────
def submit_for_review(*, content_id: str, title: str,
                       check_result, preview_url: str = "") -> SlackResult:
    """checker.CheckerResult 받아 Stage 2(사람 검토) 진입.

    - passed=False 면 알림 안 보내고 error 결과 반환.
    - passed=True 면 notify_checker_passed 호출.
    """
    if not check_result.passed:
        log.warning("[notifications] check 실패 — Stage 2 진입 안 함: %s",
                     check_result.issues)
        return SlackResult(sent=False, error=f"check_failed:{check_result.issues}")
    return notify_checker_passed(
        content_id=content_id, title=title,
        summary=check_result.summary(), preview_url=preview_url,
    )
