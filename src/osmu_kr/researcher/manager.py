"""풀 관리(prune)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Tuple

from ..config import Config
from ..evaluator.base import BaseEvaluator
from ..models import (
    STATUS_EXPIRED, STATUS_GOLDEN, Evaluation, KeywordPoolItem,
    from_iso, now_utc, to_iso,
)
from ..storage.base import BaseStorage
from . import alchemist

log = logging.getLogger(__name__)


@dataclass
class PruneReport:
    revaluated: int = 0
    refreshed: int = 0
    expired: int = 0
    transmuted: int = 0
    overflow_removed: int = 0

    def summary(self):
        return (f"revaluated={self.revaluated} refreshed={self.refreshed} "
                f"expired={self.expired} transmuted={self.transmuted} "
                f"overflow_removed={self.overflow_removed}")


def _next_id(existing):
    nums = []
    for it in existing:
        try:
            nums.append(int(it.keyword_id))
        except (TypeError, ValueError):
            pass
    n = (max(nums) if nums else 0) + 1
    return f"{n:04d}"


def prune(storage: BaseStorage, evaluator: BaseEvaluator, cfg: Config) -> Tuple[List[KeywordPoolItem], PruneReport]:
    items = storage.list_pool()
    report = PruneReport()
    now = now_utc()
    cutoff = now - timedelta(days=cfg.revival_days)

    survivors = []
    new_variants = []

    for it in items:
        created_at = from_iso(it.updated_at or it.created_at)
        if created_at >= cutoff:
            survivors.append(it)
            continue
        report.revaluated += 1
        ev = evaluator.evaluate(it.keyword, seed=it.seed_keyword)
        if ev.score >= cfg.golden_threshold:
            it.search_volume = ev.search_volume
            it.competition = ev.competition
            it.cpc = ev.cpc
            it.commercial_intent = ev.commercial_intent
            it.score = ev.score
            it.status = STATUS_GOLDEN
            it.updated_at = to_iso(now)
            it.note = (it.note + " | re-evaluated:golden").strip(" |")
            survivors.append(it)
            report.refreshed += 1
        elif cfg.medium_lower <= ev.score < cfg.medium_upper:
            for variant in alchemist.transmute(it.keyword):
                v_ev = evaluator.evaluate_longtail(variant, seed=it.seed_keyword)
                if v_ev.score >= cfg.golden_threshold:
                    nv = KeywordPoolItem(
                        keyword_id=_next_id(items + survivors + new_variants),
                        seed_keyword=it.seed_keyword,
                        keyword=variant,
                        **v_ev.to_row(),
                        status=STATUS_GOLDEN,
                        source=f"{evaluator.name}+alchemy",
                        note=f"transmuted from '{it.keyword}'",
                    )
                    new_variants.append(nv)
                    report.transmuted += 1
            report.expired += 1
        else:
            it.status = STATUS_EXPIRED
            report.expired += 1

    pool = survivors + new_variants
    if len(pool) > cfg.pool_max_size:
        pool.sort(key=lambda x: x.score, reverse=True)
        report.overflow_removed = len(pool) - cfg.pool_max_size
        pool = pool[: cfg.pool_max_size]

    storage.replace_pool(pool)
    log.info("prune complete: %s", report.summary())
    return pool, report
