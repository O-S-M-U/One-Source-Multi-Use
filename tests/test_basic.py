"""기본 동작 검증."""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from osmu_kr import Config, KeywordResearcher
from osmu_kr.evaluator import HeuristicEvaluator
from osmu_kr.researcher.alchemist import transmute
from osmu_kr.researcher.expander import expand


def fresh_researcher():
    tmp = tempfile.mkdtemp(prefix="osmu_test_")
    os.environ["OSMU_LOCAL_DATA_DIR"] = tmp
    os.environ["OSMU_STORAGE_BACKEND"] = "local"
    os.environ["OSMU_LOCAL_FORMAT"] = "csv"
    os.environ["OSMU_GOLDEN_THRESHOLD"] = "65"
    os.environ["OSMU_MEDIUM_LOWER"] = "45"
    os.environ["OSMU_MEDIUM_UPPER"] = "65"
    os.environ["OSMU_POOL_MAX_SIZE"] = "100"
    return KeywordResearcher(Config())


def test_evaluator_deterministic():
    e = HeuristicEvaluator()
    a = e.evaluate("AI ETF 추천 2025")
    b = e.evaluate("AI ETF 추천 2025")
    assert a.score == b.score
    assert 0 <= a.score <= 100


def test_expander_includes_seed_and_dedups():
    out = expand("다이어트", limit=10, use_autocomplete=False)
    assert "다이어트" in out
    assert len(set(out)) == len(out)


def test_alchemy_produces_distinct_variants():
    out = transmute("다이어트", max_variants=3)
    assert len(out) == 3
    for v in out:
        assert "다이어트" in v
        assert v != "다이어트"


def test_run_seed_creates_pool_items():
    rs = fresh_researcher()
    rep = rs.run_seed("AI ETF")
    pool = rs.storage.list_pool()
    assert len(pool) >= 1
    assert rep.expanded > 0


def test_select_records_content_and_removes_from_pool():
    rs = fresh_researcher()
    rs.run_seed("AI ETF")
    pool_before = rs.storage.list_pool()
    pick = pool_before[0]
    rs.select_for_content(pick.keyword_id, title_final="t")
    pool_after = rs.storage.list_pool()
    assert pick.keyword_id not in {it.keyword_id for it in pool_after}
    contents = rs.storage.list_content()
    assert any(r.keyword_id == pick.keyword_id for r in contents)


def test_seed_cooldown_blocks_same_seed():
    os.environ["OSMU_SEED_COOLDOWN_DAYS"] = "7"
    rs = fresh_researcher()
    rs.run_seed("다이어트")
    pool = [it for it in rs.storage.list_pool() if it.seed_keyword == "다이어트"]
    assert len(pool) >= 2
    rs.select_for_content(pool[0].keyword_id, title_final="t")
    try:
        rs.select_for_content(pool[1].keyword_id, title_final="t2")
    except PermissionError:
        return
    raise AssertionError("동일 seed cooldown이 적용되지 않음")


def test_prune_removes_expired():
    from datetime import timedelta
    from osmu_kr.models import to_iso, now_utc
    os.environ["OSMU_REVIVAL_DAYS"] = "0.1"
    rs = fresh_researcher()
    rs.run_seed("AI ETF")
    items = rs.storage.list_pool()
    past = now_utc() - timedelta(days=1)
    for it in items:
        it.updated_at = to_iso(past)
        it.created_at = to_iso(past)
    rs.storage.replace_pool(items)
    pool, report = rs.prune()
    assert report.revaluated == len(items)


def test_xlsx_storage_round_trip():
    from osmu_kr.storage.xlsx_local import LocalXlsxStorage
    from osmu_kr.models import KeywordPoolItem
    tmp = tempfile.mkdtemp(prefix="osmu_xlsx_")
    sx = LocalXlsxStorage(data_dir=tmp)
    sx.upsert_pool(KeywordPoolItem(keyword_id="0001", seed_keyword="다이어트",
                                    keyword="다이어트 추천", score=82.5, status="golden",
                                    search_volume=12000, cpc=750.0, competition="낮음"))
    sx2 = LocalXlsxStorage(data_dir=tmp)
    pool = sx2.list_pool()
    assert len(pool) == 1
    assert pool[0].keyword == "다이어트 추천"


def test_factory_xlsx_format():
    from osmu_kr.storage import build_storage
    tmp = tempfile.mkdtemp(prefix="osmu_factory_xlsx_")
    os.environ["OSMU_STORAGE_BACKEND"] = "local"
    os.environ["OSMU_LOCAL_FORMAT"] = "xlsx"
    os.environ["OSMU_LOCAL_DATA_DIR"] = tmp
    storage = build_storage(Config())
    assert storage.name == "xlsx"


def test_naver_golden_evaluator_falls_back_to_heuristic_without_creds():
    from osmu_kr.evaluator import NaverGoldenEvaluator
    os.environ.pop("NAVER_CLIENT_ID", None)
    os.environ.pop("NAVER_CLIENT_SECRET", None)
    ev = NaverGoldenEvaluator()
    res = ev.evaluate("다이어트 추천")
    assert 0 <= res.score <= 100
    assert "fallback" in res.raw.get("evaluator", "")


