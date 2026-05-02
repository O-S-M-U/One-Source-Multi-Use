"""추천기 — seed cooldown + 연속 주제 회피."""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

from ..config import Config
from ..models import ContentRecord, KeywordPoolItem, from_iso, now_utc
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


def recommend(storage: BaseStorage, cfg: Config, top_n: int = 5):
    pool = storage.list_pool()
    contents = storage.list_content()
    blocked = _seeds_in_cooldown(contents, cfg.seed_cooldown_days)
    last = _last_seed(contents) if cfg.avoid_consecutive_topic else None

    def is_eligible(item):
        if item.status != "golden":
            return False
        if item.seed_keyword in blocked:
            return False
        if last and item.seed_keyword == last:
            return False
        return True

    eligible = [it for it in pool if is_eligible(it)]
    eligible.sort(key=lambda x: x.score, reverse=True)
    return eligible[:top_n]
