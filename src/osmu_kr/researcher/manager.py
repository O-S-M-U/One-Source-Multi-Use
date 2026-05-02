"""풀 관리 — 부활 심사(revival) + 정리(prune)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Tuple

from ..config import Config
from ..evaluator.base import BaseEvaluator
from ..evaluator import diagnose_weakness
from ..models import (
    GRADE_ORDER, STATUS_DEPRECATED, STATUS_EXPIRED, STATUS_GOLDEN, STATUS_MEDIUM,
    Evaluation, KeywordPoolItem, from_iso, grade_from_score, now_utc, to_iso,
)
from ..storage.base import BaseStorage
from . import alchemist

log = logging.getLogger(__name__)


@dataclass
class PruneReport:
    revaluated: int = 0
    refreshed: int = 0
    expired: int = 0
    deprecated: int = 0      # 부활 심사 미달
    transmuted: int = 0
    overflow_removed: int = 0
    revival_passed: int = 0  # 부활 심사 통과

    def summary(self):
        return (f"revaluated={self.revaluated} refreshed={self.refreshed} "
                f"expired={self.expired} deprecated={self.deprecated} "
                f"transmuted={self.transmuted} overflow={self.overflow_removed} "
                f"revival_passed={self.revival_passed}")


@dataclass
class ManageReport:
    """full_manage 의 종합 보고서."""
    prune: PruneReport = field(default_factory=PruneReport)
    pool_size_before: int = 0
    pool_size_after: int = 0
    active_count: int = 0
    top_recommendations: list = field(default_factory=list)


def _next_id(existing):
    nums = []
    for it in existing:
        try:
            nums.append(int(it.keyword_id))
        except (TypeError, ValueError):
            pass
    n = (max(nums) if nums else 0) + 1
    return f"{n:04d}"


def prune(storage: BaseStorage, evaluator: BaseEvaluator, cfg: Config,
          *, run_revival: bool = True) -> Tuple[List[KeywordPoolItem], PruneReport]:
    """REVIVAL_DAYS 경과 항목을 재평가하여 황금이면 갱신, 보통이면 알케미 변형 후 본인은 만료.

    run_revival=False 면 시간만료 시 단순 expire 마킹만(과거 동작).
    """
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

        # ── REVIVAL_DAYS 초과 → 재평가 ──
        report.revaluated += 1
        ev = evaluator.evaluate(it.keyword, seed=it.seed_keyword)
        new_grade = grade_from_score(ev.score)

        if ev.score >= cfg.golden_threshold:
            it.search_volume = ev.search_volume
            it.competition = ev.competition
            it.cpc = ev.cpc
            it.commercial_intent = ev.commercial_intent
            it.score = ev.score
            it.status = STATUS_GOLDEN
            it.grade = new_grade
            it.updated_at = to_iso(now)
            it.revival_count = (it.revival_count or 0) + 1
            it.note = (it.note + " | revival:passed").strip(" |")
            survivors.append(it)
            report.refreshed += 1
            report.revival_passed += 1
        elif cfg.medium_lower <= ev.score < cfg.medium_upper:
            # 알케미 시도 — 처방형
            weak = diagnose_weakness(ev)
            for variant in alchemist.transmute_with_diagnosis(it.keyword, weak, max_variants=5):
                v_ev = evaluator.evaluate_longtail(variant, seed=it.seed_keyword)
                if v_ev.score >= cfg.golden_threshold:
                    nv = KeywordPoolItem(
                        keyword_id=_next_id(items + survivors + new_variants),
                        seed_keyword=it.seed_keyword,
                        keyword=variant,
                        search_volume=v_ev.search_volume,
                        competition=v_ev.competition,
                        cpc=v_ev.cpc,
                        commercial_intent=v_ev.commercial_intent,
                        score=v_ev.score,
                        status=STATUS_GOLDEN,
                        source=f"{evaluator.name}+alchemy",
                        note=f"transmuted from '{it.keyword}'",
                        grade=grade_from_score(v_ev.score),
                        profile="롱테일",
                        is_alchemy="Y",
                        original_keyword=it.keyword,
                    )
                    new_variants.append(nv)
                    report.transmuted += 1
            # 원본은 만료 (보통 점수 → 알케미 처리 후 자신은 제거)
            report.expired += 1
        else:
            # 미달 → deprecated (마킹만, 풀에선 제거)
            it.status = STATUS_DEPRECATED
            report.deprecated += 1

    pool = survivors + new_variants
    if len(pool) > cfg.pool_max_size:
        pool.sort(
            key=lambda x: (-GRADE_ORDER.get(x.grade or grade_from_score(x.score), 0),
                           -x.score)
        )
        report.overflow_removed = len(pool) - cfg.pool_max_size
        pool = pool[: cfg.pool_max_size]

    storage.replace_pool(pool)
    log.info("prune complete: %s", report.summary())
    return pool, report


def full_manage(storage: BaseStorage, evaluator: BaseEvaluator, cfg: Config) -> ManageReport:
    """CLI manage 모드 — 부활 심사 + 정리 + 추천 미리보기."""
    from . import recommender
    pool_before = storage.list_pool()
    pool_after, prune_report = prune(storage, evaluator, cfg, run_revival=True)
    active = [it for it in pool_after if it.status == STATUS_GOLDEN]
    recs = recommender.recommend(storage, cfg, top_n=5)
    return ManageReport(
        prune=prune_report,
        pool_size_before=len(pool_before),
        pool_size_after=len(pool_after),
        active_count=len(active),
        top_recommendations=recs,
    )
