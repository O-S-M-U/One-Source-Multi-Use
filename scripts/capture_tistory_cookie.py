"""Tistory 쿠키 캡처 (ops-1).

[ 흐름 ]
  1. Playwright headed Chromium 브라우저 실행 (봇 탐지 회피 위해 headless 금지)
  2. https://www.tistory.com/ 로 이동 → 카카오 로그인 페이지
  3. 사용자가 직접 ID/PW + 2FA 까지 통과 (스크립트는 대기)
  4. "Enter 눌러 캡처 시작" — 로그인 완료 확인 후 콘솔에 Enter
  5. 쿠키 추출 → ./cookies/tistory_{blog_id}.json 저장
  6. (옵션) accounts 테이블에 자동 등록

[ 사용 ]
  python scripts/capture_tistory_cookie.py \
      --blog-id myblog \
      --login-id me@kakao.com \
      [--account-id acc-001]      # 안 주면 자동 부여
      [--register]                # accounts 테이블에 자동 등록
      [--cookies-dir ./cookies]   # 기본값

[ 운영 노트 ]
  · 쿠키 만료 (보통 30~90일) 시 다시 실행해서 갱신.
  · accounts.cookie_updated_at 가 함께 갱신됨.
  · .gitignore 에 cookies/ 가 반드시 포함돼야 함 (.env 와 같은 수준).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("capture_tistory_cookie")


def capture(*, blog_id: str, login_id: str = "",
             cookies_dir: str = "./cookies",
             headed: bool = True) -> str:
    """Playwright headed 브라우저로 로그인 + 쿠키 저장. 저장 경로 반환."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright 미설치. pip install playwright && playwright install chromium")
        sys.exit(1)

    Path(cookies_dir).mkdir(parents=True, exist_ok=True)
    cookie_path = os.path.join(cookies_dir, f"tistory_{blog_id}.json")

    log.info("Tistory 쿠키 캡처 시작 — blog=%s login=%s", blog_id, login_id or "(미입력)")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, slow_mo=120)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(120_000)

            # 카카오 로그인 페이지 (Tistory 가 자동으로 redirect)
            page.goto("https://www.tistory.com/auth/login")
            log.info("브라우저에서 직접 카카오 로그인을 완료하세요 (2FA 포함).")
            log.info("로그인 완료 후 이 터미널로 돌아와 ENTER 키를 눌러주세요...")
            try:
                input()
            except EOFError:
                pass

            # 로그인 성공 후 어떤 URL 에 있어도 일단 쿠키 export
            cookies = context.cookies()
            if not cookies:
                log.error("쿠키가 비어있음 — 로그인이 완료되지 않은 것 같습니다.")
                sys.exit(2)

            with open(cookie_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            log.info("✅ 쿠키 저장: %s (%d개)", cookie_path, len(cookies))
            log.info("  • 만료된 쿠키는 자동 무시됨")
            log.info("  • 이 파일은 절대 Git/Slack 등에 공유하지 마세요.")
            return cookie_path
        finally:
            browser.close()


def maybe_register_account(*, account_id: str, blog_id: str,
                              login_id: str, cookie_path: str) -> None:
    """accounts 테이블에 자동 등록 + cookie_updated_at 갱신."""
    from datetime import datetime, timezone
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from osmu_kr import Config
    from osmu_kr.models import Account
    from osmu_kr.storage.factory import build_storage

    storage = build_storage(Config())
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    storage.upsert_account(Account(
        id=account_id, platform="tistory", blog_id=blog_id,
        login_id=login_id, cookie_path=cookie_path,
        cookie_updated_at=ts, is_active=1,
        note="captured by scripts/capture_tistory_cookie.py",
    ))
    log.info("✅ accounts 테이블 등록: id=%s blog=%s", account_id, blog_id)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="capture_tistory_cookie")
    p.add_argument("--blog-id", required=True, help="Tistory 블로그 식별자")
    p.add_argument("--login-id", default="", help="(옵션) 메모용 로그인 이메일/ID")
    p.add_argument("--account-id", default="", help="(옵션) accounts.id — 안 주면 자동")
    p.add_argument("--cookies-dir", default="./cookies")
    p.add_argument("--register", action="store_true",
                    help="accounts 테이블에 자동 등록")
    p.add_argument("--headless", action="store_true",
                    help="기본은 headed (사용자 직접 로그인). headless 는 디버깅 전용")
    args = p.parse_args(argv)

    cookie_path = capture(
        blog_id=args.blog_id, login_id=args.login_id,
        cookies_dir=args.cookies_dir, headed=not args.headless,
    )

    if args.register:
        account_id = args.account_id or f"tistory_{args.blog_id}"
        maybe_register_account(
            account_id=account_id, blog_id=args.blog_id,
            login_id=args.login_id, cookie_path=cookie_path,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
