"""housekeeping (v13-E) — keyword_researcher 매 실행 시 내재화.

[ v13 spec ]
  매 실행 시작 시 자동 수행:
    1) revival_days 초과 키워드 스캔 → 재평가 (점수 갱신, keyword_evaluations 기록)
    2) 재평가 결과 저품질 → keywords.status = archived
    3) pool_max_size 초과 시 풀 삭제 정책 실행:
       - 1순위: evaluation_count >= keyword.pool_eviction_eval_count(3) AND
                avg_score < keyword.pool_eviction_score_threshold(45)
                정렬: last_evaluated_at ASC (오래된 순)
       - 2순위 fallback: 1순위로 부족하면
                정렬: last_evaluated_at ASC, total_score(가장 최근) ASC
       - 공통 예외: status='archived' 는 풀 카운트 제외, published 키워드(keyword_usages.status='published')는 삭제 대상 제외

[ 사용 ]
  hk = Housekeeping(rs.storage, evaluator=..., config_mgr=...)
  report = hk.run()
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from ..config_manager import ConfigManager
from ..evaluator.base import BaseEvaluator
from ..models import (
    KSTATUS_ACTIVE, KSTATUS_ARCHIVED, KeywordPoolItem, ResearchHistoryRecord,
    USAGE_PUBLISHED, from_iso, normalize_status, now_utc, to_iso,
    grade_from_score,
)
from ..storage.base import BaseStorage

log = logging.getLogger(__name__)


@dataclass
class HousekeepingReport:
    re_evaluated: int = 0
    archived_low_quality: int = 0
    evicted_primary: int = 0     # 1순위 — 저품질 반복
    evicted_fallback: int = 0    # 2순위 — last_evaluated_at + total_score
    pool_size_before: int = 0
    pool_size_after: int = 0
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"re_eval={self.re_evaluated} archived={self.archived_low_quality} "
            f"evict[1순위={self.evicted_primary} 2순위={self.evicted_fallback}] "
            f"pool {self.pool_size_before}→{self.pool_size_after}"
        )


class Housekeeping:
    def __init__(self, storage: BaseStorage,
                 *, evaluator: Optional[BaseEvaluator] = None,
                 config_mgr: Optional[ConfigManager] = None):
        self.storage = storage
        self.evaluator = evaluator
        self.config_mgr = config_mgr or ConfigManager(storage)

    # ── 공개 API ──────────────────────────────────────
    def run(self) -> HousekeepingReport:
        report = HousekeepingReport()
        pool = self._active_pool()
        report.pool_size_before = len(pool)

        # 1) revival 재평가
        if self.evaluator is not None:
            report.re_evaluated, report.archived_low_quality = self._revival_pass(pool)
            pool = self._active_pool()  # 재로드 (archive 처리 반영)

        # 2) 풀 삭제 정책
        max_size = self.config_mgr.get_int("keyword.pool_max_size", 50)
        if len(pool) > max_size:
            excess = len(pool) - max_size
            primary = self._evict_primary(pool, excess)
            report.evicted_primary = len(primary)
            still_excess = excess - len(primary)
            if still_excess > 0:
                pool = self._active_pool()
                fallback = self._evict_fallback(pool, still_excess)
                report.evicted_fallback = len(fallback)

        report.pool_size_after = len(self._active_pool())
        log.info("[housekeeping] %s", report.summary())
        return report

    # ── 내부 — revival ───────────────────────────────
    def _revival_pass(self, pool: List[KeywordPoolItem]) -> tuple:
        """revival_days 초과 키워드 재평가 → 저품질이면 archive."""
        if self.evaluator is None:
            return (0, 0)
        revival_days = self.config_mgr.get_float("keyword.revival_days", 30.0)
        crumb_upper = self.config_mgr.get_float(
            "keyword.pool_eviction_score_threshold", 45.0,
        )
        cutoff = now_utc() - timedelta(days=revival_days)
        re_evaluated = 0
        archived = 0
        for item in pool:
            stamp = item.last_evaluated_at or item.updated_at
            try:
                last_eval = from_iso(stamp) if stamp else None
            except Exception:
                last_eval = None
            if last_eval is None or last_eval < cutoff:
                # 재평가 실행
                try:
                    ev = self.evaluator.evaluate(item.keyword, seed=item.seed_keyword)
                except Exception as e:
                    log.warning("[housekeeping] re-eval 실패 %s: %s", item.keyword, e)
                    continue
                item.score = ev.score
                item.last_evaluated_at = to_iso(now_utc())
                item.updated_at = item.last_evaluated_at
                # 점수 부족 → archive
                if ev.score <= crumb_upper:
                    item.status = KSTATUS_ARCHIVED
                    item.archived_at = to_iso(now_utc())
                    item.last_status_reason = (
                        f"housekeeping_archive: score={ev.score:.1f} <= {crumb_upper}"
                    )
                    archived += 1
                else:
                    item.grade = grade_from_score(ev.score, crumb_upper=crumb_upper)
                self.storage.upsert_pool(item)
                # keyword_evaluations 이벤트 누적
                try:
                    self.storage.append_history(ResearchHistoryRecord(
                        keyword=item.keyword,
                        grade=item.grade,
                        total_score=ev.score,
                        seed_keyword=item.seed_keyword,
                        evaluator=self.evaluator.name,
                    ))
                except Exception:
                    pass
                re_evaluated += 1
        return (re_evaluated, archived)

    # ── 내부 — 1순위/2순위 풀 삭제 ───────────────────
    def _published_kid_set(self) -> set:
        return {u.keyword_id for u in self.storage.list_usages()
                if u.status == USAGE_PUBLISHED}

    def _evaluation_stats(self, keyword: str) -> tuple:
        """keyword_evaluations 기반 (count, avg_score, last_total)."""
        scores = []
        last_total = None
        for h in self.storage.list_history():
            if h.keyword == keyword:
                scores.append(h.total_score)
                last_total = h.total_score   # ordered by created_at
        if not scores:
            return (0, 0.0, None)
        return (len(scores), statistics.mean(scores), last_total)

    def _evict_primary(self, pool: List[KeywordPoolItem],
                        excess: int) -> List[KeywordPoolItem]:
        """1순위: eval_count >= 3 AND avg_score < 45, last_evaluated_at ASC."""
        eval_count_min = self.config_mgr.get_int("keyword.pool_eviction_eval_count", 3)
        score_thr = self.config_mgr.get_float(
            "keyword.pool_eviction_score_threshold", 45.0,
        )
        published = self._published_kid_set()
        candidates = []
        for it in pool:
            if it.keyword_id in published:
                continue
            count, avg, _ = self._evaluation_stats(it.keyword)
            if count >= eval_count_min and avg < score_thr:
                candidates.append((it, count, avg))
        # last_evaluated_at ASC
        candidates.sort(key=lambda c: c[0].last_evaluated_at or c[0].updated_at)
        evicted = []
        for c, _cnt, _avg in candidates[:excess]:
            self.storage.delete_pool(c.keyword_id)
            evicted.append(c)
            log.info("[housekeeping] 1순위 eviction: %s (avg=%.1f, count=%d)",
                      c.keyword_id, _avg, _cnt)
        return evicted

    def _evict_fallback(self, pool: List[KeywordPoolItem],
                          excess: int) -> List[KeywordPoolItem]:
        """2순위: last_evaluated_at ASC → total_score(최근) ASC, 평균 임계 미적용."""
        published = self._published_kid_set()
        candidates = []
        for it in pool:
            if it.keyword_id in published:
                continue
            _, _, last_total = self._evaluation_stats(it.keyword)
            candidates.append((it, last_total or it.score))
        candidates.sort(
            key=lambda c: (c[0].last_evaluated_at or c[0].updated_at, c[1])
        )
        evicted = []
        for c, _ in candidates[:excess]:
            self.storage.delete_pool(c.keyword_id)
            evicted.append(c)
            log.info("[housekeeping] 2순위 eviction: %s", c.keyword_id)
        return evicted

    # ── 헬퍼 ──────────────────────────────────────────
    def _active_pool(self) -> List[KeywordPoolItem]:
        return [it for it in self.storage.list_pool()
                if normalize_status(it.status) == KSTATUS_ACTIVE]
