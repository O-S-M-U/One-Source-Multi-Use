"""KeywordResearcher 본체."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import Config
from ..evaluator import build_evaluator, diagnose_weakness
from ..evaluator.base import BaseEvaluator
from ..models import (
    STATUS_GOLDEN, STATUS_MEDIUM, STATUS_REJECTED,
    ContentRecord, Evaluation, KeywordPoolItem,
    now_utc, to_iso,
)
from ..storage import BaseStorage, build_storage
from . import alchemist, expander, manager, recommender

log = logging.getLogger(__name__)


@dataclass
class SeedRunReport:
    seed: str
    expanded: int = 0
    accepted: int = 0
    transmuted: int = 0
    rejected: int = 0
    items: List[KeywordPoolItem] = field(default_factory=list)

    def summary(self):
        return (f"seed='{self.seed}' expanded={self.expanded} "
                f"accepted={self.accepted} transmuted={self.transmuted} "
                f"rejected={self.rejected}")


class KeywordResearcher:
    def __init__(self, cfg=None, storage=None, evaluator=None):
        self.cfg = cfg or Config()
        self.storage = storage or build_storage(self.cfg)
        self.evaluator = evaluator or build_evaluator(self.cfg)
        log.info("KeywordResearcher ready (storage=%s evaluator=%s)",
                 self.storage.name, self.evaluator.name)

    def _next_keyword_id(self, extra=None):
        existing = self.storage.list_pool() + (extra or [])
        nums = []
        for it in existing:
            try:
                nums.append(int(it.keyword_id))
            except (TypeError, ValueError):
                pass
        n = (max(nums) if nums else 0) + 1
        return f"{n:04d}"

    def _next_content_id(self):
        existing = self.storage.list_content()
        nums = []
        for r in existing:
            try:
                nums.append(int(r.id))
            except (TypeError, ValueError):
                pass
        n = (max(nums) if nums else 0) + 1
        return f"{n:03d}"

    def run_seed(self, seed: str, *, expand_limit: int = 10) -> SeedRunReport:
        seed = (seed or "").strip()
        report = SeedRunReport(seed=seed)
        if not seed:
            return report

        candidates = expander.expand(seed, limit=expand_limit)
        report.expanded = len(candidates)
        accepted_buf = []

        for kw in candidates:
            if self.storage.find_pool_by_keyword(kw):
                continue
            ev = self.evaluator.evaluate(kw, seed=seed)
            if ev.score >= self.cfg.golden_threshold:
                item = self._build_pool_item(seed, kw, ev, accepted_buf)
                accepted_buf.append(item)
                report.items.append(item)
                report.accepted += 1
            elif self.cfg.medium_lower <= ev.score < self.cfg.medium_upper:
                # 약점 진단 기반 처방형 알케미
                weaknesses = diagnose_weakness(ev)
                added_any = False
                for variant in alchemist.transmute_with_diagnosis(kw, weaknesses, max_variants=5):
                    if self.storage.find_pool_by_keyword(variant):
                        continue
                    v_ev = self.evaluator.evaluate_longtail(variant, seed=seed)
                    if v_ev.score >= self.cfg.golden_threshold:
                        item = self._build_pool_item(
                            seed, variant, v_ev, accepted_buf,
                            source_suffix="+alchemy",
                            note=f"transmuted from '{kw}'",
                        )
                        accepted_buf.append(item)
                        report.items.append(item)
                        report.transmuted += 1
                        added_any = True
                if not added_any:
                    report.rejected += 1
            else:
                report.rejected += 1

        for it in accepted_buf:
            self.storage.upsert_pool(it)
        log.info("run_seed: %s", report.summary())
        self._enforce_max_size()
        return report

    def _build_pool_item(self, seed, keyword, ev, extra, source_suffix="", note=""):
        return KeywordPoolItem(
            keyword_id=self._next_keyword_id(extra),
            seed_keyword=seed,
            keyword=keyword,
            search_volume=ev.search_volume,
            competition=ev.competition,
            cpc=ev.cpc,
            commercial_intent=ev.commercial_intent,
            score=ev.score,
            status=STATUS_GOLDEN,
            source=f"{self.evaluator.name}{source_suffix}",
            note=note,
        )

    def _enforce_max_size(self):
        pool = self.storage.list_pool()
        if len(pool) <= self.cfg.pool_max_size:
            return
        pool.sort(key=lambda x: x.score, reverse=True)
        self.storage.replace_pool(pool[: self.cfg.pool_max_size])

    def check_keyword(self, keyword, *, seed=None):
        keyword = (keyword or "").strip()
        if not keyword:
            raise ValueError("keyword 가 비어 있습니다.")
        seed = (seed or keyword).strip()
        existing = self.storage.find_pool_by_keyword(keyword)
        ev = self.evaluator.evaluate(keyword, seed=seed)
        if existing:
            existing.search_volume = ev.search_volume
            existing.competition = ev.competition
            existing.cpc = ev.cpc
            existing.commercial_intent = ev.commercial_intent
            existing.score = ev.score
            existing.updated_at = to_iso(now_utc())
            existing.status = STATUS_GOLDEN if ev.score >= self.cfg.golden_threshold else STATUS_MEDIUM
            self.storage.upsert_pool(existing)
            return existing

        status = (STATUS_GOLDEN if ev.score >= self.cfg.golden_threshold
                  else (STATUS_MEDIUM if ev.score >= self.cfg.medium_lower else STATUS_REJECTED))
        item = KeywordPoolItem(
            keyword_id=self._next_keyword_id(),
            seed_keyword=seed,
            keyword=keyword,
            search_volume=ev.search_volume,
            competition=ev.competition,
            cpc=ev.cpc,
            commercial_intent=ev.commercial_intent,
            score=ev.score,
            status=status,
            source=f"{self.evaluator.name}/manual",
            note="user-checked",
        )
        if status != STATUS_REJECTED:
            self.storage.upsert_pool(item)
        return item

    def prune(self):
        return manager.prune(self.storage, self.evaluator, self.cfg)

    def recommend(self, top_n=5):
        return recommender.recommend(self.storage, self.cfg, top_n=top_n)

    def select_for_content(self, keyword_id, *, original_source="", title_final=""):
        item = self.storage.get_pool(keyword_id)
        if not item:
            raise KeyError(f"keyword_id={keyword_id}가 pool에 없습니다.")
        blocked = recommender._seeds_in_cooldown(
            self.storage.list_content(), self.cfg.seed_cooldown_days
        )
        if item.seed_keyword in blocked:
            raise PermissionError(
                f"seed_cooldown 위반: '{item.seed_keyword}' 는 최근 "
                f"{self.cfg.seed_cooldown_days}일 내에 사용됨"
            )
        record = ContentRecord(
            id=self._next_content_id(),
            keyword=item.keyword,
            seed_keyword=item.seed_keyword,
            keyword_id=item.keyword_id,
            original_source=original_source,
            status="대기중",
            title_final=title_final,
            created_at=to_iso(now_utc()),
            note=f"selected from pool (score={item.score})",
        )
        self.storage.append_content(record)
        self.storage.delete_pool(keyword_id)
        return record
