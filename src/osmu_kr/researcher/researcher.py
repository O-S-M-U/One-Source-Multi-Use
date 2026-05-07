"""KeywordResearcher 본체 — 사전 필터 + 단계별 파이프라인 + 부활 심사 통합."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import Config
from ..evaluator import build_evaluator, diagnose_weakness
from ..evaluator.base import BaseEvaluator
from ..evaluator.naver_golden import COMMERCIAL_WORDS as GOLDEN_COMMERCIAL_WORDS
from ..models import (
    GRADE_FAIL, GRADE_GOOD, GRADE_GOLDEN, GRADE_MEDIUM,
    STATUS_GOLDEN, STATUS_MEDIUM, STATUS_REJECTED,
    ContentRecord, Evaluation, KeywordPoolItem, ResearchHistoryRecord,
    grade_from_score, now_utc, to_iso,
)
from ..storage import BaseStorage, build_storage
from . import alchemist, expander, manager, recommender

log = logging.getLogger(__name__)


@dataclass
class SeedRunReport:
    seed: str
    expanded: int = 0
    pre_filtered: int = 0   # 사전 필터 통과 (비싼 API 호출 대상)
    accepted: int = 0
    transmuted: int = 0
    rejected: int = 0
    items: List[KeywordPoolItem] = field(default_factory=list)
    housekeeping: Optional[object] = None   # v13-E HousekeepingReport

    def summary(self):
        return (f"seed='{self.seed}' expanded={self.expanded} "
                f"pre_filtered={self.pre_filtered} accepted={self.accepted} "
                f"transmuted={self.transmuted} rejected={self.rejected}")


def quick_score_commercial(keyword: str) -> int:
    """상업적 의도만으로 빠른 사전 점수 (API 호출 없음). golden_keyword.py 호환."""
    hits = [w for w in GOLDEN_COMMERCIAL_WORDS if w in keyword]
    return 20 if len(hits) >= 2 else (15 if len(hits) == 1 else 0)


class KeywordResearcher:
    def __init__(self, cfg=None, storage=None, evaluator=None):
        self.cfg = cfg or Config()
        self.storage = storage or build_storage(self.cfg)
        self.evaluator = evaluator or build_evaluator(self.cfg)
        # v13-D: ConfigManager — env > DB config > defaults
        from ..config_manager import ConfigManager
        self.config_mgr = ConfigManager(self.storage)
        log.info("KeywordResearcher ready (storage=%s evaluator=%s)",
                 self.storage.name, self.evaluator.name)

    # ── v13-C/D: config-driven thresholds (legacy cfg 도 fallback) ──
    @property
    def golden_threshold(self) -> float:
        """env > db config > 코드 기본값(60)."""
        v = self.config_mgr.get_float("keyword.golden_threshold", 0.0)
        return v if v > 0 else float(getattr(self.cfg, "golden_threshold", 60.0))

    @property
    def crumb_upper(self) -> float:
        """v13: 부스러기/강철 경계 (default 45)."""
        return self.config_mgr.get_float("keyword.pool_eviction_score_threshold", 45.0)

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

    # ── 핵심: 사전 필터 + 단계별 파이프라인 ────────────────
    def run_seed(self, seed: str, *, expand_limit: int = 10,
                 pre_filter_top: int = 15,
                 housekeeping: bool = True) -> SeedRunReport:
        """[ 5단계 파이프라인 ]
        0. (v13-E) housekeeping — revival 재평가 + 풀삭제 정책 (매 실행 시작)
        1. expand → 후보 (자동완성 + 접미어)
        2. quick_score_commercial 로 상위 pre_filter_top 개 사전 선별 (API 호출 0)
        3. 평가기 evaluate() — 비싼 API 호출
        4. 황금/좋은(>= golden_threshold) → 풀 적재 / 보통 → 알케미 → evaluate_longtail
        5. 분석 이력(research_history) 누적
        """
        seed = (seed or "").strip()
        report = SeedRunReport(seed=seed)
        if not seed:
            return report

        # ── Step 0 (v13-E): housekeeping 내재화 ──
        if housekeeping:
            try:
                from .housekeeping import Housekeeping
                hk = Housekeeping(self.storage, evaluator=self.evaluator,
                                    config_mgr=self.config_mgr)
                hk_report = hk.run()
                log.info("▶ housekeeping: %s", hk_report.summary())
                report.housekeeping = hk_report
            except Exception as e:
                log.warning("[run_seed] housekeeping 실패(무시): %s", e)

        # ── Step 1: 후보 확장 ──
        candidates = expander.expand(seed, limit=expand_limit)
        report.expanded = len(candidates)

        # ── Step 2: 사전 필터 (API 호출 없음) ──
        # 씨앗 키워드 자체는 항상 포함, 나머지는 quick_score 내림차순
        scored_candidates = sorted(
            candidates,
            key=lambda kw: (0 if kw == seed else -quick_score_commercial(kw))
        )
        top_candidates = scored_candidates[:pre_filter_top]
        report.pre_filtered = len(top_candidates)

        accepted_buf = []

        # ── Step 3-4: 본 평가 + 알케미 ──
        for kw in top_candidates:
            if self.storage.find_pool_by_keyword(kw):
                continue
            ev = self.evaluator.evaluate(kw, seed=seed)

            self._record_history(kw, ev, seed=seed, profile="일반",
                                  is_alchemy=False, original_keyword="")

            if ev.score >= self.cfg.golden_threshold:
                item = self._build_pool_item(seed, kw, ev, accepted_buf,
                                              profile="일반", is_alchemy=False)
                accepted_buf.append(item)
                report.items.append(item)
                report.accepted += 1
            elif self.cfg.medium_lower <= ev.score < self.cfg.medium_upper:
                weaknesses = diagnose_weakness(ev)
                added_any = False
                for variant in alchemist.transmute_with_diagnosis(
                        kw, weaknesses, max_variants=5):
                    if self.storage.find_pool_by_keyword(variant):
                        continue
                    v_ev = self.evaluator.evaluate_longtail(variant, seed=seed)
                    self._record_history(variant, v_ev, seed=seed,
                                          profile="롱테일", is_alchemy=True,
                                          original_keyword=kw)
                    if v_ev.score >= self.cfg.golden_threshold:
                        item = self._build_pool_item(
                            seed, variant, v_ev, accepted_buf,
                            source_suffix="+alchemy",
                            note=f"transmuted from '{kw}'",
                            profile="롱테일", is_alchemy=True,
                            original_keyword=kw,
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

    def _record_history(self, keyword, ev, *, seed, profile, is_alchemy, original_keyword):
        """research_history 시트에 분석 시점 스냅샷 기록."""
        try:
            raw = ev.raw or {}
            dl = raw.get("datalab") or {}
            blog = raw.get("blog") or {}
            gt = raw.get("google_trends") or {}
            comp = raw.get("commercial_hits") or []
            weak = []
            comp_scores = raw.get("components") or {}
            weights = raw.get("weights") or {}
            if comp_scores and weights:
                if (comp_scores.get("commercial", 0) / max(1, weights.get("commercial", 20))) < 0.50:
                    weak.append("상업의도부족")
                if (comp_scores.get("blog", 0) / max(1, weights.get("blog_comp", 30))) < 0.40:
                    weak.append("경쟁도높음")
                if (comp_scores.get("datalab", 0) / max(1, weights.get("datalab", 40))) < 0.40:
                    weak.append("트렌드낮음")

            rec = ResearchHistoryRecord(
                keyword=keyword,
                grade=grade_from_score(ev.score, golden_threshold=self.golden_threshold, crumb_upper=self.crumb_upper),
                total_score=round(ev.score, 2),
                profile=profile,
                datalab_score=float(dl.get("trend_score", 0) or 0),
                datalab_direction=str(dl.get("trend_direction", "")),
                blog_results=str(blog.get("total_results", "")) if blog.get("total_results") is not None else "",
                blog_competition=str(blog.get("competition_label", "")),
                commercial_hits=", ".join(comp) if comp else "없음",
                gtrends_score=float(gt.get("trend_score", 0) or 0),
                weak_points=", ".join(weak) if weak else "-",
                is_alchemy="Y" if is_alchemy else "N",
                original_keyword=original_keyword,
                seed_keyword=seed,
                evaluator=self.evaluator.name,
            )
            self.storage.append_history(rec)
        except Exception as e:
            log.warning("history 기록 실패 (무시): %s", e)

    def _build_pool_item(self, seed, keyword, ev, extra,
                         source_suffix="", note="",
                         profile="일반", is_alchemy=False, original_keyword=""):
        # 약점 진단
        weak = diagnose_weakness(ev) if profile == "일반" else []
        item = KeywordPoolItem(
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
            grade=grade_from_score(ev.score, golden_threshold=self.golden_threshold, crumb_upper=self.crumb_upper),
            profile=profile,
            weak_points=", ".join(w.replace("_", "") for w in weak),
            is_alchemy="Y" if is_alchemy else "N",
            original_keyword=original_keyword,
            revival_count=0,
        )
        return item

    def _enforce_max_size(self):
        pool = self.storage.list_pool()
        if len(pool) <= self.cfg.pool_max_size:
            return
        # 등급 우선, 그 다음 점수 — golden_keyword.py 정책
        from ..models import GRADE_ORDER
        pool.sort(
            key=lambda x: (-GRADE_ORDER.get(x.grade or grade_from_score(x.score), 0),
                           -x.score)
        )
        self.storage.replace_pool(pool[: self.cfg.pool_max_size])

    def check_keyword(self, keyword, *, seed=None):
        keyword = (keyword or "").strip()
        if not keyword:
            raise ValueError("keyword 가 비어 있습니다.")
        seed = (seed or keyword).strip()
        existing = self.storage.find_pool_by_keyword(keyword)
        ev = self.evaluator.evaluate(keyword, seed=seed)
        self._record_history(keyword, ev, seed=seed, profile="일반",
                              is_alchemy=False, original_keyword="")
        if existing:
            existing.search_volume = ev.search_volume
            existing.competition = ev.competition
            existing.cpc = ev.cpc
            existing.commercial_intent = ev.commercial_intent
            existing.score = ev.score
            existing.updated_at = to_iso(now_utc())
            existing.status = STATUS_GOLDEN if ev.score >= self.cfg.golden_threshold else STATUS_MEDIUM
            existing.grade = grade_from_score(ev.score)
            self.storage.upsert_pool(existing)
            return existing

        status = (STATUS_GOLDEN if ev.score >= self.cfg.golden_threshold
                  else (STATUS_MEDIUM if ev.score >= self.cfg.medium_lower else STATUS_REJECTED))
        weak = diagnose_weakness(ev)
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
            grade=grade_from_score(ev.score, golden_threshold=self.golden_threshold, crumb_upper=self.crumb_upper),
            profile="일반",
            weak_points=", ".join(w.replace("_", "") for w in weak),
            is_alchemy="N",
            revival_count=0,
        )
        if status != STATUS_REJECTED:
            self.storage.upsert_pool(item)
        return item

    def prune(self, *, run_revival: bool = True):
        """기본은 부활 심사 포함. False면 단순 만료만."""
        return manager.prune(self.storage, self.evaluator, self.cfg,
                              run_revival=run_revival)

    def manage(self):
        """CLI manage 모드 — 부활 심사 + 정리 + 추천 미리보기."""
        return manager.full_manage(self.storage, self.evaluator, self.cfg)

    def recommend(self, top_n=5):
        return recommender.recommend(self.storage, self.cfg, top_n=top_n)

    def select_for_content(self, keyword_id, *, original_source="", title_final="",
                            account_id="", blog_id=""):
        """v13: 선택 → ContentRecord 생성 + keyword_usages(in_progress) lock.

        - keyword.status 자체는 active 로 둠. lock 은 keyword_usages 가 가짐.
        - 같은 keyword 에 in_progress 가 이미 있으면 LockBusy.
        - keyword 가 archived 면 거부.
        - 작업 중단 시 SafetyLayer.mark_failed(usage_id) 로 잠금 해제.
        """
        from .safety import LockBusy, SafetyLayer, TransitionError

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

        # keyword_usages 에 in_progress lock 생성
        safety = SafetyLayer(self.storage)
        try:
            usage = safety.start_lock(
                keyword_id,
                account_id=account_id, blog_id=blog_id,
                contents_id=record.id,
                note=f"select title={title_final[:50]}",
            )
            # ContentRecord 에 usage 연결 (note 에 기록)
            self.storage.update_content(
                record.id, note=record.note + f" | usage={usage.id}"
            )
        except (LockBusy, TransitionError) as e:
            # 컨텐츠 레코드는 살려두되 (작업 흔적), 사용자에게 명확한 에러
            raise PermissionError(f"키워드 lock 실패: {e}")

        return record
