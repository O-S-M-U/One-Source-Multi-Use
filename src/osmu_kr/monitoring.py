"""monitoring (infra-3) — 외부 API 호출량 + 한도 도달 Slack 알림.

[ 설계 ]
  · 호출 횟수를 storage.config 의 monitoring.usage.* 키에 일자별로 누적.
    예: monitoring.usage.20260506.anthropic_calls = 42
  · 임계 도달 시 monitoring.warned.YYYYMMDD.<service> 플래그로 중복 알림 방지.
  · 비용 그 자체를 추적하지는 않음 (실제 토큰/Cost 는 외부 콘솔에서) — 호출 ‘횟수’ 만.

[ env ]
  · 임계는 ConfigManager 의 monitoring.* 키 (env 또는 DB).
    monitoring.anthropic_daily_call_warn   (default 1500)
    monitoring.cse_daily_warn              (default 80)   # 무료 100/일
    monitoring.firecrawl_monthly_warn      (default 450)  # 무료 500/월
    monitoring.searchad_daily_warn         (default 1500)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .config_manager import ConfigManager
from .notifications import post_slack_message
from .storage.base import BaseStorage

log = logging.getLogger(__name__)


_DEFAULT_WARN = {
    "anthropic":  ("monitoring.anthropic_daily_call_warn", 1500, "일"),
    "cse":        ("monitoring.cse_daily_warn",              80, "일"),
    "firecrawl":  ("monitoring.firecrawl_monthly_warn",     450, "월"),
    "searchad":   ("monitoring.searchad_daily_warn",       1500, "일"),
    "unsplash":   ("monitoring.unsplash_daily_warn",         45, "일"),  # 50/h
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m")


def _bucket(service: str) -> str:
    """일 단위 vs 월 단위 — 서비스마다 다름."""
    if service == "firecrawl":
        return _month()
    return _today()


def _usage_key(service: str) -> str:
    return f"monitoring.usage.{_bucket(service)}.{service}"


def _warned_key(service: str) -> str:
    return f"monitoring.warned.{_bucket(service)}.{service}"


class UsageMonitor:
    """외부 API 호출량 카운터 + 한도 도달 Slack 알림."""

    def __init__(self, storage: BaseStorage,
                 *, config_mgr: Optional[ConfigManager] = None):
        self.storage = storage
        self.config_mgr = config_mgr or ConfigManager(storage)

    # ── 호출 기록 ────────────────────────────────────
    def record(self, service: str, *, count: int = 1) -> int:
        """service 한 번 호출 — 카운터 증가. 누적 후 임계 체크.

        Returns:
          현재 누적 카운트.
        """
        if service not in _DEFAULT_WARN:
            log.debug("[monitoring] unknown service: %s", service)
            return 0
        key = _usage_key(service)
        try:
            cur = int(self.storage.get_config(key) or 0)
        except Exception:
            cur = 0
        new_val = cur + count
        try:
            self.storage.set_config(key, str(new_val))
        except Exception as e:
            log.warning("[monitoring] usage 기록 실패 (%s): %s", service, e)
            return new_val

        self._maybe_warn(service, new_val)
        return new_val

    # ── 한도 체크 + 1회 알림 ──────────────────────────
    def _maybe_warn(self, service: str, current: int) -> None:
        cfg_key, default, period = _DEFAULT_WARN[service]
        try:
            threshold = self.config_mgr.get_int(cfg_key, default)
        except Exception:
            threshold = default
        if threshold <= 0 or current < threshold:
            return
        # 중복 알림 방지
        warned_k = _warned_key(service)
        try:
            already = self.storage.get_config(warned_k)
        except Exception:
            already = None
        if already:
            return
        try:
            self.storage.set_config(warned_k, "1")
        except Exception:
            pass
        log.warning("[monitoring] %s 한도 도달: %d/%d (%s)",
                      service, current, threshold, period)
        try:
            post_slack_message(
                f"⚠️ *외부 API 한도 경보* — {service}\n"
                f"이번 {period} 누적 호출: *{current}* (임계 *{threshold}*).\n"
                f"무료 한도 초과/요금 폭주 가능성. 콘솔에서 사용량을 확인하세요."
            )
        except Exception as e:
            log.warning("[monitoring] Slack 알림 실패: %s", e)

    # ── 조회 ──────────────────────────────────────────
    def get(self, service: str) -> int:
        try:
            return int(self.storage.get_config(_usage_key(service)) or 0)
        except Exception:
            return 0

    def dump(self) -> dict:
        """오늘/이번 달 모든 서비스 누적값."""
        return {svc: self.get(svc) for svc in _DEFAULT_WARN.keys()}

    def reset_today(self, service: Optional[str] = None) -> None:
        """일자 갱신용 — 수동 reset 또는 자정 cron."""
        for svc in ([service] if service else _DEFAULT_WARN.keys()):
            try:
                self.storage.delete_config(_usage_key(svc))
                self.storage.delete_config(_warned_key(svc))
            except Exception:
                pass