def test_mirror_storage_falls_back_to_local_when_no_credentials():
    from osmu_kr.storage.csv_local import LocalCsvStorage
    from osmu_kr.storage.mirror import MirrorStorage
    from osmu_kr.models import KeywordPoolItem
    tmp = tempfile.mkdtemp(prefix="osmu_mirror_")
    local = LocalCsvStorage(data_dir=tmp)

    def factory_fail():
        raise RuntimeError("no credentials")

    mirror = MirrorStorage(local=local, sheets_factory=factory_fail)
    item = KeywordPoolItem(keyword_id="0001", seed_keyword="t",
                            keyword="테스트 키워드", score=80.0, status="golden")
    mirror.upsert_pool(item)
    assert any(it.keyword_id == "0001" for it in mirror.list_pool())
    s = mirror.status()
    assert s.pending_writes >= 1
    assert s.sheets_enabled is False


def test_pool_item_grade_autofill():
    from osmu_kr.models import KeywordPoolItem, grade_from_score
    item = KeywordPoolItem(keyword_id="0001", seed_keyword="t", keyword="kw", score=85.0)
    item.fill_grade()
    assert item.grade == "황금"
    item2 = KeywordPoolItem(keyword_id="0002", seed_keyword="t", keyword="kw2", score=50.0)
    item2.fill_grade()
    assert item2.grade == "보통"
    assert grade_from_score(95) == "황금"
    assert grade_from_score(70) == "좋은"
    assert grade_from_score(20) == "미달"


def test_research_history_round_trip():
    from osmu_kr.storage.csv_local import LocalCsvStorage
    from osmu_kr.models import ResearchHistoryRecord
    tmp = tempfile.mkdtemp(prefix="osmu_history_")
    s = LocalCsvStorage(data_dir=tmp)
    rec = ResearchHistoryRecord(keyword="다이어트 추천", grade="황금",
                                  total_score=85.0, profile="일반",
                                  seed_keyword="다이어트", evaluator="heuristic")
    s.append_history(rec)
    out = s.list_history()
    assert len(out) == 1
    assert out[0].keyword == "다이어트 추천"
    assert out[0].grade == "황금"


def test_pre_filter_pipeline_records_history():
    rs = fresh_researcher()
    rep = rs.run_seed("다이어트")
    history = rs.storage.list_history()
    # 분석한 모든 키워드가 이력에 기록됨 (사전 필터 통과 분량)
    assert len(history) >= rep.pre_filtered
    # pool item에 grade 채워졌는지
    for it in rs.storage.list_pool():
        assert it.grade in ("황금", "좋은", "보통", "미달")


def test_manage_full_pipeline():
    """CLI manage 모드 — full_manage 가 ManageReport 를 정상 반환."""
    rs = fresh_researcher()
    rs.run_seed("AI ETF")
    report = rs.manage()
    assert report.pool_size_after >= 0
    assert hasattr(report.prune, "revaluated")


def test_revival_deprecates_low_score():
    """REVIVAL_DAYS 경과 + 점수 미달 → deprecated 마킹 후 풀에서 제거.

    결정적 테스트를 위해 항상 score=10 만 반환하는 평가기를 주입한다.
    """
    from datetime import timedelta
    from osmu_kr.models import to_iso, now_utc, KeywordPoolItem, Evaluation
    from osmu_kr.evaluator.base import BaseEvaluator
    from osmu_kr import KeywordResearcher, Config

    class AlwaysLowEvaluator(BaseEvaluator):
        name = "always_low"
        def evaluate(self, keyword, *, seed=""):
            return Evaluation(score=10.0, raw={"evaluator": "always_low"})

    tmp = tempfile.mkdtemp(prefix="osmu_revival_")
    os.environ["OSMU_LOCAL_DATA_DIR"] = tmp
    os.environ["OSMU_STORAGE_BACKEND"] = "local"
    os.environ["OSMU_LOCAL_FORMAT"] = "csv"
    os.environ["OSMU_REVIVAL_DAYS"] = "0.1"
    os.environ["OSMU_GOLDEN_THRESHOLD"] = "65"
    os.environ["OSMU_MEDIUM_LOWER"] = "45"
    os.environ["OSMU_MEDIUM_UPPER"] = "65"
    rs = KeywordResearcher(Config(), evaluator=AlwaysLowEvaluator())

    fake = KeywordPoolItem(
        keyword_id="9999", seed_keyword="저품질", keyword="저품질 샘플",
        score=80.0, status="golden",
    )
    rs.storage.upsert_pool(fake)
    items = rs.storage.list_pool()
    past = now_utc() - timedelta(days=1)
    for it in items:
        it.updated_at = to_iso(past)
        it.created_at = to_iso(past)
    rs.storage.replace_pool(items)

    pool, report = rs.prune(run_revival=True)
    assert "9999" not in {it.keyword_id for it in pool}, "deprecated 처리 실패"
    assert report.deprecated >= 1


TESTS = [
    test_evaluator_deterministic,
    test_expander_includes_seed_and_dedups,
    test_alchemy_produces_distinct_variants,
    test_run_seed_creates_pool_items,
    test_select_records_content_and_removes_from_pool,
    test_seed_cooldown_blocks_same_seed,
    test_prune_removes_expired,
    test_xlsx_storage_round_trip,
    test_factory_xlsx_format,
    test_naver_golden_evaluator_falls_back_to_heuristic_without_creds,
    test_mirror_storage_falls_back_to_local_when_no_credentials,
    test_pool_item_grade_autofill,
    test_research_history_round_trip,
    test_pre_filter_pipeline_records_history,
    test_manage_full_pipeline,
    test_revival_deprecates_low_score,
]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\nresult: {len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
