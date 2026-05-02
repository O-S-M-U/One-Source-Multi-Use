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
