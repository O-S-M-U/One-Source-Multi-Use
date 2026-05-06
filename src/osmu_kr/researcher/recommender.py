"""추천기 — seed cooldown + 연속 주제 회피."""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

from ..config import Config
from ..models import (
    ContentRecord, KeywordPoolItem, KSTATUS_ACTIVE, KSTATUS_ARCHIVED,
    USAGE_IN_PROGRESS, USAGE_PUBLISHED,
    from_iso, normalize_status, now_utc,
)
from ..storage.base import BaseStorage


def _last_seed(records):
    if not records:
        return None
    latest = max(records, key=lambda r: r.created_at or "")
    return (latest.seed_keyword or "").strip()


def _seeds_in_cooldown(records, cooldown_days):
    cutoff = now_utc() - timedelta(days=cooldown_days)
    seeds = set()
    for r in records:
        if not r.seed_keyword:
            continue
        try:
            ts = from_iso(r.created_at)
        except Exception:
            continue
        if ts >= cutoff:
            seeds.add(r.seed_keyword.strip())
    return seeds


def recommend(storage: BaseStorage, cfg: Config, top_n: int = 5,
              *, blog_id: str = ""):
    """v13 추천 로직.

    제외 조건:
      · keywords.status = archived
      · 해당 keyword 에 활성 in_progress lock 이 있음 (이미 작업 중)
      · 같은 blog_id 에서 이미 published 상태로 사용된 적 있음 (자기잠식 차단)
      · 최근 cooldown 안의 seed
      · 직전 발행 seed (연속 회피)
    """
    pool = storage.list_pool()
    contents = storage.list_content()
    blocked_seeds = _seeds_in_cooldown(contents, cfg.seed_cooldown_days)
    last = _last_seed(contents) if cfg.avoid_consecutive_topic else None

    # keyword_id → 활성 lock / 같은 blog 에서 published 여부
    locked_kids = set()
    published_in_blog_kids = set()
    for u in storage.list_usages():
        if u.status == USAGE_IN_PROGRESS:
            locked_kids.add(u.keyword_id)
        if blog_id and u.status == USAGE_PUBLISHED and u.blog_id == blog_id:
            published_in_blog_kids.add(u.keyword_id)

    def is_eligible(item):
        if normalize_status(item.status) != KSTATUS_ACTIVE:
            return False
        if item.keyword_id in locked_kids:
            return False
        if item.keyword_id in published_in_blog_kids:
            return False
        if item.seed_keyword in blocked_seeds:
            return False
        if last and item.seed_keyword == last:
            return False
        return True

    eligible = [it for it in pool if is_eligible(it)]
    eligible.sort(key=lambda x: x.score, reverse=True)
    return eligible[:top_n]
