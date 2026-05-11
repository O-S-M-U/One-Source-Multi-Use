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


def test_select_records_content_and_locks_via_usage():
    """v13: select 시 pool 에서 삭제하지 않고 keyword_usages(in_progress) 로 lock.

    keyword.status 는 active 로 유지 — lock 은 keyword_usages 가 책임.
    """
    from osmu_kr.models import (
        KSTATUS_ACTIVE, USAGE_IN_PROGRESS, normalize_status,
    )
    rs = fresh_researcher()
    rs.run_seed("AI ETF")
    pool_before = rs.storage.list_pool()
    pick = pool_before[0]
    rs.select_for_content(pick.keyword_id, title_final="t")

    # keyword 는 풀에 그대로 + status=active
    pool_after = rs.storage.list_pool()
    found = [it for it in pool_after if it.keyword_id == pick.keyword_id]
    assert len(found) == 1
    assert normalize_status(found[0].status) == KSTATUS_ACTIVE

    # keyword_usages 에 in_progress lock 한 건
    active_lock = rs.storage.get_active_usage(pick.keyword_id)
    assert active_lock is not None
    assert active_lock.status == USAGE_IN_PROGRESS
    assert active_lock.started_at

    # contents 도 한 건 적재
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
    """v13: 등급 부스러기/강철/황금. legacy alias(GRADE_GOOD/GRADE_MEDIUM/GRADE_FAIL) 도 호환."""
    from osmu_kr.models import KeywordPoolItem, grade_from_score
    item = KeywordPoolItem(keyword_id="0001", seed_keyword="t", keyword="kw", score=85.0)
    item.fill_grade()
    assert item.grade == "황금"
    item2 = KeywordPoolItem(keyword_id="0002", seed_keyword="t", keyword="kw2", score=50.0)
    item2.fill_grade()
    assert item2.grade == "강철"   # v13: 보통 → 강철
    assert grade_from_score(95) == "황금"
    assert grade_from_score(70) == "황금"  # v13: golden_threshold=60 default
    assert grade_from_score(20) == "부스러기"  # v13: 미달 → 부스러기


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


def test_keyword_translator_korean_to_english_and_slug():
    from osmu_kr.content_generator.keyword_translator import (
        translate_to_english_queries, keyword_to_slug, make_filename,
    )
    qs = translate_to_english_queries("직장인 다이어트 식단", max_queries=3)
    assert qs, "후보 비어 있으면 안 됨"
    joined = " ".join(qs).lower()
    assert "diet" in joined and "meal" in joined

    slug = keyword_to_slug("직장인 다이어트 식단")
    assert slug == "office-diet-meal"

    slug2 = keyword_to_slug("AI ETF 추천 2025")
    assert "ai" in slug2 and "etf" in slug2

    fn = make_filename(slug, 1, "jpg")
    assert fn == "office-diet-meal-1.jpg"


def test_picsum_image_provider_returns_image_items_with_roles():
    """폴백은 항상 동작 + role 부여 (concept/example/comparison)."""
    from osmu_kr.content_generator.images import PicsumImageProvider
    from osmu_kr.content_generator.interfaces import ImageItem
    p = PicsumImageProvider()
    items = p.search("직장인 다이어트 식단", count=3)
    assert len(items) == 3
    expected_roles = ["concept", "example", "comparison"]
    for i, it in enumerate(items, 1):
        assert isinstance(it, ImageItem)
        assert it.url.startswith("https://picsum.photos/")
        assert it.filename == f"office-diet-meal-{i}.jpg"
        assert it.source == "picsum"
        assert it.role == expected_roles[i - 1]
        # alt 텍스트에 한국어 역할 라벨 포함
        assert ("직장인 다이어트 식단" in it.alt or "관련 이미지" in it.alt)
        assert it.caption  # caption 채워짐


def test_html_validator_detects_banned_phrases():
    from osmu_kr.content_generator.writer import validate_html_structure
    bad = ('<h1>x</h1><h2>1</h2><p>외부 검색이 일시적으로 어려워 기본 가이드를 보여드립니다.</p>'
           '<h2>2</h2><p>본문</p><h2>3</h2><p>본문</p>'
           '<img src="a"/><img src="b"/>')
    issues = validate_html_structure(bad, expected_image_count=2, min_h2=3, min_p=3)
    assert any(i.startswith("banned_phrase") for i in issues)


def test_strip_banned_phrases_removes_offending_paragraphs():
    from osmu_kr.content_generator.writer import strip_banned_phrases
    html = ('<h1>x</h1>'
            '<p>이 글은 외부 검색이 일시적으로 어려워 작성됐습니다.</p>'
            '<p>이건 정상 본문 단락입니다. 충분한 내용을 담고 있습니다.</p>')
    cleaned = strip_banned_phrases(html)
    assert "외부 검색이" not in cleaned
    assert "이건 정상 본문 단락입니다" in cleaned


def test_heuristic_writer_no_banned_phrases_and_4_sections():
    """폴백 Writer 도 신뢰도 저하 표현 없고 H2 4개 + 충분한 단락 생성."""
    from osmu_kr.content_generator.writer import HeuristicWriter, BANNED_PHRASES
    from osmu_kr.content_generator.interfaces import ImageItem
    w = HeuristicWriter()
    images = [
        ImageItem(url="https://x/1.jpg", filename="a-1.jpg", alt="a", role="concept"),
        ImageItem(url="https://x/2.jpg", filename="a-2.jpg", alt="b", role="example"),
        ImageItem(url="https://x/3.jpg", filename="a-3.jpg", alt="c", role="comparison"),
    ]
    html = w.write("AI ETF 추천", "", images=images, sources=[])
    # H2 4개 (개념/활용/주의/요약)
    h2_count = html.lower().count("<h2")
    assert h2_count >= 4
    # 금지 표현 없음
    for phrase in BANNED_PHRASES:
        assert phrase not in html, f"금지 표현 발견: {phrase}"
    # 이미지 3개 모두 figure + figcaption
    assert html.count("<img") >= 2
    assert html.count("<figcaption") >= 2
    # role 속성 유지
    assert 'data-role="concept"' in html
    assert 'data-role="example"' in html


def test_chained_image_provider_dedup_and_renumber():
    """ChainedImageProvider 가 다중 Provider 결과를 합치고 파일명 일관 적용."""
    from osmu_kr.content_generator.images import (
        ChainedImageProvider, PicsumImageProvider,
    )
    chain = ChainedImageProvider([PicsumImageProvider(), PicsumImageProvider()])
    items = chain.search("AI ETF 추천", count=3)
    assert len(items) == 3
    # 파일명이 인덱스 1,2,3 순서로 매겨졌는지
    for i, it in enumerate(items, 1):
        assert it.filename.endswith(f"-{i}.jpg")


def test_heuristic_writer_produces_html_with_images():
    """LLM 자격증명 없을 때 폴백 writer 가 H1/H2/이미지 포함 HTML 생성 + ImageItem 처리."""
    from osmu_kr.content_generator.writer import HeuristicWriter
    from osmu_kr.content_generator.interfaces import ImageItem
    w = HeuristicWriter()
    images = [
        ImageItem(url="https://example/1.jpg", filename="diet-1.jpg",
                  alt="다이어트 추천 관련 이미지 1", source="test"),
        ImageItem(url="https://example/2.jpg", filename="diet-2.jpg",
                  alt="다이어트 추천 관련 이미지 2", source="test"),
    ]
    html = w.write("다이어트 추천",
                    "다이어트는 식단 조절과 운동을 병행해야 합니다. "
                    "단기간에 살을 빼려면 칼로리 적자가 필수입니다.",
                    sources=["https://example.com/a"],
                    images=images)
    assert "<h1>" in html.lower()
    assert "<h2>" in html.lower()
    assert html.count("<img") >= 2
    # data-filename 속성 포함
    assert 'data-filename="diet-1.jpg"' in html
    # alt 텍스트도 포함
    assert "다이어트 추천 관련 이미지" in html


def test_html_validation_detects_missing_images():
    from osmu_kr.content_generator.writer import validate_html_structure
    bad = "<h1>제목</h1><h2>본문</h2><p>글</p>"
    # 이미지 누락 검출 — 다른 검증은 느슨하게(min_h2=1, min_p=1)
    issues = validate_html_structure(bad, expected_image_count=2, min_h2=1, min_p=1)
    assert any(i.startswith("insufficient_images") for i in issues)

    good = ('<h1>x</h1><h2>y</h2><p>z</p>'
            '<img src="https://a/1.jpg" alt="a 1"/>'
            '<img src="https://a/2.jpg" alt="a 2"/>')
    assert validate_html_structure(good, expected_image_count=2, min_h2=1, min_p=1) == []


def test_repair_missing_images_appends_when_writer_skips():
    from osmu_kr.content_generator.writer import repair_missing_images
    from osmu_kr.content_generator.interfaces import ImageItem
    html_no_img = "<h1>x</h1><h2>섹션1</h2><p>본문</p><h2>섹션2</h2><p>본문</p>"
    images = [
        ImageItem(url="https://a/1.jpg", filename="a-1.jpg", alt="a 1"),
        ImageItem(url="https://a/2.jpg", filename="a-2.jpg", alt="a 2"),
    ]
    fixed = repair_missing_images(html_no_img, images)
    assert fixed.count("<img") >= 2


def test_collector_with_stub_crawler():
    """Crawler 스텁 → Collector 가 raw_content 합성."""
    from osmu_kr.content_generator.collector import Collector
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, query, *, limit=5):
            return [f"https://x.com/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(
                url=url, title=f"제목 {url[-1]}",
                content=("이 글은 다이어트에 대한 내용입니다. "
                         "단기간 다이어트는 추천하지 않습니다. "
                         "꾸준한 식단 관리가 핵심입니다."),
            )

    c = Collector(StubCrawler(), min_sources=3)
    raw = c.collect("다이어트", limit=3)
    assert raw.char_count > 0
    assert len(raw.sources) == 3
    # dedup 으로 동일 문장 3번 들어가지 않음
    assert raw.text.count("이 글은 다이어트에 대한 내용입니다") == 1


def test_generator_full_pipeline_with_stubs():
    """모든 의존성 stub → Generator 전체 흐름 + ImageItem + JSON image_urls 검증."""
    import json as _json
    from osmu_kr.content_generator import Generator
    from osmu_kr.content_generator.generator import GeneratorConfig
    from osmu_kr.content_generator.interfaces import (
        BaseCrawler, BaseWriter, BaseImageProvider, CrawledPage, ImageItem,
    )
    from osmu_kr.storage.csv_local import LocalCsvStorage

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, query, *, limit=5):
            return [f"https://news.example/{query}/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(
                url=url, title="기사 제목",
                content=(
                    f"이 기사는 {url} 에서 가져온 본문입니다. "
                    "다이어트와 관련된 다양한 정보를 정리한 글입니다. "
                    "단기간 다이어트보다 꾸준한 식단 관리가 더 효과적입니다."
                ),
            )

    class StubWriter(BaseWriter):
        name = "stub_writer"
        def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적"):
            tags = "".join(
                f'<img src="{im.url}" alt="{im.alt}" data-filename="{im.filename}"/>'
                for im in (images or [])
            )
            return (f"<h1>{keyword}</h1><h2>본문</h2><p>{raw_content[:80]}</p>{tags}"
                    f"<h2>마무리</h2><p>요약</p>")

    class StubImages(BaseImageProvider):
        name = "stub_images"
        def search(self, query, *, count=3, slug="", alt_keyword=""):
            return [
                ImageItem(
                    url=f"https://img.example/{i}.jpg",
                    filename=f"{slug or 'image'}-{i}.jpg",
                    alt=f"{alt_keyword or query} 관련 이미지 {i}",
                    source="stub",
                ) for i in range(1, count + 1)
            ]

    tmp = tempfile.mkdtemp(prefix="osmu_gen_")
    storage = LocalCsvStorage(data_dir=tmp)
    gen = Generator(
        storage=storage,
        crawler=StubCrawler(),
        writer=StubWriter(),
        images=StubImages(),
        config=GeneratorConfig(n_sources=3, n_images=3),
    )
    result = gen.generate("직장인 다이어트 식단")
    assert result.status == "generated"
    assert "<h1>" in result.refined_post
    assert result.refined_post.count("<img") >= 2
    assert len(result.original_source) == 3
    assert len(result.image_urls) == 3
    # 파일명 규칙 — slug-N.jpg
    for i, im in enumerate(result.image_urls, 1):
        assert im.filename == f"office-diet-meal-{i}.jpg"

    # content_db 의 image_urls 가 JSON 으로 직렬화됐는지
    saved = next(r for r in storage.list_content() if r.id == result.record_id)
    assert saved.status == "generated"
    parsed = _json.loads(saved.image_urls)
    assert isinstance(parsed, list) and len(parsed) == 3
    assert parsed[0]["filename"] == "office-diet-meal-1.jpg"
    assert parsed[0]["url"].startswith("https://img.example/")


def test_generator_firecrawl_fallback_to_template():
    """Crawler 가 빈 결과만 줄 때 → 폴백 텍스트로 진행."""
    from osmu_kr.content_generator import Generator
    from osmu_kr.content_generator.generator import GeneratorConfig
    from osmu_kr.content_generator.interfaces import (
        BaseCrawler, BaseWriter, BaseImageProvider, CrawledPage,
    )
    from osmu_kr.storage.csv_local import LocalCsvStorage

    class EmptyCrawler(BaseCrawler):
        name = "empty"
        def search(self, query, *, limit=5):
            return []
        def scrape(self, url):
            return CrawledPage(url=url, error="not_called")

    class StubWriter(BaseWriter):
        name = "stub_writer"
        def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적"):
            assert "외부 검색이 일시적으로" in raw_content, "폴백 텍스트가 전달돼야 함"
            tags = "".join(
                f'<img src="{im.url}" alt="{im.alt}" data-filename="{im.filename}"/>'
                for im in (images or [])
            )
            return f"<h1>{keyword}</h1><h2>요약</h2><p>{raw_content}</p>{tags}"

    class StubImages(BaseImageProvider):
        name = "stub_images"
        def search(self, query, *, count=3, slug="", alt_keyword=""):
            from osmu_kr.content_generator.interfaces import ImageItem
            return [ImageItem(url=f"https://i.example/{i}", filename=f"{slug}-{i}.jpg",
                                alt=f"{alt_keyword} {i}")
                    for i in range(1, count + 1)]

    tmp = tempfile.mkdtemp(prefix="osmu_fallback_")
    storage = LocalCsvStorage(data_dir=tmp)
    gen = Generator(
        storage=storage, crawler=EmptyCrawler(),
        writer=StubWriter(), images=StubImages(),
        config=GeneratorConfig(n_sources=3, n_images=2),
    )
    result = gen.generate("재테크")
    assert result.status == "generated"
    assert "외부 검색이 일시적으로" in result.error_log or result.error_log


def test_generator_writer_failure_fallbacks_to_heuristic():
    """LLM 실패 시 1회 retry 후 fallback_to_heuristic=True 면 휴리스틱으로 살아남음."""
    from osmu_kr.content_generator import Generator
    from osmu_kr.content_generator.generator import GeneratorConfig
    from osmu_kr.content_generator.interfaces import (
        BaseCrawler, BaseWriter, BaseImageProvider, CrawledPage,
    )
    from osmu_kr.storage.csv_local import LocalCsvStorage

    class StubCrawler(BaseCrawler):
        name = "s"
        def search(self, query, *, limit=5):
            return [f"https://x/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(
                url=url,
                content=("이 글은 키워드 관련 본문입니다. "
                         "추천 정보를 충분히 담고 있습니다. "
                         "독자에게 도움이 되는 내용입니다."),
            )

    class FailingWriter(BaseWriter):
        name = "fail"
        def write(self, *a, **kw):
            raise RuntimeError("intentional")

    class StubImages(BaseImageProvider):
        name = "i"
        def search(self, query, *, count=3, slug="", alt_keyword=""):
            from osmu_kr.content_generator.interfaces import ImageItem
            return [ImageItem(url=f"https://i/{i}", filename=f"{slug}-{i}.jpg",
                                alt=f"{alt_keyword} {i}")
                    for i in range(1, count + 1)]

    tmp = tempfile.mkdtemp(prefix="osmu_writerfail_")
    gen = Generator(
        storage=LocalCsvStorage(data_dir=tmp),
        crawler=StubCrawler(), writer=FailingWriter(), images=StubImages(),
        config=GeneratorConfig(fallback_to_heuristic=True),
    )
    result = gen.generate("AI ETF")
    assert "<h1>" in result.refined_post.lower()
    assert "fallback" in result.error_log.lower()


def test_update_content_in_place():
    """update_content — id 와 created_at 은 보존, 나머지 필드만 갱신."""
    from osmu_kr.storage.csv_local import LocalCsvStorage
    from osmu_kr.models import ContentRecord
    tmp = tempfile.mkdtemp(prefix="osmu_upd_")
    s = LocalCsvStorage(data_dir=tmp)
    s.append_content(ContentRecord(id="001", keyword="A",
                                    refined_post="old", status="대기중",
                                    created_at="2026-01-01T00:00:00+0000"))

    ok = s.update_content("001", refined_post="new HTML", status="generated",
                          # 보호 필드는 무시돼야 함
                          id="999", created_at="2030-12-31T00:00:00+0000")
    assert ok is True
    rows = s.list_content()
    assert len(rows) == 1
    r = rows[0]
    assert r.id == "001", "id 가 변경되면 안 됨"
    assert r.created_at == "2026-01-01T00:00:00+0000", "created_at 도 보호"
    assert r.refined_post == "new HTML"
    assert r.status == "generated"

    # 없는 id
    assert s.update_content("999", refined_post="x") is False


def test_generator_retry_record_in_place():
    """retry_record — 같은 id 에 결과 in-place 갱신, status='generated'."""
    from osmu_kr.content_generator import Generator
    from osmu_kr.content_generator.generator import GeneratorConfig
    from osmu_kr.content_generator.interfaces import (
        BaseCrawler, BaseWriter, BaseImageProvider, CrawledPage, ImageItem,
    )
    from osmu_kr.storage.csv_local import LocalCsvStorage
    from osmu_kr.models import ContentRecord

    class StubCrawler(BaseCrawler):
        name = "s"
        def search(self, q, *, limit=5):
            return [f"https://x/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(url=url,
                                content="이 글은 키워드 본문입니다. 추천 정보를 담고 있습니다. "
                                        "독자에게 유용한 내용입니다.")
    class StubWriter(BaseWriter):
        name = "w"
        def write(self, kw, raw, *, sources=None, images=None, tone="전문적"):
            return f"<h1>{kw} retry</h1><h2>본문</h2><p>{raw[:80]}</p>"
    class StubImages(BaseImageProvider):
        name = "i"
        def search(self, q, *, count=3, slug="", alt_keyword=""):
            return [ImageItem(url=f"https://i/{i}", filename=f"{slug or 'x'}-{i}.jpg",
                                alt=f"{alt_keyword} {i}", role="concept")
                    for i in range(1, count + 1)]

    tmp = tempfile.mkdtemp(prefix="osmu_retry_")
    storage = LocalCsvStorage(data_dir=tmp)
    # 작성 대기 record 1개 — refined_post 비어 있음
    storage.append_content(ContentRecord(
        id="001", keyword="다이어트 추천", status="대기중",
        refined_post="", created_at="2026-01-01T00:00:00+0000",
    ))

    gen = Generator(
        storage=storage, crawler=StubCrawler(), writer=StubWriter(), images=StubImages(),
        config=GeneratorConfig(n_sources=3, n_images=3),
    )
    result = gen.retry_record("001")
    assert result.record_id == "001"
    assert "<h1>" in result.refined_post

    # storage 에서 같은 id 로 읽으면 새 결과
    refreshed = next(r for r in storage.list_content() if r.id == "001")
    assert refreshed.id == "001"
    assert refreshed.created_at == "2026-01-01T00:00:00+0000"  # 보존
    assert refreshed.status == "generated"
    assert "<h1>" in refreshed.refined_post
    assert "retried" in refreshed.note


def test_generator_retry_record_missing_id_raises():
    from osmu_kr.content_generator import Generator
    from osmu_kr.storage.csv_local import LocalCsvStorage
    import tempfile as _tf
    storage = LocalCsvStorage(data_dir=_tf.mkdtemp(prefix="osmu_retry_missing_"))
    gen = Generator(storage=storage,
                     crawler=type("C", (), {
                         "name": "n", "search": lambda *a, **k: [],
                         "scrape": lambda *a, **k: None,
                         "search_and_scrape": lambda *a, **k: [],
                     })(),
                     writer=type("W", (), {
                         "name": "w",
                         "write": lambda self, *a, **k: "<h1>x</h1>",
                     })(),
                     images=type("I", (), {
                         "name": "i", "search": lambda *a, **k: [],
                     })())
    try:
        gen.retry_record("nonexistent")
    except KeyError:
        return
    raise AssertionError("KeyError 가 발생해야 합니다")


def test_delete_content_csv_round_trip():
    """LocalCsvStorage 의 delete_content — append 후 삭제하면 사라짐."""
    from osmu_kr.storage.csv_local import LocalCsvStorage
    from osmu_kr.models import ContentRecord
    tmp = tempfile.mkdtemp(prefix="osmu_del_csv_")
    s = LocalCsvStorage(data_dir=tmp)
    s.append_content(ContentRecord(id="001", keyword="A"))
    s.append_content(ContentRecord(id="002", keyword="B"))
    s.append_content(ContentRecord(id="003", keyword="C"))
    assert {r.id for r in s.list_content()} == {"001", "002", "003"}

    assert s.delete_content("002") is True
    ids = {r.id for r in s.list_content()}
    assert ids == {"001", "003"}, f"002 가 안 지워짐: {ids}"

    # 없는 id 는 False
    assert s.delete_content("999") is False
    # 빈 id 는 False
    assert s.delete_content("") is False


def test_delete_content_xlsx_round_trip():
    """LocalXlsxStorage 의 delete_content — 엑셀 시트에서도 동일하게 동작."""
    from osmu_kr.storage.xlsx_local import LocalXlsxStorage
    from osmu_kr.models import ContentRecord
    tmp = tempfile.mkdtemp(prefix="osmu_del_xlsx_")
    s = LocalXlsxStorage(data_dir=tmp)
    s.append_content(ContentRecord(id="001", keyword="A"))
    s.append_content(ContentRecord(id="002", keyword="B"))
    assert s.delete_content("001") is True
    assert {r.id for r in s.list_content()} == {"002"}


def test_delete_content_mirror_delegates_to_local():
    """Mirror 백엔드는 local 에 delete 적용 + sheets 호출 (없으면 보류큐)."""
    from osmu_kr.storage.csv_local import LocalCsvStorage
    from osmu_kr.storage.mirror import MirrorStorage
    from osmu_kr.models import ContentRecord
    tmp = tempfile.mkdtemp(prefix="osmu_del_mirror_")
    local = LocalCsvStorage(data_dir=tmp)
    local.append_content(ContentRecord(id="001", keyword="A"))
    local.append_content(ContentRecord(id="002", keyword="B"))

    def fail_factory():
        raise RuntimeError("no creds")

    mirror = MirrorStorage(local=local, sheets_factory=fail_factory)
    assert mirror.delete_content("001") is True
    assert {r.id for r in mirror.list_content()} == {"002"}


def test_keyword_classifier_game_domain():
    """게임 키워드는 GAME 도메인으로 분류."""
    from osmu_kr.content_generator.keyword_classifier import classify, profile_for, Domain
    assert classify("데드바이데이라이트") == Domain.GAME
    assert classify("리그오브레전드 추천 챔프") == Domain.GAME
    assert classify("Elden Ring 공략") == Domain.GAME

    profile = profile_for("데드바이데이라이트")
    assert profile.domain == Domain.GAME
    assert "게임" in profile.name_ko
    # 도메인 섹션이 게임 특화인지
    assert any("초보자" in t for t in profile.section_titles)
    assert any("플레이" in t or "전략" in t or "빌드" in t
               for t in profile.section_titles)


def test_keyword_classifier_other_domains():
    from osmu_kr.content_generator.keyword_classifier import classify, Domain
    assert classify("AI ETF 추천") == Domain.FINANCE
    assert classify("직장인 다이어트 식단") == Domain.DIET
    assert classify("맥북 프로 추천") == Domain.IT
    assert classify("선크림 추천") == Domain.BEAUTY
    assert classify("도쿄 여행 코스") == Domain.TRAVEL
    assert classify("파스타 레시피") == Domain.FOOD
    assert classify("랜덤 일반 키워드") == Domain.GENERAL


def test_heuristic_writer_game_keyword_no_seed_leak():
    """게임 키워드 + 폴백 시드 입력 → 본문에 ‘목차 안내문’ 노출 X + 게임 특화 섹션."""
    from osmu_kr.content_generator.writer import (
        HeuristicWriter, FALLBACK_SEED_MARKER, BANNED_PHRASES,
    )
    from osmu_kr.content_generator.interfaces import ImageItem

    w = HeuristicWriter()
    seed_input = (
        f"{FALLBACK_SEED_MARKER}\n"
        "이 텍스트는 절대 본문에 그대로 노출되면 안 됩니다. 시드 마커 포함."
    )
    images = [
        ImageItem(url="https://x/1", filename="dbd-1.jpg", alt="a", role="concept"),
        ImageItem(url="https://x/2", filename="dbd-2.jpg", alt="b", role="example"),
        ImageItem(url="https://x/3", filename="dbd-3.jpg", alt="c", role="comparison"),
    ]
    html = w.write("데드바이데이라이트", seed_input, images=images, sources=[])
    # 시드 텍스트가 본문에 그대로 노출되지 않아야 함
    assert FALLBACK_SEED_MARKER not in html
    assert "이 텍스트는 절대 본문에" not in html
    assert "절대 본문에 그대로 노출" not in html

    # 도메인 섹션이 적용됐는지 — 게임 키워드라면 ‘초보자’ 또는 ‘플레이’ 가 H2 에 등장
    h2_section = html.lower()
    assert "초보자" in html or "플레이" in html
    # 금지 표현 없음
    for phrase in BANNED_PHRASES:
        assert phrase not in html, f"금지 표현 발견: {phrase}"


def test_heuristic_writer_finance_keyword_uses_finance_sections():
    from osmu_kr.content_generator.writer import HeuristicWriter
    w = HeuristicWriter()
    html = w.write("AI ETF 추천", "", images=[], sources=[])
    # 금융 섹션 어휘
    assert ("수익률" in html or "리스크" in html
            or "투자" in html or "포트폴리오" in html)


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


def test_keyword_context_game_keyword_carries_topic():
    """1단계 핵심 — '데드바이데이라이트' → inferred_topic='게임' 가 살아있어야 한다."""
    from osmu_kr.content_generator.keyword_context import KeywordContext

    ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
    assert ctx.keyword == "데드바이데이라이트 공략"
    assert ctx.inferred_topic == "게임"
    assert ctx.domain == "game"
    assert ctx.intent_hint == "공략"
    # 로그 한 줄 — '게임 관련' 힌트가 사람이 읽기 좋게 들어가는지 확인
    assert "게임" in ctx.short()
    assert "공략" in ctx.short()


def test_keyword_context_intent_inference():
    """간단한 룰 기반 intent 추론 — 추천/비교/리뷰 모두 잡혀야 한다."""
    from osmu_kr.content_generator.keyword_context import (
        KeywordContext, infer_intent,
    )

    assert infer_intent("AI ETF 추천 2025") == "추천"
    assert infer_intent("아이폰 vs 갤럭시 비교") == "비교"
    assert infer_intent("로봇청소기 리뷰") == "리뷰"
    assert infer_intent("재테크 방법") == "방법"
    assert infer_intent("그냥 키워드") == "정보"        # 기본값
    # 도메인 분류가 빈 문자열에 대해서도 안전하게 동작해야 함
    empty = KeywordContext.from_keyword("")
    assert empty.keyword == ""
    assert empty.inferred_topic == "일반"
    assert empty.intent_hint == "정보"


def test_keyword_context_coerce_accepts_str_and_passthrough():
    """coerce: str / KeywordContext / None 모두 KeywordContext 를 돌려준다."""
    from osmu_kr.content_generator.keyword_context import KeywordContext

    a = KeywordContext.coerce("리그오브레전드 추천")
    assert a.inferred_topic == "게임"
    assert a.intent_hint == "추천"

    b = KeywordContext.coerce(a)               # passthrough
    assert b is a

    c = KeywordContext.coerce(None)            # None → 빈 컨텍스트
    assert c.keyword == ""
    assert c.inferred_topic == "일반"


def test_collector_logs_topic_hint_for_game_keyword(caplog=None):
    """완료 기준 그 자체 — collector 입력 로그에 '게임 관련 키워드' 힌트가 포함된다."""
    import io, logging
    from osmu_kr.content_generator.collector import Collector
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, query, *, limit=5):
            return [f"https://x.com/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(
                url=url, title="제목",
                content=("데드바이데이라이트는 4vs1 비대칭 호러 게임입니다. "
                          "생존자는 발전기 5개를 수리해 탈출해야 합니다. "
                          "킬러는 후크에 매달아 제거합니다."),
            )

    # collector 로거에 stream 핸들러를 붙여서 메시지 캡처
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    target = logging.getLogger("osmu_kr.content_generator.collector")
    prev_level = target.level
    target.setLevel(logging.INFO)
    target.addHandler(handler)
    try:
        c = Collector(StubCrawler(), min_sources=3)
        raw = c.collect("데드바이데이라이트 공략", limit=3)
    finally:
        target.removeHandler(handler)
        target.setLevel(prev_level)

    log_text = buf.getvalue()
    assert "데드바이데이라이트" in log_text, log_text
    assert "게임 관련 키워드" in log_text, log_text
    assert "공략" in log_text, log_text
    # raw_content 에도 컨텍스트가 살아있어야 한다
    assert raw.context is not None
    assert raw.context.inferred_topic == "게임"
    assert raw.context.domain == "game"
    assert raw.context.intent_hint == "공략"


def test_collector_accepts_keyword_context_directly():
    """str 뿐 아니라 KeywordContext 도 입력으로 받을 수 있어야 한다(후방호환)."""
    from osmu_kr.content_generator.collector import Collector
    from osmu_kr.content_generator.keyword_context import KeywordContext
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, query, *, limit=5):
            return [f"https://x.com/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(url=url, title="제목",
                                content=("적금 추천 글입니다. "
                                          "금리 비교가 핵심입니다. "
                                          "우대조건도 살펴봐야 합니다."))

    ctx = KeywordContext.from_keyword("적금 추천")
    c = Collector(StubCrawler(), min_sources=3)
    raw = c.collect(ctx, limit=3)
    assert raw.context is ctx or raw.context.keyword == "적금 추천"
    assert raw.context.inferred_topic == "재테크/금융"
    assert raw.context.intent_hint == "추천"


# ────────────────────────────────────────────────────────
# [2단계] interpreter — 0단계 keyword 정규화 (룰 + LLM 보강)
# ────────────────────────────────────────────────────────

def test_keyword_context_topic_summary_filled_by_rule():
    """from_keyword 만으로도 topic_summary 가 채워져야 한다."""
    from osmu_kr.content_generator.keyword_context import KeywordContext

    a = KeywordContext.from_keyword("데드바이데이라이트 공략")
    assert a.topic_summary
    assert "게임" in a.topic_summary
    assert "공략" in a.topic_summary
    assert a.source == "rule"

    # 진짜 미등재 — 어떤 도메인 단어도 안 걸리는 키워드
    b = KeywordContext.from_keyword("오리지널 한정판 굿즈")
    assert b.inferred_topic == "일반", f"unexpected topic: {b.inferred_topic}"
    assert b.topic_summary
    assert "일반" in b.topic_summary
    # 도메인 미식별 신호가 한 줄 요약에 함께 있어야 한다 (LLM 보강 권장)
    assert "LLM" in b.topic_summary or "보강" in b.topic_summary


def test_interpreter_rule_only_when_use_llm_false():
    """use_llm=False 면 LLM 호출 없이 룰만 — source='rule'."""
    from osmu_kr.content_generator.interpreter import interpret

    ctx = interpret("데드바이데이라이트 공략", use_llm=False)
    assert ctx.domain == "game"
    assert ctx.intent_hint == "공략"
    assert ctx.source == "rule"


def test_interpreter_llm_fallback_when_no_api_key(monkeypatch=None):
    """use_llm=True 라도 키 없으면 룰 결과 + source='llm_fallback_rule'."""
    import os
    from osmu_kr.content_generator.interpreter import interpret

    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        ctx = interpret("적금 추천", use_llm=True)
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved

    assert ctx.domain == "finance"            # 룰이 잡아낸 결과
    assert ctx.intent_hint == "추천"
    assert ctx.source == "llm_fallback_rule"
    assert ctx.raw_signals.get("llm_skip") == "no_api_key"


def test_interpreter_llm_overrides_when_call_succeeds():
    """LLM 응답이 정상이면 domain/intent/topic_summary 가 덮어써져야 한다.

    실제 Anthropic 호출 대신 _post_anthropic 을 stub 으로 교체해 테스트한다.
    미등재 키워드(‘스텔라 블레이드 빌드’) → 룰은 general 이지만 LLM 이 game 으로 보정.
    """
    import os
    from osmu_kr.content_generator import interpreter as itp

    fake_response = (
        '{"domain": "game", "intent": "공략", '
        '"topic_summary": "스텔라 블레이드 캐릭터 빌드/스킬 셋업을 찾는 게임 키워드"}'
    )
    saved_post = itp._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test_key_anything"

    def stub_post(api_key, model, system, user, **_kw):
        assert "스텔라 블레이드" in user
        return fake_response

    itp._post_anthropic = stub_post
    try:
        ctx = itp.interpret("스텔라 블레이드 빌드", use_llm=True)
    finally:
        itp._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    assert ctx.domain == "game"
    assert ctx.inferred_topic == "게임"
    assert ctx.intent_hint == "공략"
    assert "스텔라 블레이드" in ctx.topic_summary
    assert ctx.source == "llm"
    assert ctx.raw_signals.get("llm_model")


def test_interpreter_llm_failure_falls_back_to_rule():
    """LLM 호출이 예외/잘못된 JSON 이면 룰 결과로 폴백해야 한다."""
    import os
    from osmu_kr.content_generator import interpreter as itp

    saved_post = itp._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test_key_bad"

    def stub_post(*_a, **_kw):
        raise RuntimeError("Anthropic HTTP 500: simulated")

    itp._post_anthropic = stub_post
    try:
        ctx = itp.interpret("아이폰 vs 갤럭시 비교", use_llm=True)
    finally:
        itp._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    assert ctx.domain == "it"             # 룰이 잡은 결과 보존
    assert ctx.intent_hint == "비교"
    assert ctx.source == "llm_fallback_rule"
    assert "call_failed" in ctx.raw_signals.get("llm_skip", "")


def test_interpreter_passthrough_when_already_context():
    """이미 KeywordContext 면 그대로 통과 (불필요 LLM 호출 없음)."""
    from osmu_kr.content_generator.keyword_context import KeywordContext
    from osmu_kr.content_generator.interpreter import interpret

    ctx_in = KeywordContext.from_keyword("다이어트 후기")
    ctx_out = interpret(ctx_in, use_llm=True)
    assert ctx_out is ctx_in


# ────────────────────────────────────────────────────────
# [3단계] collector Phase 1 — Blueprint + 검증 + summary_embedding
# ────────────────────────────────────────────────────────

def test_blueprint_rule_mode_basic_shape():
    """룰 모드만으로도 Phase 1 산출물 4종이 채워져야 한다."""
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.keyword_context import KeywordContext

    ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
    bp = generate_blueprint(ctx, use_llm=False)

    assert bp.title
    assert bp.target_reader.primary_intent == "공략"
    assert bp.target_reader.knowledge_level in {"초보", "중급", "전문가"}
    assert len(bp.paragraphs) >= 3
    # 첫·마지막 단락은 llm_generated 강제
    assert bp.paragraphs[0].paragraph_type == "llm_generated"
    assert bp.paragraphs[-1].paragraph_type == "llm_generated"
    # 가운데 단락은 fact_based 가 적어도 1개
    assert any(p.paragraph_type == "fact_based" for p in bp.paragraphs[1:-1])
    assert bp.intro and bp.short_conclusion
    assert bp.source == "rule"


def test_blueprint_llm_overrides_when_call_succeeds():
    """LLM 응답 정상이면 title/target_reader/paragraphs 가 전부 덮어써져야 한다."""
    import os
    from osmu_kr.content_generator import blueprint as bp_mod
    from osmu_kr.content_generator.keyword_context import KeywordContext

    fake = (
        '{"title": "데드바이데이라이트 초보 입문 — 첫 매칭 전 필수 팁",'
        ' "target_reader": {"persona": "처음 DBD를 시작하는 직장인 게이머",'
        '   "knowledge_level": "초보", "primary_intent": "공략"},'
        ' "paragraphs": ['
        '   {"section_index": 1, "title": "DBD 가 어떤 게임인지 빠르게 정리",'
        '    "paragraph_type": "llm_generated", "description": "장르·규칙·승리 조건"},'
        '   {"section_index": 2, "title": "생존자 캐릭터 추천 3종",'
        '    "paragraph_type": "fact_based", "description": "초보용 캐릭터 비교",'
        '    "facts_required": ["메그", "드와이트", "클로뎃"]},'
        '   {"section_index": 3, "title": "초보가 자주 죽는 4가지 실수",'
        '    "paragraph_type": "fact_based", "description": "회피법까지",'
        '    "facts_required": ["발전기 본딩", "후크 매달림"]},'
        '   {"section_index": 4, "title": "다음에 시도할 콘텐츠",'
        '    "paragraph_type": "llm_generated", "description": "다음 행동 가이드"}'
        ' ],'
        ' "intro": "DBD 첫 진입 직장인을 위한 필수 가이드.",'
        ' "short_conclusion": "초보 핵심 4가지를 정리한 입문 가이드입니다."}'
    )
    saved_post = bp_mod._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test"
    bp_mod._post_anthropic = lambda *a, **kw: fake
    try:
        ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
        bp = bp_mod.generate_blueprint(ctx, use_llm=True)
    finally:
        bp_mod._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    assert bp.source == "llm"
    assert "DBD" in bp.title or "데드바이데이라이트" in bp.title
    assert bp.target_reader.knowledge_level == "초보"
    assert len(bp.paragraphs) == 4
    fact_titles = [p.title for p in bp.paragraphs if p.paragraph_type == "fact_based"]
    assert len(fact_titles) == 2


def test_blueprint_llm_failure_falls_back_to_rule():
    """Anthropic 호출 예외 시 룰 결과로 폴백해야 한다."""
    import os
    from osmu_kr.content_generator import blueprint as bp_mod
    from osmu_kr.content_generator.keyword_context import KeywordContext

    saved_post = bp_mod._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test"
    def boom(*a, **kw):
        raise RuntimeError("Anthropic HTTP 500")
    bp_mod._post_anthropic = boom
    try:
        ctx = KeywordContext.from_keyword("적금 추천")
        bp = bp_mod.generate_blueprint(ctx, use_llm=True)
    finally:
        bp_mod._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    assert bp.source == "llm_fallback_rule"
    assert "call_failed" in bp.raw_signals.get("llm_skip", "")
    assert bp.target_reader.primary_intent == "추천"


def test_blueprint_validator_rejects_generic_template():
    """‘개념 → 활용 → 결론’ 류 일반 템플릿은 reject."""
    from osmu_kr.content_generator.blueprint import (
        BlueprintResult, ParagraphBlock, TargetReader,
    )
    from osmu_kr.content_generator.blueprint_validator import validate_blueprint
    from osmu_kr.content_generator.keyword_context import KeywordContext

    ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
    bad = BlueprintResult(
        keyword=ctx.keyword,
        title="데드바이데이라이트",
        target_reader=TargetReader(persona="x", knowledge_level="초보",
                                    primary_intent="공략"),
        paragraphs=[
            ParagraphBlock(1, "개념", "llm_generated", "x"),
            ParagraphBlock(2, "활용", "fact_based", "x"),
            ParagraphBlock(3, "결론", "llm_generated", "x"),
        ],
        intro="x",
        short_conclusion="x",
    )
    issues = validate_blueprint(bad, ctx)
    assert issues
    assert any("generic" in i for i in issues), issues


def test_blueprint_validator_accepts_concrete_template():
    """구체적 단락 제목 + fact_based 분포 + commercial 채워짐 → 통과."""
    from osmu_kr.content_generator.blueprint import (
        BlueprintResult, CommercialElements, ParagraphBlock, TargetReader,
    )
    from osmu_kr.content_generator.blueprint_validator import validate_blueprint
    from osmu_kr.content_generator.keyword_context import KeywordContext

    ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
    good = BlueprintResult(
        keyword=ctx.keyword,
        title="데드바이데이라이트 초보 공략",
        target_reader=TargetReader(persona="x", knowledge_level="초보",
                                    primary_intent="공략"),
        paragraphs=[
            ParagraphBlock(1, "DBD 가 어떤 게임인지 1분 요약",
                           "llm_generated", "장르·규칙"),
            ParagraphBlock(2, "초보 추천 생존자 3명 비교",
                           "fact_based", "메그/드와이트/클로뎃"),
            ParagraphBlock(3, "공략 핵심 — 발전기 본딩 회피법",
                           "fact_based", "발전기 동선"),
            ParagraphBlock(4, "다음 단계로 시도할 콘텐츠",
                           "llm_generated", "다음 행동"),
        ],
        intro="x", short_conclusion="x",
        commercial_elements=CommercialElements(
            recommendations=["메그 빌드", "드와이트 빌드"],
            comparison_points=["캐릭터별 강점"],
            cta_candidates=["빌드 더 보기", "초보 영상"],
        ),
    )
    assert validate_blueprint(good, ctx) == []


def test_stub_embedder_deterministic_and_dim():
    """StubEmbedder 는 같은 입력에 같은 벡터, 차원=768."""
    from osmu_kr.content_generator.embedder import StubEmbedder, cosine

    e = StubEmbedder()
    a = e.encode("데드바이데이라이트 초보 공략")
    b = e.encode("데드바이데이라이트 초보 공략")
    c = e.encode("적금 추천 비교")
    assert len(a) == 768 == e.dim
    assert a == b
    assert cosine(a, a) > 0.99
    # 다른 입력은 cosine < 1
    assert cosine(a, c) < 0.99


def test_zero_embedder_returns_none():
    from osmu_kr.content_generator.embedder import ZeroEmbedder

    z = ZeroEmbedder()
    assert z.encode("아무 텍스트") is None
    assert z.dim == 0


def test_build_embedder_respects_env(monkeypatch=None):
    """OSMU_EMBEDDER=stub → StubEmbedder, =disabled → ZeroEmbedder."""
    import os
    from osmu_kr.content_generator.embedder import (
        build_embedder, StubEmbedder, ZeroEmbedder,
    )
    saved = os.environ.get("OSMU_EMBEDDER")
    try:
        os.environ["OSMU_EMBEDDER"] = "stub"
        assert isinstance(build_embedder(), StubEmbedder)
        os.environ["OSMU_EMBEDDER"] = "disabled"
        assert isinstance(build_embedder(), ZeroEmbedder)
    finally:
        if saved is None:
            os.environ.pop("OSMU_EMBEDDER", None)
        else:
            os.environ["OSMU_EMBEDDER"] = saved


def test_collector_phase1_full_with_stub_embedder():
    """phase1: blueprint(룰) + summary_embedding(stub) 통합."""
    import os
    from osmu_kr.content_generator.collector import Collector
    from osmu_kr.content_generator.embedder import StubEmbedder
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage
    from osmu_kr.content_generator.keyword_context import KeywordContext

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, q, *, limit=5): return []
        def scrape(self, url): return CrawledPage(url=url, title="t", content="c")

    saved_use = os.environ.pop("OSMU_USE_LLM_BLUEPRINT", None)
    try:
        c = Collector(StubCrawler(), embedder=StubEmbedder())
        ctx = KeywordContext.from_keyword("적금 추천")
        bp = c.phase1(ctx)
    finally:
        if saved_use is not None:
            os.environ["OSMU_USE_LLM_BLUEPRINT"] = saved_use

    assert bp.keyword == "적금 추천"
    assert bp.title
    assert bp.target_reader.primary_intent == "추천"
    assert len(bp.paragraphs) >= 3
    assert isinstance(bp.summary_embedding, list)
    assert len(bp.summary_embedding) == 768
    assert bp.raw_signals.get("embedder") == "stub"


# ────────────────────────────────────────────────────────
# [4단계] Commercial Elements + Phase 2 fact 매핑
# ────────────────────────────────────────────────────────

def test_blueprint_rule_mode_includes_commercial_elements():
    """룰 모드에서도 commercial_elements 가 도메인별 폴백으로 채워져야 한다."""
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.keyword_context import KeywordContext

    bp = generate_blueprint(KeywordContext.from_keyword("적금 추천"), use_llm=False)
    ce = bp.commercial_elements
    assert len(ce.recommendations) >= 2, ce.recommendations
    assert len(ce.comparison_points) >= 2, ce.comparison_points
    assert len(ce.cta_candidates) >= 2, ce.cta_candidates
    # finance 도메인 폴백 — 비교 포인트에 ‘수익률’ 류 단어가 있어야 함
    assert any("수익률" in s or "수수료" in s for s in ce.comparison_points)


def test_blueprint_llm_response_with_commercial_elements():
    """LLM 응답에 commercial_elements 가 들어오면 그대로 반영."""
    import os
    from osmu_kr.content_generator import blueprint as bp_mod
    from osmu_kr.content_generator.keyword_context import KeywordContext

    fake = (
        '{"title": "DBD 초보 첫 매칭 가이드",'
        ' "target_reader": {"persona": "초보 게이머", "knowledge_level": "초보", "primary_intent": "공략"},'
        ' "paragraphs": ['
        '   {"section_index": 1, "title": "DBD 가 어떤 게임인지 정리", "paragraph_type": "llm_generated", "description": "장르"},'
        '   {"section_index": 2, "title": "초보 추천 캐릭터 비교", "paragraph_type": "fact_based", "description": "비교", "facts_required": ["메그", "드와이트"]},'
        '   {"section_index": 3, "title": "초보 자주 죽는 실수", "paragraph_type": "fact_based", "description": "실수"},'
        '   {"section_index": 4, "title": "다음 도전할 콘텐츠", "paragraph_type": "llm_generated", "description": "다음"}'
        ' ],'
        ' "intro": "DBD 첫 매칭 가이드.",'
        ' "short_conclusion": "초보용 핵심 4가지 정리.",'
        ' "commercial_elements": {'
        '   "recommendations": ["메그 토마스", "드와이트 페어필드", "초보 빌드 4종"],'
        '   "comparison_points": ["생존자 캐릭터 강점", "맵 별 동선"],'
        '   "cta_candidates": ["추천 빌드 더 보기", "초보 영상 가이드"]'
        ' }}'
    )
    saved_post = bp_mod._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test"
    bp_mod._post_anthropic = lambda *a, **kw: fake
    try:
        bp = bp_mod.generate_blueprint(
            KeywordContext.from_keyword("데드바이데이라이트 공략"), use_llm=True,
        )
    finally:
        bp_mod._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    ce = bp.commercial_elements
    assert "메그 토마스" in ce.recommendations
    assert any("강점" in s for s in ce.comparison_points)
    assert any("빌드" in s for s in ce.cta_candidates)


def test_phase1_autofills_commercial_when_llm_omits_them():
    """LLM 이 commercial 누락한 응답이어도 phase1 이 룰 폴백으로 보강."""
    import os
    from osmu_kr.content_generator import blueprint as bp_mod
    from osmu_kr.content_generator.collector import Collector
    from osmu_kr.content_generator.embedder import StubEmbedder
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage
    from osmu_kr.content_generator.keyword_context import KeywordContext

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, q, *, limit=5): return []
        def scrape(self, url): return CrawledPage(url=url, title="t", content="c")

    fake = (
        '{"title": "적금 추천 — 직장인용 비교",'
        ' "target_reader": {"persona": "직장인 초보", "knowledge_level": "초보", "primary_intent": "추천"},'
        ' "paragraphs": ['
        '   {"section_index": 1, "title": "적금 추천 글이 다룰 범위 정리", "paragraph_type": "llm_generated", "description": "범위"},'
        '   {"section_index": 2, "title": "수익률 상위 적금 3종 비교", "paragraph_type": "fact_based", "description": "수익률 비교"},'
        '   {"section_index": 3, "title": "수수료·우대조건 체크포인트", "paragraph_type": "fact_based", "description": "수수료"},'
        '   {"section_index": 4, "title": "지속할 만한 추천 전략", "paragraph_type": "llm_generated", "description": "전략"}'
        ' ],'
        ' "intro": "직장인 적금 비교 가이드.",'
        ' "short_conclusion": "추천 적금 3종 비교 정리."'
        '}'
    )
    saved_post = bp_mod._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    saved_use = os.environ.get("OSMU_USE_LLM_BLUEPRINT")
    os.environ["ANTHROPIC_API_KEY"] = "test"
    os.environ["OSMU_USE_LLM_BLUEPRINT"] = "1"
    bp_mod._post_anthropic = lambda *a, **kw: fake
    try:
        c = Collector(StubCrawler(), embedder=StubEmbedder())
        bp = c.phase1(KeywordContext.from_keyword("적금 추천"))
    finally:
        bp_mod._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key
        if saved_use is None:
            os.environ.pop("OSMU_USE_LLM_BLUEPRINT", None)
        else:
            os.environ["OSMU_USE_LLM_BLUEPRINT"] = saved_use

    # paragraphs 는 LLM 결과 유지 (구조 OK)
    assert len(bp.paragraphs) == 4
    assert "적금" in bp.title
    # commercial 은 자동 보강
    assert bp.commercial_elements.recommendations
    assert bp.commercial_elements.cta_candidates
    assert bp.raw_signals.get("commercial_autofix")


def test_phase2_fact_mapping_with_stub_crawler():
    """Phase2 — fact_based 단락별 facts 모음 + 도메인 관련성."""
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage
    from osmu_kr.content_generator.keyword_context import KeywordContext
    from osmu_kr.content_generator.phase2 import Phase2Collector, Phase2Config

    GAME_BODY = (
        "데드바이데이라이트는 4vs1 비대칭 호러 게임으로, 생존자가 발전기를 수리해 탈출합니다. "
        "메그 토마스는 빠른 달리기로 초보에게 추천되는 생존자 캐릭터입니다. "
        "킬러는 후크에 매달아 탈락시키며, 맵 별로 동선이 달라집니다. "
        "초보자는 발전기 본딩을 피하고 안전한 코너를 활용해야 살아남습니다."
    )

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, q, *, limit=5):
            return [f"https://wiki.example/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(url=url, title="DBD 가이드", content=GAME_BODY)

    ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
    bp = generate_blueprint(ctx, use_llm=False)
    ph2 = Phase2Collector(StubCrawler(),
                            config=Phase2Config(min_facts_per_section=1,
                                                  min_total_facts=1,
                                                  facts_per_query=2,
                                                  pages_per_query=1))
    res = ph2.run(bp, domain=ctx.domain)

    assert res.total_facts >= 1
    # fact_based 단락마다 sources 가 있어야 함
    fact_sections = [p for p in bp.paragraphs if p.paragraph_type == "fact_based"]
    assert len(res.sources_by_section) == len(fact_sections)
    # 도메인 관련성 — 게임 본문이라 mismatch 안 나야 함
    assert "domain_mismatch" not in " ".join(res.issues)
    # FactItem 형태 확인
    sample = next(iter(res.sources_by_section.values()))
    assert sample[0].fact_text and sample[0].source_url


def test_phase2_flags_domain_mismatch_on_off_topic_facts():
    """게임 키워드인데 facts 가 일반 비즈니스 본문이면 domain_mismatch."""
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage
    from osmu_kr.content_generator.keyword_context import KeywordContext
    from osmu_kr.content_generator.phase2 import Phase2Collector, Phase2Config

    OFF_TOPIC = (
        "디지털 전환을 진행하는 기업은 클라우드 인프라 구축으로 운영 효율을 높입니다. "
        "SaaS 도입은 초기 투자 비용을 줄이고 유지보수 부담을 낮추는 효과가 있습니다. "
        "고객 경험 관리는 마케팅 캠페인 측정 지표로 점점 더 중요해지고 있습니다. "
        "데이터 거버넌스 정책 수립은 보안 사고를 줄이고 규제 대응에 도움이 됩니다."
    )

    class OffTopicCrawler(BaseCrawler):
        name = "stub_off"
        def search(self, q, *, limit=5):
            return [f"https://biz.example/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(url=url, title="비즈니스 글", content=OFF_TOPIC)

    ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
    bp = generate_blueprint(ctx, use_llm=False)
    ph2 = Phase2Collector(OffTopicCrawler(),
                            config=Phase2Config(min_facts_per_section=1,
                                                  min_total_facts=1,
                                                  facts_per_query=2,
                                                  pages_per_query=1))
    res = ph2.run(bp, domain=ctx.domain)
    assert any("domain_mismatch" in i for i in res.issues), res.issues


def test_phase2_flags_insufficient_facts():
    """단락당 최소 facts 미달이면 issue."""
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage
    from osmu_kr.content_generator.keyword_context import KeywordContext
    from osmu_kr.content_generator.phase2 import Phase2Collector, Phase2Config

    class EmptyCrawler(BaseCrawler):
        name = "empty"
        def search(self, q, *, limit=5): return []
        def scrape(self, url):
            return CrawledPage(url=url, title="", content="")

    ctx = KeywordContext.from_keyword("적금 추천")
    bp = generate_blueprint(ctx, use_llm=False)
    ph2 = Phase2Collector(EmptyCrawler(),
                            config=Phase2Config(min_facts_per_section=3,
                                                  min_total_facts=6))
    res = ph2.run(bp, domain=ctx.domain)
    assert res.total_facts == 0
    assert any(i.startswith("insufficient_facts") for i in res.issues)
    assert any(i.startswith("total_facts_too_low") for i in res.issues)


# ────────────────────────────────────────────────────────
# [5단계] SQLite 영속화 — v9 spec 5개 테이블
# ────────────────────────────────────────────────────────

def test_sqlite_storage_pool_round_trip():
    """SqliteStorage — KeywordPoolItem upsert / list / delete 라운드트립."""
    import os, tempfile
    from osmu_kr.models import KeywordPoolItem
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db_path = os.path.join(tempfile.mkdtemp(prefix="osmu_sqlite_"), "test.db")
    s = SqliteStorage(db_path=db_path)

    item = KeywordPoolItem(
        keyword_id="0001", seed_keyword="다이어트", keyword="직장인 다이어트 식단",
        score=82.5, status="golden", grade="황금", profile="롱테일",
        weak_points="", is_alchemy="Y", original_keyword="다이어트", revival_count=1,
    )
    s.upsert_pool(item)
    pool = s.list_pool()
    assert len(pool) == 1
    assert pool[0].keyword == "직장인 다이어트 식단"
    assert pool[0].grade == "황금"
    assert pool[0].is_alchemy == "Y"
    assert pool[0].revival_count == 1

    # upsert 갱신
    item.score = 90.0
    s.upsert_pool(item)
    assert s.list_pool()[0].score == 90.0

    # delete
    assert s.delete_pool("0001") is True
    assert s.list_pool() == []
    s.close()


def test_sqlite_storage_content_with_v9_fields_round_trip():
    """SqliteStorage — ContentRecord 의 v9 풍부 필드(JSON 컬럼)도 보존."""
    import json, os, tempfile
    from osmu_kr.models import ContentRecord
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db_path = os.path.join(tempfile.mkdtemp(prefix="osmu_sqlite_"), "test.db")
    s = SqliteStorage(db_path=db_path)

    target_reader = {"persona": "DBD 초보", "knowledge_level": "초보",
                      "primary_intent": "공략"}
    paragraphs = [
        {"section_index": 1, "title": "DBD 가 어떤 게임인지",
         "paragraph_type": "llm_generated", "description": "장르"},
        {"section_index": 2, "title": "초보 추천 캐릭터",
         "paragraph_type": "fact_based", "description": "비교",
         "facts_required": ["메그", "드와이트"]},
    ]
    facts = {"2": [{"fact_text": "메그는 빠른 달리기로 초보에 추천", "source_url": "https://x"}]}
    embedding = [0.01] * 768
    commercial = {"recommendations": ["메그", "드와이트"],
                   "comparison_points": ["속도 비교"],
                   "cta_candidates": ["빌드 더 보기"]}

    rec = ContentRecord(
        id="042",
        keyword="데드바이데이라이트 공략",
        title="DBD 초보 공략",
        status="generated",
        refined_post="<h1>DBD</h1>",
        target_reader_json=json.dumps(target_reader, ensure_ascii=False),
        paragraph_blueprint_json=json.dumps(paragraphs, ensure_ascii=False),
        normalized_sources_json=json.dumps(facts, ensure_ascii=False),
        summary_embedding_json=json.dumps(embedding, ensure_ascii=False),
        commercial_elements_json=json.dumps(commercial, ensure_ascii=False),
        publish_attempt_count=2,
    )
    s.append_content(rec)

    loaded = s.list_content()
    assert len(loaded) == 1
    r = loaded[0]
    assert r.id == "042"
    assert r.title == "DBD 초보 공략"
    assert r.publish_attempt_count == 2
    # JSON 컬럼 라운드트립
    assert json.loads(r.target_reader_json)["primary_intent"] == "공략"
    assert json.loads(r.paragraph_blueprint_json)[1]["paragraph_type"] == "fact_based"
    assert json.loads(r.normalized_sources_json)["2"][0]["source_url"] == "https://x"
    assert len(json.loads(r.summary_embedding_json)) == 768
    assert "메그" in json.loads(r.commercial_elements_json)["recommendations"]

    # update_content in-place
    assert s.update_content("042", status="발행완료", publish_attempt_count=3) is True
    updated = s.list_content()[0]
    assert updated.status == "발행완료"
    assert updated.publish_attempt_count == 3
    assert updated.id == "042"           # 보호 필드 유지

    # delete
    assert s.delete_content("042") is True
    assert s.list_content() == []
    s.close()


def test_sqlite_storage_history_round_trip():
    import os, tempfile
    from osmu_kr.models import ResearchHistoryRecord
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db_path = os.path.join(tempfile.mkdtemp(prefix="osmu_sqlite_"), "test.db")
    s = SqliteStorage(db_path=db_path)

    s.append_history(ResearchHistoryRecord(
        keyword="적금 추천", grade="황금", total_score=88.0, profile="일반",
        evaluator="naver_golden",
    ))
    rows = s.list_history()
    assert len(rows) == 1
    assert rows[0].keyword == "적금 추천"
    assert rows[0].grade == "황금"
    s.close()


def test_factory_builds_sqlite_when_backend_is_sqlite():
    import os, tempfile
    from osmu_kr import Config
    from osmu_kr.storage.factory import build_storage
    from osmu_kr.storage.sqlite_local import SqliteStorage

    tmp = tempfile.mkdtemp(prefix="osmu_factory_sqlite_")
    saved_be = os.environ.get("OSMU_STORAGE_BACKEND")
    saved_db = os.environ.get("OSMU_SQLITE_PATH")
    os.environ["OSMU_STORAGE_BACKEND"] = "sqlite"
    os.environ["OSMU_SQLITE_PATH"] = os.path.join(tmp, "fac.db")
    try:
        s = build_storage(Config())
    finally:
        if saved_be is None:
            os.environ.pop("OSMU_STORAGE_BACKEND", None)
        else:
            os.environ["OSMU_STORAGE_BACKEND"] = saved_be
        if saved_db is None:
            os.environ.pop("OSMU_SQLITE_PATH", None)
        else:
            os.environ["OSMU_SQLITE_PATH"] = saved_db

    assert isinstance(s, SqliteStorage)
    # 라이브 DB 파일 만들어졌는지
    assert os.path.isfile(os.path.join(tmp, "fac.db"))


def test_generator_persists_phase1_phase2_payload_to_sqlite():
    """Generator → SQLite 저장 시 phase1/phase2 산출물이 JSON 컬럼에 들어가는지."""
    import json, os, tempfile
    from osmu_kr.content_generator import Generator
    from osmu_kr.content_generator.generator import GeneratorConfig
    from osmu_kr.content_generator.interfaces import (
        BaseCrawler, BaseWriter, BaseImageProvider, CrawledPage, ImageItem,
    )
    from osmu_kr.storage.sqlite_local import SqliteStorage

    GAME = (
        "데드바이데이라이트는 4vs1 비대칭 호러 게임으로 생존자가 발전기를 수리해 탈출합니다. "
        "메그 토마스는 빠른 달리기로 초보자에 추천되는 생존자 캐릭터입니다. "
        "킬러는 후크에 매달아 탈락시키며 맵별로 동선이 다릅니다. "
        "발전기 본딩은 초보가 자주 죽는 패턴이라 분산 수리가 안전합니다."
    )

    class C(BaseCrawler):
        name="c"
        def search(self, q, *, limit=5): return [f"https://w/{i}" for i in range(limit)]
        def scrape(self, url): return CrawledPage(url=url, title="t", content=GAME)
    class W(BaseWriter):
        name="w"
        def write(self, kw, raw, *, sources=None, images=None, tone="전문적"):
            return f"<h1>{kw}</h1><h2>본문</h2><p>{raw[:60]}</p><h2>마무리</h2><p>요약</p>"
    class I(BaseImageProvider):
        name="i"
        def search(self, q, *, count=3, slug="", alt_keyword=""):
            return [ImageItem(url=f"https://i/{i}.jpg", filename=f"x-{i}.jpg",
                                alt="a", source="s") for i in range(1, count+1)]

    tmp = tempfile.mkdtemp(prefix="osmu_gen_sqlite_")
    db_path = os.path.join(tmp, "g.db")
    storage = SqliteStorage(db_path=db_path)

    saved = os.environ.get("OSMU_EMBEDDER")
    os.environ["OSMU_EMBEDDER"] = "stub"
    try:
        g = Generator(crawler=C(), writer=W(), images=I(), storage=storage,
                      config=GeneratorConfig(min_h2_sections=1, min_paragraphs=1,
                                              min_images=0))
        result = g.generate("데드바이데이라이트 공략", save=True)
    finally:
        if saved is None:
            os.environ.pop("OSMU_EMBEDDER", None)
        else:
            os.environ["OSMU_EMBEDDER"] = saved

    assert result.record_id
    rec = storage.list_content()[0]
    # v9 필드 모두 채워졌는지 확인
    assert rec.title  # blueprint.title
    assert rec.target_reader_json
    assert rec.paragraph_blueprint_json
    assert rec.commercial_elements_json
    assert rec.summary_embedding_json
    assert rec.normalized_sources_json
    # JSON 파싱 가능
    tr = json.loads(rec.target_reader_json)
    assert tr.get("primary_intent")
    pb = json.loads(rec.paragraph_blueprint_json)
    assert isinstance(pb, list) and len(pb) >= 3
    emb = json.loads(rec.summary_embedding_json)
    assert len(emb) == 768
    storage.close()


# ────────────────────────────────────────────────────────
# [6단계] PostgreSQL + pgvector — Neon 등 (실DB 없으면 자동 skip)
# ────────────────────────────────────────────────────────
class _SkipNoDB(Exception):
    """psycopg/pgvector 미설치 또는 OSMU_DATABASE_URL 미설정 시 자동 skip."""
    pass


def _need_postgres():
    import os, importlib
    url = os.environ.get("OSMU_DATABASE_URL", "")
    if not url:
        raise _SkipNoDB("OSMU_DATABASE_URL not set")
    try:
        importlib.import_module("psycopg")
    except ImportError:
        raise _SkipNoDB("psycopg not installed")
    return url


def test_postgres_storage_imports_without_db_url():
    """모듈 import 자체는 DB 없이도 안전해야 한다 (lazy import)."""
    from osmu_kr.storage import postgres  # noqa: F401
    from osmu_kr.storage import postgres_schema  # noqa: F401
    assert hasattr(postgres, "PostgresStorage")


def test_postgres_factory_raises_without_url():
    """OSMU_STORAGE_BACKEND=postgres 인데 DATABASE_URL 비면 분명한 에러."""
    import os
    from osmu_kr import Config
    from osmu_kr.storage.factory import build_storage

    saved_be = os.environ.get("OSMU_STORAGE_BACKEND")
    saved_url = os.environ.get("OSMU_DATABASE_URL")
    os.environ["OSMU_STORAGE_BACKEND"] = "postgres"
    os.environ.pop("OSMU_DATABASE_URL", None)
    try:
        try:
            build_storage(Config())
            assert False, "RuntimeError 가 나야 함"
        except RuntimeError as e:
            assert "OSMU_DATABASE_URL" in str(e)
    finally:
        if saved_be is None:
            os.environ.pop("OSMU_STORAGE_BACKEND", None)
        else:
            os.environ["OSMU_STORAGE_BACKEND"] = saved_be
        if saved_url is not None:
            os.environ["OSMU_DATABASE_URL"] = saved_url


def test_postgres_storage_round_trip_when_db_available():
    """실 DB(Neon 등) 가 있을 때만 라운드트립 검증 — 아니면 skip."""
    try:
        url = _need_postgres()
    except _SkipNoDB as e:
        print(f"  SKIP postgres 테스트: {e}")
        return

    import json
    from osmu_kr.models import ContentRecord, KeywordPoolItem
    from osmu_kr.storage.postgres import PostgresStorage

    s = PostgresStorage(database_url=url)

    # 격리 — 테스트 시작 시 정리
    s.conn.cursor().execute("DELETE FROM contents")
    s.conn.cursor().execute("DELETE FROM keywords")
    s.conn.commit()

    # pool
    item = KeywordPoolItem(
        keyword_id="pg-0001", seed_keyword="다이어트", keyword="직장인 다이어트 식단",
        score=82.5, status="golden", grade="황금", profile="롱테일",
    )
    s.upsert_pool(item)
    pool = s.list_pool()
    assert any(p.keyword_id == "pg-0001" for p in pool)

    # content + 임베딩
    embedding = [0.01] * 768
    rec = ContentRecord(
        id="pg-c-001",
        keyword="데드바이데이라이트 공략",
        title="DBD 초보 공략",
        status="generated",
        refined_post="<h1>DBD</h1>",
        target_reader_json='{"primary_intent": "공략"}',
        paragraph_blueprint_json='[{"section_index":1,"title":"x","paragraph_type":"llm_generated","description":"x"}]',
        normalized_sources_json='{"2": []}',
        summary_embedding_json=json.dumps(embedding),
        commercial_elements_json='{"recommendations":["메그"]}',
    )
    s.append_content(rec)
    loaded = [r for r in s.list_content() if r.id == "pg-c-001"]
    assert loaded
    r = loaded[0]
    if s.use_vector:
        assert len(json.loads(r.summary_embedding_json)) == 768

    # update
    assert s.update_content("pg-c-001", status="발행완료") is True
    assert any(rr.status == "발행완료" for rr in s.list_content() if rr.id == "pg-c-001")

    # 자기잠식 ANN — pgvector 가용한 경우만
    if s.use_vector:
        sims = s.find_similar_contents(embedding, top_k=3)
        assert sims
        first_rec, score = sims[0]
        assert first_rec.id == "pg-c-001"
        assert 0.0 <= score <= 1.0

    # cleanup
    s.delete_content("pg-c-001")
    s.delete_pool("pg-0001")
    s.close()


# ────────────────────────────────────────────────────────
# [v13-A] keywords.status 단순화 + keyword_usages 활성화
# ────────────────────────────────────────────────────────

def test_normalize_status_v13_active_archived_only():
    """v13: keywords.status 는 active/archived 2단계."""
    from osmu_kr.models import (
        normalize_status, KSTATUS_ACTIVE, KSTATUS_ARCHIVED,
    )
    # legacy + 7-A 시기 모두 → active 또는 archived 로 매핑
    assert normalize_status("golden") == KSTATUS_ACTIVE
    assert normalize_status("medium") == KSTATUS_ACTIVE
    assert normalize_status("reviving") == KSTATUS_ACTIVE
    assert normalize_status("used") == KSTATUS_ACTIVE   # v13: 발행 사실은 keyword_usages 가
    assert normalize_status("candidate") == KSTATUS_ACTIVE
    assert normalize_status("inprogress") == KSTATUS_ACTIVE
    assert normalize_status("published") == KSTATUS_ACTIVE
    assert normalize_status("failed") == KSTATUS_ACTIVE
    # archive 계열
    assert normalize_status("rejected") == KSTATUS_ARCHIVED
    assert normalize_status("expired") == KSTATUS_ARCHIVED
    assert normalize_status("deprecated") == KSTATUS_ARCHIVED
    assert normalize_status("archived") == KSTATUS_ARCHIVED
    # 알 수 없는 값 → active
    assert normalize_status("zzz") == KSTATUS_ACTIVE


def test_safety_layer_v13_lock_lifecycle_happy_path():
    """v13: start_lock → mark_published 정상 흐름."""
    from osmu_kr.models import (
        KSTATUS_ACTIVE, KeywordPoolItem,
        USAGE_IN_PROGRESS, USAGE_PUBLISHED,
    )
    from osmu_kr.researcher.safety import SafetyLayer

    rs = fresh_researcher()
    rs.storage.upsert_pool(KeywordPoolItem(
        keyword_id="0001", seed_keyword="x", keyword="테스트 키워드",
        status=KSTATUS_ACTIVE,
    ))
    safety = SafetyLayer(rs.storage)
    usage = safety.start_lock("0001", account_id="a", blog_id="b",
                                contents_id="c-001", note="test")
    assert usage.status == USAGE_IN_PROGRESS
    assert usage.started_at
    assert safety.is_locked("0001")

    pub = safety.mark_published(usage.id, contents_id="c-001", note="ok")
    assert pub.status == USAGE_PUBLISHED
    assert pub.published_at
    # lock 해제
    assert not safety.is_locked("0001")


def test_safety_layer_v13_rejects_double_lock():
    """v13: 같은 keyword 에 in_progress 가 이미 있으면 LockBusy."""
    from osmu_kr.models import KSTATUS_ACTIVE, KeywordPoolItem
    from osmu_kr.researcher.safety import LockBusy, SafetyLayer

    rs = fresh_researcher()
    rs.storage.upsert_pool(KeywordPoolItem(
        keyword_id="0002", seed_keyword="x", keyword="이중잠금",
        status=KSTATUS_ACTIVE,
    ))
    safety = SafetyLayer(rs.storage)
    safety.start_lock("0002")
    try:
        safety.start_lock("0002")
        assert False, "LockBusy 가 나야 함"
    except LockBusy:
        pass


def test_safety_layer_v13_failed_unlocks_and_keeps_published_at_empty():
    """v13: mark_failed 시 lock 즉시 해제. published_at 은 빈 채로 (180일 카운트 미적용)."""
    from osmu_kr.models import KSTATUS_ACTIVE, KeywordPoolItem, USAGE_FAILED
    from osmu_kr.researcher.safety import SafetyLayer

    rs = fresh_researcher()
    rs.storage.upsert_pool(KeywordPoolItem(
        keyword_id="0003", seed_keyword="x", keyword="실패",
        status=KSTATUS_ACTIVE,
    ))
    safety = SafetyLayer(rs.storage)
    usage = safety.start_lock("0003")
    failed = safety.mark_failed(usage.id, note="user_canceled")
    assert failed.status == USAGE_FAILED
    assert failed.failed_at
    assert failed.published_at == ""    # 180일 카운트 미적용
    assert not safety.is_locked("0003")  # lock 해제


def test_safety_layer_v13_archive_cancels_active_lock():
    """v13: archive_keyword 시 활성 lock 자동 failed."""
    from osmu_kr.models import (
        KSTATUS_ACTIVE, KSTATUS_ARCHIVED, KeywordPoolItem,
        USAGE_FAILED, normalize_status,
    )
    from osmu_kr.researcher.safety import SafetyLayer

    rs = fresh_researcher()
    rs.storage.upsert_pool(KeywordPoolItem(
        keyword_id="0004", seed_keyword="x", keyword="아카이브",
        status=KSTATUS_ACTIVE,
    ))
    safety = SafetyLayer(rs.storage)
    usage = safety.start_lock("0004")
    safety.archive_keyword("0004", reason="quality_low")

    item = rs.storage.get_pool("0004")
    assert normalize_status(item.status) == KSTATUS_ARCHIVED
    # 활성 lock 자동 failed 처리됐는지
    refreshed = next((u for u in rs.storage.list_usages() if u.id == usage.id), None)
    assert refreshed is not None
    assert refreshed.status == USAGE_FAILED


def test_recommend_v13_excludes_active_lock_and_published_in_blog():
    """v13 recommend: in_progress lock 또는 같은 blog 에서 published 된 키워드 제외."""
    from osmu_kr.researcher.safety import SafetyLayer

    rs = fresh_researcher()
    rs.run_seed("AI ETF")
    pool = rs.storage.list_pool()
    assert len(pool) >= 3

    safety = SafetyLayer(rs.storage)
    # pool[0] 을 in_progress 로 lock
    safety.start_lock(pool[0].keyword_id, blog_id="myblog")
    # pool[1] 을 my_blog 에 published 로 마킹
    u2 = safety.start_lock(pool[1].keyword_id, blog_id="myblog")
    safety.mark_published(u2.id)

    # blog_id="myblog" 에 대해 recommend 하면 둘 다 제외돼야 함
    recs = rs.recommend(top_n=10)
    # recommender 에 blog_id 안 줬으니 published 는 제외 안 됨 — lock 만 빠짐
    rec_ids = {r.keyword_id for r in recs}
    assert pool[0].keyword_id not in rec_ids   # in_progress lock 제외

    # blog_id 명시했을 때
    from osmu_kr.researcher import recommender as rec_mod
    recs_with_blog = rec_mod.recommend(rs.storage, rs.cfg, top_n=10, blog_id="myblog")
    rec_ids2 = {r.keyword_id for r in recs_with_blog}
    assert pool[0].keyword_id not in rec_ids2  # lock
    assert pool[1].keyword_id not in rec_ids2  # published_in_blog


# ────────────────────────────────────────────────────────
# [v13-B] keyword embedding + 씨드 중복 + 어뷰징 쿨다운
# ────────────────────────────────────────────────────────

def test_keyword_embedding_round_trip_in_sqlite():
    """v13-B: KeywordPoolItem.embedding_json SQLite 라운드트립."""
    import json, os, tempfile
    from osmu_kr.models import KSTATUS_ACTIVE, KeywordPoolItem
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db = os.path.join(tempfile.mkdtemp(prefix="osmu_kw_emb_"), "x.db")
    s = SqliteStorage(db_path=db)

    emb = [0.01] * 768
    s.upsert_pool(KeywordPoolItem(
        keyword_id="0001", seed_keyword="x", keyword="다이어트",
        status=KSTATUS_ACTIVE,
        embedding_json=json.dumps(emb),
        last_evaluated_at="2026-05-06T10:00:00+0000",
    ))
    loaded = s.get_pool("0001")
    assert loaded is not None
    assert json.loads(loaded.embedding_json) == emb
    assert loaded.last_evaluated_at == "2026-05-06T10:00:00+0000"
    s.close()


def test_keyword_safety_seed_duplicate_finds_similar():
    """씨드 입력 시 유사도 ≥ 0.93 인 기존 키워드 탐지."""
    import json
    from osmu_kr.models import KSTATUS_ACTIVE, KeywordPoolItem
    from osmu_kr.researcher.keyword_safety import KeywordSafety
    from osmu_kr.content_generator.embedder import StubEmbedder

    rs = fresh_researcher()
    embedder = StubEmbedder()
    ks = KeywordSafety(rs.storage, embedder=embedder)

    # 기존 키워드 등록 + embedding 생성
    existing = KeywordPoolItem(
        keyword_id="kw-001", seed_keyword="다이어트",
        keyword="다이어트 식단 추천", status=KSTATUS_ACTIVE,
    )
    ks.ensure_keyword_embedding(existing)
    rs.storage.upsert_pool(existing)

    # 거의 같은 씨드 입력 — 같은 텍스트라 stub embedding 도 동일 → cosine 1.0
    matches = ks.find_seed_duplicates("다이어트 식단 추천", threshold=0.9)
    assert len(matches) == 1
    assert matches[0].keyword_id == "kw-001"
    assert matches[0].similarity >= 0.99   # 같은 텍스트 → 거의 1.0


def test_keyword_safety_cooldown_blocks_recent_published_similar():
    """v13-B: 같은 blog 의 최근 published 키워드와 유사도 0.85 이상이면 cooldown 위반."""
    import json
    from datetime import timedelta
    from osmu_kr.models import (
        KSTATUS_ACTIVE, KeywordPoolItem, KeywordUsage,
        USAGE_PUBLISHED, now_utc, to_iso,
    )
    from osmu_kr.researcher.keyword_safety import KeywordSafety
    from osmu_kr.content_generator.embedder import StubEmbedder

    rs = fresh_researcher()
    embedder = StubEmbedder()
    ks = KeywordSafety(rs.storage, embedder=embedder)

    # 두 키워드 — 같은 텍스트라 stub embedding 같음 → cosine ≈ 1.0
    pub_kw = KeywordPoolItem(
        keyword_id="kw-pub", seed_keyword="다이어트",
        keyword="다이어트 식단 추천", status=KSTATUS_ACTIVE,
    )
    cand_kw = KeywordPoolItem(
        keyword_id="kw-cand", seed_keyword="다이어트",
        keyword="다이어트 식단 추천", status=KSTATUS_ACTIVE,
    )
    ks.ensure_keyword_embedding(pub_kw)
    ks.ensure_keyword_embedding(cand_kw)
    rs.storage.upsert_pool(pub_kw)
    rs.storage.upsert_pool(cand_kw)

    # 1일 전 발행 이력 등록
    one_day_ago = to_iso(now_utc() - timedelta(days=1))
    rs.storage.upsert_usage(KeywordUsage(
        id="u-001", keyword_id="kw-pub", blog_id="myblog",
        contents_id="c-001", status=USAGE_PUBLISHED,
        started_at=one_day_ago, published_at=one_day_ago,
    ))

    # 후보 키워드의 cooldown 체크
    conflict = ks.check_abuse_cooldown(
        "kw-cand", threshold=0.85, days=3.0, blog_id="myblog",
    )
    assert conflict is not None
    assert conflict.match.keyword_id == "kw-pub"
    assert conflict.match.similarity >= 0.85


def test_keyword_safety_cooldown_passes_when_old_enough():
    """4일 전 발행이면 3일 cooldown 안 걸려야 함."""
    import json
    from datetime import timedelta
    from osmu_kr.models import (
        KSTATUS_ACTIVE, KeywordPoolItem, KeywordUsage,
        USAGE_PUBLISHED, now_utc, to_iso,
    )
    from osmu_kr.researcher.keyword_safety import KeywordSafety
    from osmu_kr.content_generator.embedder import StubEmbedder

    rs = fresh_researcher()
    ks = KeywordSafety(rs.storage, embedder=StubEmbedder())

    a = KeywordPoolItem(keyword_id="kw-a", seed_keyword="x",
                          keyword="다이어트 식단 추천", status=KSTATUS_ACTIVE)
    b = KeywordPoolItem(keyword_id="kw-b", seed_keyword="x",
                          keyword="다이어트 식단 추천", status=KSTATUS_ACTIVE)
    ks.ensure_keyword_embedding(a)
    ks.ensure_keyword_embedding(b)
    rs.storage.upsert_pool(a)
    rs.storage.upsert_pool(b)

    four_days_ago = to_iso(now_utc() - timedelta(days=4))
    rs.storage.upsert_usage(KeywordUsage(
        id="u-x", keyword_id="kw-a", blog_id="myblog",
        contents_id="c-x", status=USAGE_PUBLISHED,
        started_at=four_days_ago, published_at=four_days_ago,
    ))

    conflict = ks.check_abuse_cooldown("kw-b", threshold=0.85, days=3.0,
                                        blog_id="myblog")
    assert conflict is None  # 4일 > 3일 → 통과


def test_safety_layer_start_lock_raises_on_cooldown_violation():
    """v13-B: SafetyLayer.start_lock 이 cooldown 위반 시 CooldownViolation 발생."""
    from datetime import timedelta
    from osmu_kr.models import (
        KSTATUS_ACTIVE, KeywordPoolItem, KeywordUsage,
        USAGE_PUBLISHED, now_utc, to_iso,
    )
    from osmu_kr.researcher.keyword_safety import KeywordSafety
    from osmu_kr.researcher.safety import CooldownViolation, SafetyLayer
    from osmu_kr.content_generator.embedder import StubEmbedder

    rs = fresh_researcher()
    ks = KeywordSafety(rs.storage, embedder=StubEmbedder())

    pub = KeywordPoolItem(keyword_id="kw-p", seed_keyword="x",
                            keyword="다이어트 식단 추천", status=KSTATUS_ACTIVE)
    cand = KeywordPoolItem(keyword_id="kw-c", seed_keyword="x",
                             keyword="다이어트 식단 추천", status=KSTATUS_ACTIVE)
    ks.ensure_keyword_embedding(pub)
    ks.ensure_keyword_embedding(cand)
    rs.storage.upsert_pool(pub)
    rs.storage.upsert_pool(cand)

    one_day_ago = to_iso(now_utc() - timedelta(days=1))
    rs.storage.upsert_usage(KeywordUsage(
        id="u-001", keyword_id="kw-p", blog_id="myblog",
        contents_id="c-001", status=USAGE_PUBLISHED,
        started_at=one_day_ago, published_at=one_day_ago,
    ))

    safety = SafetyLayer(rs.storage)
    try:
        safety.start_lock("kw-c", blog_id="myblog")
        assert False, "CooldownViolation 이 나야 함"
    except CooldownViolation as e:
        assert "cooldown" in str(e).lower()

    # skip_cooldown=True 면 통과
    usage = safety.start_lock("kw-c", blog_id="myblog", skip_cooldown=True)
    assert usage.id


def _cli_use_sqlite_backend():
    """CLI 테스트용 — SQLite 백엔드로 강제해 인스턴스 간 영속화 확보."""
    import os, tempfile
    tmp = tempfile.mkdtemp(prefix="osmu_cli_")
    os.environ["OSMU_STORAGE_BACKEND"] = "sqlite"
    os.environ["OSMU_SQLITE_PATH"] = os.path.join(tmp, "cli.db")
    return tmp


def _publish_setup_sqlite():
    """publisher 테스트용 — SQLite 백엔드 + 계정 + 콘텐츠 준비."""
    import os, tempfile
    from osmu_kr.models import Account, ContentRecord
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db = os.path.join(tempfile.mkdtemp(prefix="osmu_pub_"), "x.db")
    s = SqliteStorage(db_path=db)
    s.upsert_account(Account(id="acc-1", platform="tistory",
                              blog_id="myblog", login_id="me@k.com",
                              cookie_path="/tmp/none", is_active=1))
    return s


def test_publisher_mock_publishes_successfully():
    """next-4: MockPublisher 로 게이트 통과 → 발행 완료 갱신."""
    from datetime import timedelta
    from osmu_kr.models import (
        ContentRecord, KeywordUsage, USAGE_IN_PROGRESS, now_utc, to_iso,
    )
    from osmu_kr.publisher import MockPublisher, Publisher

    s = _publish_setup_sqlite()
    # 콘텐츠 + 진행 중인 usage 한 건
    s.append_content(ContentRecord(
        id="c-1", keyword="적금 추천", title="적금 추천 가이드",
        status="승인완료", refined_post="<h1>적금 추천</h1><p>본문</p>",
        # min_draft_minutes 통과를 위해 created_at 을 1시간 전으로
        created_at=to_iso(now_utc() - timedelta(hours=1)),
    ))
    s.upsert_usage(KeywordUsage(
        id="u-1", keyword_id="kw-1", contents_id="c-1",
        blog_id="myblog", status=USAGE_IN_PROGRESS,
    ))

    pub = Publisher(s, backend=MockPublisher())
    res = pub.publish("c-1")
    assert res.success, res.summary()
    rec = next(r for r in s.list_content() if r.id == "c-1")
    assert rec.status == "발행완료"
    assert rec.platform_url
    # usage 도 published 로 전이
    u = s.get_active_usage("kw-1")
    assert u is None   # 더 이상 in_progress 아님
    s.close()


def test_publisher_blocks_when_draft_too_fresh():
    """min_draft_minutes 미만이면 차단."""
    from osmu_kr.models import ContentRecord, now_utc, to_iso
    from osmu_kr.publisher import MockPublisher, Publisher

    s = _publish_setup_sqlite()
    # created_at = 방금 — min_draft_minutes (30) 미만
    s.append_content(ContentRecord(
        id="c-2", keyword="적금", refined_post="<h1>x</h1>",
        created_at=to_iso(now_utc()),
    ))
    pub = Publisher(s, backend=MockPublisher())
    res = pub.publish("c-2")
    assert not res.success
    assert "draft_too_fresh" in res.blocked_reason
    s.close()


def test_publisher_blocks_daily_limit():
    """daily_limit 초과 시 차단."""
    from datetime import timedelta
    from osmu_kr.models import (
        ContentRecord, KeywordUsage, USAGE_PUBLISHED, now_utc, to_iso,
    )
    from osmu_kr.publisher import MockPublisher, Publisher

    s = _publish_setup_sqlite()
    today = now_utc()
    # 오늘 이미 2건 published 된 상태
    for i in range(2):
        s.upsert_usage(KeywordUsage(
            id=f"u-pub-{i}", keyword_id=f"kw-{i}",
            contents_id=f"prev-{i}", blog_id="myblog",
            status=USAGE_PUBLISHED,
            started_at=to_iso(today - timedelta(hours=2 + i)),
            published_at=to_iso(today - timedelta(hours=1 + i)),
        ))
    # 새 콘텐츠 발행 시도 (draft 충분히 오래됨)
    s.append_content(ContentRecord(
        id="c-3", keyword="적금", refined_post="<h1>x</h1>",
        created_at=to_iso(today - timedelta(hours=2)),
    ))
    pub = Publisher(s, backend=MockPublisher())
    res = pub.publish("c-3")
    assert not res.success
    assert "daily_limit_exceeded" in res.blocked_reason
    s.close()


def test_publisher_tistory_blocked_without_real_env():
    """OSMU_PUBLISH_REAL 가 없으면 TistoryPlaywrightPublisher 가 드라이런 메시지."""
    import os
    from osmu_kr.models import ContentRecord, now_utc, to_iso
    from osmu_kr.publisher import Publisher, TistoryPlaywrightPublisher

    saved = os.environ.pop("OSMU_PUBLISH_REAL", None)
    s = _publish_setup_sqlite()
    # 콘텐츠 준비 (게이트 통과를 위해 충분히 오래된 draft)
    from datetime import timedelta
    s.append_content(ContentRecord(
        id="c-t", keyword="적금", refined_post="<h1>x</h1>",
        created_at=to_iso(now_utc() - timedelta(hours=2)),
    ))
    try:
        pub = Publisher(s, backend=TistoryPlaywrightPublisher())
        res = pub.publish("c-t")
        assert not res.success
        assert "OSMU_PUBLISH_REAL" in res.error
    finally:
        if saved is not None:
            os.environ["OSMU_PUBLISH_REAL"] = saved
        s.close()


def test_checker_passes_well_formed_html():
    """next-3: 잘 만든 HTML 통과."""
    from osmu_kr.checker import Checker
    body_p = "<p>적금 추천 비교 본문입니다. 수익률·우대조건을 살펴봅니다.</p>"
    html = (
        "<h1>적금 추천</h1>"
        + body_p * 20
        + "<h2>섹션 1 — 적금 추천 핵심</h2>"
        + body_p * 15
        + '<img alt="적금 비교 이미지" src="https://i/1.jpg">'
        + "<h2>섹션 2 — 적금 비교</h2>"
        + body_p * 15
        + "<h2>섹션 3 — 마무리</h2>"
        + body_p * 15
    )
    chk = Checker()
    res = chk.run(html, expected_image_count=1)
    assert res.passed, f"issues={res.issues}"
    assert res.h1_count == 1
    assert res.h2_count == 3
    assert res.img_with_alt == 1


def test_checker_flags_short_html_and_missing_alt():
    """next-3: 길이 부족 + alt 누락 자동 catch."""
    from osmu_kr.checker import Checker
    html = '<h1>x</h1><h2>y</h2><p>too short</p><img src="https://i/1.jpg">'
    chk = Checker()
    res = chk.run(html, expected_image_count=1)
    assert not res.passed
    assert any("char_count_too_low" in i for i in res.issues)
    assert any("h2_count_out_of_range" in i for i in res.issues)
    assert any("image_alt_missing" in i for i in res.issues)


def test_checker_keywords_present_check_with_blueprint():
    """next-3: assigned_keywords / commercial recommendations 가 본문에 등장하는지."""
    from osmu_kr.checker import Checker
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.keyword_context import KeywordContext

    bp = generate_blueprint(KeywordContext.from_keyword("적금 추천"), use_llm=False)
    # 본문에 키워드 있는 케이스
    good = (
        "<h1>적금 추천</h1>"
        + ("<p>적금 추천에 대해 알아봅니다. 수익률 비교.</p>" * 8)
        + "<h2>1</h2><p>본문.</p>" * 4
    )
    chk = Checker()
    res = chk.run(good, blueprint=bp)
    assert "적금 추천" in res.keywords_present


def test_checker_plagiarism_high_when_article_quotes_facts():
    """next-3: article 문장이 source 와 거의 같으면 plagiarism_max_sentence 높음."""
    import os
    os.environ["OSMU_EMBEDDER"] = "stub"
    from osmu_kr.checker import Checker
    from osmu_kr.content_generator.phase2 import FactItem, Phase2Result

    src = "데드바이데이라이트는 4vs1 비대칭 호러 게임으로 생존자가 발전기를 수리해 탈출합니다."
    # h1 / p 끝에 마침표 — 본문에 src 가 독립 문장으로 추출되도록
    html = (
        "<h1>DBD 초보 공략.</h1>"
        + f"<p>{src}</p>"
        + "<h2>섹션.</h2><p>본문입니다.</p>" * 6
    )
    phase2 = Phase2Result(keyword="DBD")
    phase2.sources_by_section = {2: [FactItem(fact_text=src, source_url="x")]}

    chk = Checker()
    res = chk.run(html, normalized_sources=phase2)
    assert res.plagiarism_max_sentence > 0.5, (
        f"max_sentence={res.plagiarism_max_sentence:.3f} — "
        f"stub embed 가 동일 문장에 대해 cosine ≈ 1.0 을 줘야 함"
    )


def test_cli_config_set_and_get():
    """next-2: CLI config-set/config-get 라운드트립."""
    import io, os
    from contextlib import redirect_stdout
    from osmu_kr.cli import main as cli_main

    _cli_use_sqlite_backend()
    saved_legacy = os.environ.pop("OSMU_GOLDEN_THRESHOLD", None)
    saved_new = os.environ.pop("OSMU_KEYWORD_GOLDEN_THRESHOLD", None)
    try:
        buf1 = io.StringIO()
        with redirect_stdout(buf1):
            rc = cli_main(["config-set", "--key", "keyword.golden_threshold",
                            "--value", "62"])
        assert rc == 0
        assert "62" in buf1.getvalue()

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc = cli_main(["config-get", "--key", "keyword.golden_threshold"])
        assert rc == 0
        out = buf2.getvalue()
        assert "'62'" in out
        assert "source=db" in out
    finally:
        if saved_legacy is not None:
            os.environ["OSMU_GOLDEN_THRESHOLD"] = saved_legacy
        if saved_new is not None:
            os.environ["OSMU_KEYWORD_GOLDEN_THRESHOLD"] = saved_new


def test_cli_account_add_and_list():
    """next-2: CLI account-add → account-list."""
    import io
    from contextlib import redirect_stdout
    from osmu_kr.cli import main as cli_main

    _cli_use_sqlite_backend()
    rc = cli_main(["account-add", "--id", "acc-cli-001",
                    "--blog-id", "myblog", "--login-id", "me@kakao.com"])
    assert rc == 0

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["account-list"])
    assert rc == 0
    out = buf.getvalue()
    assert "acc-cli-001" in out
    assert "myblog" in out


def test_cli_housekeeping_runs_without_error():
    """next-2: CLI housekeeping 실행."""
    import io
    from contextlib import redirect_stdout
    from osmu_kr.cli import main as cli_main

    _cli_use_sqlite_backend()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["housekeeping"])
    assert rc == 0
    assert "housekeeping" in buf.getvalue()


def test_cm_base_writer_default_write_from_blueprint_delegates_to_write():
    """cm-A: BaseWriter.write_from_blueprint default 구현이 write() 에 위임."""
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.interfaces import BaseWriter
    from osmu_kr.content_generator.keyword_context import KeywordContext

    captured = {}

    class W(BaseWriter):
        name = "w"
        def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적"):
            captured["keyword"] = keyword
            captured["raw_len"] = len(raw_content)
            captured["sources"] = list(sources or [])
            captured["tone"] = tone
            return f"<h1>{keyword}</h1><h2>x</h2><p>ok</p>"

    bp = generate_blueprint(KeywordContext.from_keyword("적금 추천"), use_llm=False)
    w = W()
    html = w.write_from_blueprint(bp, normalized_sources=None, images=None)
    assert html.startswith("<h1>")
    assert captured["keyword"] == "적금 추천"
    # 청사진이 텍스트로 직렬화돼서 raw_content 로 들어가야 함
    assert captured["raw_len"] > 0


def test_cm_generator_uses_blueprint_writer_path():
    """cm-C: Generator 가 write_from_blueprint() 를 우선 호출."""
    import os
    from osmu_kr.content_generator import Generator
    from osmu_kr.content_generator.generator import GeneratorConfig
    from osmu_kr.content_generator.interfaces import (
        BaseCrawler, BaseImageProvider, BaseWriter, CrawledPage, ImageItem,
    )

    captured = {"used": ""}

    class C(BaseCrawler):
        name = "c"
        def search(self, q, *, limit=5): return [f"https://x/{i}" for i in range(limit)]
        def scrape(self, url):
            return CrawledPage(url=url, title="t",
                                content=("적금 추천 글입니다. 금리 비교가 핵심. "
                                          "우대조건도 살펴봐야 합니다. " * 4))
    class W(BaseWriter):
        name = "w"
        def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적"):
            captured["used"] = "write"
            return f"<h1>{keyword}</h1><h2>x</h2><p>ok</p>"
        def write_from_blueprint(self, blueprint, normalized_sources=None, *, images=None, tone="전문적"):
            captured["used"] = "write_from_blueprint"
            captured["title"] = blueprint.title
            return f"<h1>{blueprint.title}</h1><h2>x</h2><p>ok</p>"
    class I(BaseImageProvider):
        name = "i"
        def search(self, q, *, count=3, slug="", alt_keyword=""):
            return [ImageItem(url=f"https://i/{i}.jpg", filename=f"x-{i}.jpg",
                                alt="a", source="s") for i in range(1, count + 1)]

    saved = os.environ.get("OSMU_EMBEDDER")
    os.environ["OSMU_EMBEDDER"] = "stub"
    try:
        g = Generator(crawler=C(), writer=W(), images=I(),
                      config=GeneratorConfig(min_h2_sections=1, min_paragraphs=1,
                                              min_images=0))
        result = g.generate("적금 추천", save=False)
    finally:
        if saved is None:
            os.environ.pop("OSMU_EMBEDDER", None)
        else:
            os.environ["OSMU_EMBEDDER"] = saved

    assert captured["used"] == "write_from_blueprint"
    assert "적금 추천" in captured["title"]
    assert result.refined_post.startswith("<h1>")


def test_v13f_accounts_round_trip_in_sqlite():
    """v13-F: accounts 테이블 CRUD."""
    import os, tempfile
    from osmu_kr.models import Account
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db = os.path.join(tempfile.mkdtemp(prefix="osmu_acct_"), "x.db")
    s = SqliteStorage(db_path=db)

    a = Account(
        id="acc-001", platform="tistory", blog_id="myblog",
        login_id="me@kakao.com",
        cookie_path="./cookies/tistory_myblog.json",
        is_active=1, note="primary",
    )
    s.upsert_account(a)
    fetched = s.get_account("acc-001")
    assert fetched is not None
    assert fetched.platform == "tistory"
    assert fetched.blog_id == "myblog"
    assert fetched.is_active == 1

    # 활성 계정 조회
    active = s.get_active_account(platform="tistory", blog_id="myblog")
    assert active is not None and active.id == "acc-001"

    # 비활성화
    a.is_active = 0
    s.upsert_account(a)
    assert s.get_active_account(platform="tistory", blog_id="myblog") is None
    s.close()


def test_v13e_housekeeping_pool_eviction_2tier():
    """v13-E: pool_max_size 초과 시 1순위/2순위 풀 삭제 정책."""
    import os
    from osmu_kr.config_manager import dot_to_env
    from osmu_kr.models import (
        KSTATUS_ACTIVE, KeywordPoolItem, ResearchHistoryRecord, to_iso, now_utc,
    )
    from osmu_kr.researcher.housekeeping import Housekeeping

    rs = fresh_researcher()
    # legacy env 제거 — config_mgr.set() 이 효력 발휘하도록
    os.environ.pop("OSMU_POOL_MAX_SIZE", None)
    os.environ.pop(dot_to_env("keyword.pool_max_size"), None)
    rs.config_mgr._cache_clear()
    # config 세팅: pool_max=3, eviction_eval_count=2, eviction_score_threshold=45
    rs.config_mgr.set("keyword.pool_max_size", 3)
    rs.config_mgr.set("keyword.pool_eviction_eval_count", 2)
    rs.config_mgr.set("keyword.pool_eviction_score_threshold", 45)
    rs.config_mgr.set("keyword.revival_days", 9999)  # revival 비활성

    # 4개 키워드 — kw1 은 저품질 반복(1순위 대상), 나머지는 신규
    base_dt = "2026-05-01T00:00:00+0000"
    rs.storage.upsert_pool(KeywordPoolItem(
        keyword_id="kw1", seed_keyword="x", keyword="저품질 반복",
        score=30.0, status=KSTATUS_ACTIVE, last_evaluated_at=base_dt,
    ))
    # 평가 이력 2건 — 평균 30
    rs.storage.append_history(ResearchHistoryRecord(
        keyword="저품질 반복", grade="부스러기", total_score=30.0,
    ))
    rs.storage.append_history(ResearchHistoryRecord(
        keyword="저품질 반복", grade="부스러기", total_score=30.0,
    ))
    for kid, kw, score in [
        ("kw2", "신규 b", 70.0), ("kw3", "신규 c", 65.0), ("kw4", "신규 d", 75.0),
    ]:
        rs.storage.upsert_pool(KeywordPoolItem(
            keyword_id=kid, seed_keyword="x", keyword=kw,
            score=score, status=KSTATUS_ACTIVE,
            last_evaluated_at=base_dt,
        ))

    hk = Housekeeping(rs.storage, evaluator=None, config_mgr=rs.config_mgr)
    report = hk.run()

    # 4개 → 3개로 줄어야 함. kw1 (저품질 반복) 이 1순위로 evict.
    assert report.pool_size_after <= 3
    assert report.evicted_primary == 1
    pool_after = [it.keyword_id for it in rs.storage.list_pool()]
    assert "kw1" not in pool_after


def test_v13e_housekeeping_archive_low_score_on_revival():
    """v13-E: revival 재평가에서 저품질 → archive."""
    from osmu_kr.models import (
        KSTATUS_ACTIVE, KSTATUS_ARCHIVED, KeywordPoolItem,
        Evaluation, normalize_status,
    )
    from osmu_kr.evaluator.base import BaseEvaluator
    from osmu_kr.researcher.housekeeping import Housekeeping

    class LowScoreEval(BaseEvaluator):
        name = "low_eval"
        def evaluate(self, keyword, *, seed=""):
            return Evaluation(score=30.0, raw={"e": "low"})

    rs = fresh_researcher()
    rs.config_mgr.set("keyword.revival_days", 0.001)   # 즉시 트리거
    rs.config_mgr.set("keyword.pool_eviction_score_threshold", 45)
    rs.config_mgr.set("keyword.pool_max_size", 100)    # eviction 안 일어나게

    rs.storage.upsert_pool(KeywordPoolItem(
        keyword_id="kw-old", seed_keyword="x", keyword="오래된 저품질",
        score=80.0, status=KSTATUS_ACTIVE,
        last_evaluated_at="2024-01-01T00:00:00+0000",
    ))

    hk = Housekeeping(rs.storage, evaluator=LowScoreEval(),
                       config_mgr=rs.config_mgr)
    report = hk.run()
    assert report.re_evaluated >= 1
    assert report.archived_low_quality >= 1
    item = rs.storage.get_pool("kw-old")
    assert normalize_status(item.status) == KSTATUS_ARCHIVED


def test_v13g_phase1_self_cannibalization_warning():
    """v13-G: Phase 1 종료 시 기존 글의 summary_embedding 과 cosine ≥ 0.75 면 경고."""
    import json
    from osmu_kr.content_generator.blueprint import generate_blueprint
    from osmu_kr.content_generator.collector import Collector
    from osmu_kr.content_generator.embedder import StubEmbedder
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage
    from osmu_kr.content_generator.keyword_context import KeywordContext
    from osmu_kr.models import ContentRecord

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, q, *, limit=5): return []
        def scrape(self, url): return CrawledPage(url=url, title="t", content="c")

    rs = fresh_researcher()
    embedder = StubEmbedder()
    # phase1 이 만들 embedding_input 과 동일한 텍스트로 미리 인코딩 (cosine ≈ 1.0)
    ctx = KeywordContext.from_keyword("적금 추천")
    expected_bp = generate_blueprint(ctx, use_llm=False)
    existing_emb = embedder.encode(expected_bp.embedding_input())

    rs.storage.append_content(ContentRecord(
        id="prev-001", keyword="적금 추천", title="적금 추천",
        status="generated",
        summary_embedding_json=json.dumps(existing_emb),
    ))

    c = Collector(StubCrawler(), embedder=embedder, storage=rs.storage)
    bp = c.phase1(ctx)

    # 경고가 raw_signals 에 기록돼야 함
    assert "self_cannibalization_warnings" in bp.raw_signals
    assert "prev-001" in bp.raw_signals["self_cannibalization_warnings"]


def test_v13_grade_thresholds():
    """v13-C: 부스러기(0~45) / 강철(46~59) / 황금(60+)."""
    from osmu_kr.models import (
        grade_from_score, GRADE_GOLDEN, GRADE_STEEL, GRADE_CRUMB,
    )
    # default 60/45
    assert grade_from_score(80.0) == GRADE_GOLDEN
    assert grade_from_score(60.0) == GRADE_GOLDEN
    assert grade_from_score(59.9) == GRADE_STEEL
    assert grade_from_score(50.0) == GRADE_STEEL
    assert grade_from_score(45.0) == GRADE_CRUMB
    assert grade_from_score(10.0) == GRADE_CRUMB
    # config-driven thresholds — golden 70, crumb 50
    assert grade_from_score(65, golden_threshold=70.0, crumb_upper=50.0) == GRADE_STEEL
    assert grade_from_score(70, golden_threshold=70.0, crumb_upper=50.0) == GRADE_GOLDEN
    assert grade_from_score(50, golden_threshold=70.0, crumb_upper=50.0) == GRADE_CRUMB


def test_v13_researcher_uses_config_golden_threshold():
    """v13-C: KeywordResearcher.golden_threshold 가 config_mgr 기반."""
    import os
    from osmu_kr.config_manager import dot_to_env

    rs = fresh_researcher()
    # 모든 매핑된 env 정리
    saved_legacy = os.environ.pop("OSMU_GOLDEN_THRESHOLD", None)
    new_name = dot_to_env("keyword.golden_threshold")
    saved_new = os.environ.pop(new_name, None)
    try:
        rs.config_mgr._cache_clear()
        # default — 코드 기본값(legacy cfg) 또는 config 60
        gt0 = rs.golden_threshold
        assert gt0 in (60.0, 65.0, 70.0)   # default 또는 cfg 기본값
        # DB 에 저장 → 우선순위 작동
        rs.config_mgr.set("keyword.golden_threshold", 65)
        assert rs.golden_threshold == 65.0
        # crumb_upper 도 config-driven
        assert rs.crumb_upper == 45.0
        rs.config_mgr.set("keyword.pool_eviction_score_threshold", 50)
        assert rs.crumb_upper == 50.0
    finally:
        if saved_legacy is not None:
            os.environ["OSMU_GOLDEN_THRESHOLD"] = saved_legacy
        if saved_new is not None:
            os.environ[new_name] = saved_new


def test_config_table_round_trip_in_sqlite():
    """v13-D: SQLite config 테이블 set/get/list/delete 라운드트립."""
    import os, tempfile
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db = os.path.join(tempfile.mkdtemp(prefix="osmu_cfg_"), "x.db")
    s = SqliteStorage(db_path=db)
    assert s.get_config("nope") is None
    s.set_config("keyword.golden_threshold", "65")
    assert s.get_config("keyword.golden_threshold") == "65"
    s.set_config("keyword.golden_threshold", "70")  # upsert
    assert s.get_config("keyword.golden_threshold") == "70"
    rows = s.list_config()
    assert ("keyword.golden_threshold", "70") in rows
    assert s.delete_config("keyword.golden_threshold") is True
    assert s.get_config("keyword.golden_threshold") is None
    s.close()


def test_config_manager_priority_env_over_db_over_default():
    """v13-D: env > db > default 우선순위.

    keyword.golden_threshold 로 검증 — fresh_researcher 가 건드리지 않는 키.
    """
    import os
    from osmu_kr.config_manager import ConfigManager, dot_to_env

    rs = fresh_researcher()
    # fresh_researcher 가 세팅하는 legacy env 정리
    saved_legacy = os.environ.pop("OSMU_GOLDEN_THRESHOLD", None)
    new_name = dot_to_env("keyword.golden_threshold")
    saved_new = os.environ.pop(new_name, None)
    try:
        cm = ConfigManager(rs.storage)
        # default 만 있는 상태
        assert cm.get_int("keyword.golden_threshold") == 60
        assert cm.get_source("keyword.golden_threshold") == "default"

        # DB 에 저장 → DB 가 default 보다 우선
        cm.set("keyword.golden_threshold", 65)
        assert cm.get_int("keyword.golden_threshold") == 65
        assert cm.get_source("keyword.golden_threshold") == "db"

        # env (dot notation) 가 DB 보다 우선
        os.environ[new_name] = "70"
        cm._cache_clear()
        assert cm.get_int("keyword.golden_threshold") == 70
        assert cm.get_source("keyword.golden_threshold") == "env"
    finally:
        if saved_legacy is not None:
            os.environ["OSMU_GOLDEN_THRESHOLD"] = saved_legacy
        if saved_new is None:
            os.environ.pop(new_name, None)
        else:
            os.environ[new_name] = saved_new


def test_config_manager_legacy_env_fallback():
    """기존 OSMU_GOLDEN_THRESHOLD 같은 legacy 환경변수도 매핑."""
    import os
    from osmu_kr.config_manager import ConfigManager, dot_to_env

    rs = fresh_researcher()
    cm = ConfigManager(rs.storage)
    # dot notation env 는 비우고 legacy 만
    new_name = dot_to_env("keyword.golden_threshold")
    saved_new = os.environ.pop(new_name, None)
    saved_legacy = os.environ.get("OSMU_GOLDEN_THRESHOLD")
    os.environ["OSMU_GOLDEN_THRESHOLD"] = "75"
    try:
        cm._cache_clear()
        assert cm.get_int("keyword.golden_threshold") == 75
        assert "legacy" in cm.get_source("keyword.golden_threshold")
    finally:
        if saved_new is not None:
            os.environ[new_name] = saved_new
        if saved_legacy is None:
            os.environ.pop("OSMU_GOLDEN_THRESHOLD", None)
        else:
            os.environ["OSMU_GOLDEN_THRESHOLD"] = saved_legacy


def test_config_manager_install_defaults():
    """v13-D: DEFAULTS 19개 항목을 DB 에 부트스트랩."""
    from osmu_kr.config_manager import ConfigManager, DEFAULTS

    rs = fresh_researcher()
    cm = ConfigManager(rs.storage)
    n = cm.install_defaults()
    assert n == len(DEFAULTS) == 19
    # 두 번째 호출은 이미 있으니 0
    n2 = cm.install_defaults()
    assert n2 == 0
    # overwrite=True 면 다시 19개
    n3 = cm.install_defaults(overwrite=True)
    assert n3 == 19


def test_keyword_usages_round_trip_in_sqlite():
    """v13 keyword_usages SQLite CRUD."""
    import os, tempfile
    from osmu_kr.models import KeywordUsage, USAGE_IN_PROGRESS, USAGE_PUBLISHED
    from osmu_kr.storage.sqlite_local import SqliteStorage

    db = os.path.join(tempfile.mkdtemp(prefix="osmu_usage_"), "x.db")
    s = SqliteStorage(db_path=db)

    u = KeywordUsage(
        keyword_id="kw-001", account_id="a1", blog_id="myblog",
        contents_id="c-001", status=USAGE_IN_PROGRESS,
    )
    s.upsert_usage(u)
    assert u.id   # 자동 부여

    fetched = s.get_active_usage("kw-001")
    assert fetched is not None
    assert fetched.status == USAGE_IN_PROGRESS

    # published 로 전이
    fetched.status = USAGE_PUBLISHED
    fetched.published_at = "2026-05-06T10:00:00+0000"
    s.upsert_usage(fetched)
    # 더 이상 활성 lock 아님
    assert s.get_active_usage("kw-001") is None
    # 전체 목록
    all_usages = s.list_usages_by_keyword("kw-001")
    assert len(all_usages) == 1
    assert all_usages[0].status == USAGE_PUBLISHED
    s.close()


def test_collector_phase1_rejects_generic_llm_blueprint_and_falls_back():
    """LLM 이 일반 템플릿(개념/활용/결론) 을 만들어도 phase1 이 reject 하고 룰 폴백."""
    import os
    from osmu_kr.content_generator import blueprint as bp_mod
    from osmu_kr.content_generator.collector import Collector
    from osmu_kr.content_generator.embedder import StubEmbedder
    from osmu_kr.content_generator.interfaces import BaseCrawler, CrawledPage
    from osmu_kr.content_generator.keyword_context import KeywordContext

    class StubCrawler(BaseCrawler):
        name = "stub"
        def search(self, q, *, limit=5): return []
        def scrape(self, url): return CrawledPage(url=url, title="t", content="c")

    bad = (
        '{"title": "x",'
        ' "target_reader": {"persona": "x", "knowledge_level": "초보", "primary_intent": "공략"},'
        ' "paragraphs": ['
        '   {"section_index": 1, "title": "개념", "paragraph_type": "llm_generated", "description": "x"},'
        '   {"section_index": 2, "title": "활용", "paragraph_type": "fact_based", "description": "x"},'
        '   {"section_index": 3, "title": "결론", "paragraph_type": "llm_generated", "description": "x"}'
        ' ], "intro": "x", "short_conclusion": "x"}'
    )
    saved_post = bp_mod._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    saved_use = os.environ.get("OSMU_USE_LLM_BLUEPRINT")
    os.environ["ANTHROPIC_API_KEY"] = "test"
    os.environ["OSMU_USE_LLM_BLUEPRINT"] = "1"
    bp_mod._post_anthropic = lambda *a, **kw: bad
    try:
        c = Collector(StubCrawler(), embedder=StubEmbedder())
        ctx = KeywordContext.from_keyword("데드바이데이라이트 공략")
        bp = c.phase1(ctx)
    finally:
        bp_mod._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key
        if saved_use is None:
            os.environ.pop("OSMU_USE_LLM_BLUEPRINT", None)
        else:
            os.environ["OSMU_USE_LLM_BLUEPRINT"] = saved_use

    # reject → 룰 폴백 → 단락 제목이 ‘개념/활용/결론’ 이 아니라야 함
    titles = " | ".join(p.title for p in bp.paragraphs)
    assert "개념" not in titles or "활용" not in titles or "결론" not in titles
    assert bp.raw_signals.get("reject_reasons")


def test_interpreter_disable_env_var_blocks_llm():
    """OSMU_DISABLE_LLM_INTERPRETER=1 이면 use_llm=True 라도 호출 안 한다."""
    import os
    from osmu_kr.content_generator import interpreter as itp

    saved_post = itp._post_anthropic
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    saved_disable = os.environ.get("OSMU_DISABLE_LLM_INTERPRETER")
    os.environ["ANTHROPIC_API_KEY"] = "test_key"
    os.environ["OSMU_DISABLE_LLM_INTERPRETER"] = "1"

    def boom(*_a, **_kw):
        raise AssertionError("LLM 이 호출되면 안 된다")
    itp._post_anthropic = boom
    try:
        ctx = itp.interpret("커피 그라인더 추천", use_llm=True)
    finally:
        itp._post_anthropic = saved_post
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key
        if saved_disable is None:
            os.environ.pop("OSMU_DISABLE_LLM_INTERPRETER", None)
        else:
            os.environ["OSMU_DISABLE_LLM_INTERPRETER"] = saved_disable

    # disable 시엔 룰만 → source='rule' (use_llm 자체가 강제로 False 가 됨)
    assert ctx.source == "rule"


TESTS = [
    test_evaluator_deterministic,
    test_expander_includes_seed_and_dedups,
    test_alchemy_produces_distinct_variants,
    test_run_seed_creates_pool_items,
    test_select_records_content_and_locks_via_usage,
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
    test_update_content_in_place,
    test_generator_retry_record_in_place,
    test_generator_retry_record_missing_id_raises,
    test_delete_content_csv_round_trip,
    test_delete_content_xlsx_round_trip,
    test_delete_content_mirror_delegates_to_local,
    test_keyword_classifier_game_domain,
    test_keyword_classifier_other_domains,
    test_heuristic_writer_game_keyword_no_seed_leak,
    test_heuristic_writer_finance_keyword_uses_finance_sections,
    test_revival_deprecates_low_score,
    # ── content_generator ──
    test_keyword_translator_korean_to_english_and_slug,
    test_picsum_image_provider_returns_image_items_with_roles,
    test_html_validator_detects_banned_phrases,
    test_strip_banned_phrases_removes_offending_paragraphs,
    test_heuristic_writer_no_banned_phrases_and_4_sections,
    test_chained_image_provider_dedup_and_renumber,
    test_heuristic_writer_produces_html_with_images,
    test_html_validation_detects_missing_images,
    test_repair_missing_images_appends_when_writer_skips,
    test_collector_with_stub_crawler,
    test_generator_full_pipeline_with_stubs,
    test_generator_firecrawl_fallback_to_template,
    test_generator_writer_failure_fallbacks_to_heuristic,
    # ── [1단계] KeywordContext: 입력 구조 정리 ──
    test_keyword_context_game_keyword_carries_topic,
    test_keyword_context_intent_inference,
    test_keyword_context_coerce_accepts_str_and_passthrough,
    test_collector_logs_topic_hint_for_game_keyword,
    test_collector_accepts_keyword_context_directly,
    # ── [2단계] interpreter: LLM 보강 + 폴백 ──
    test_keyword_context_topic_summary_filled_by_rule,
    test_interpreter_rule_only_when_use_llm_false,
    test_interpreter_llm_fallback_when_no_api_key,
    test_interpreter_llm_overrides_when_call_succeeds,
    test_interpreter_llm_failure_falls_back_to_rule,
    test_interpreter_passthrough_when_already_context,
    test_interpreter_disable_env_var_blocks_llm,
    # ── [3단계] collector Phase 1 — Blueprint + 검증 + 임베딩 ──
    test_blueprint_rule_mode_basic_shape,
    test_blueprint_llm_overrides_when_call_succeeds,
    test_blueprint_llm_failure_falls_back_to_rule,
    test_blueprint_validator_rejects_generic_template,
    test_blueprint_validator_accepts_concrete_template,
    test_stub_embedder_deterministic_and_dim,
    test_zero_embedder_returns_none,
    test_build_embedder_respects_env,
    test_collector_phase1_full_with_stub_embedder,
    # ── [4단계] Commercial + Phase 2 ──
    test_blueprint_rule_mode_includes_commercial_elements,
    test_blueprint_llm_response_with_commercial_elements,
    test_phase1_autofills_commercial_when_llm_omits_them,
    test_phase2_fact_mapping_with_stub_crawler,
    test_phase2_flags_domain_mismatch_on_off_topic_facts,
    test_phase2_flags_insufficient_facts,
    test_collector_phase1_rejects_generic_llm_blueprint_and_falls_back,
    # ── [5단계] SQLite 영속화 ──
    test_sqlite_storage_pool_round_trip,
    test_sqlite_storage_content_with_v9_fields_round_trip,
    test_sqlite_storage_history_round_trip,
    test_factory_builds_sqlite_when_backend_is_sqlite,
    test_generator_persists_phase1_phase2_payload_to_sqlite,
    # ── [6단계] PostgreSQL + pgvector ──
    test_postgres_storage_imports_without_db_url,
    test_postgres_factory_raises_without_url,
    test_postgres_storage_round_trip_when_db_available,
    # ── [v13-A] 키워드 상태 단순화 + keyword_usages ──
    test_normalize_status_v13_active_archived_only,
    test_safety_layer_v13_lock_lifecycle_happy_path,
    test_safety_layer_v13_rejects_double_lock,
    test_safety_layer_v13_failed_unlocks_and_keeps_published_at_empty,
    test_safety_layer_v13_archive_cancels_active_lock,
    test_recommend_v13_excludes_active_lock_and_published_in_blog,
    test_v13_grade_thresholds,
    test_v13_researcher_uses_config_golden_threshold,
    test_publisher_mock_publishes_successfully,
    test_publisher_blocks_when_draft_too_fresh,
    test_publisher_blocks_daily_limit,
    test_publisher_tistory_blocked_without_real_env,
    test_checker_passes_well_formed_html,
    test_checker_flags_short_html_and_missing_alt,
    test_checker_keywords_present_check_with_blueprint,
    test_checker_plagiarism_high_when_article_quotes_facts,
    test_cli_config_set_and_get,
    test_cli_account_add_and_list,
    test_cli_housekeeping_runs_without_error,
    test_cm_base_writer_default_write_from_blueprint_delegates_to_write,
    test_cm_generator_uses_blueprint_writer_path,
    test_v13f_accounts_round_trip_in_sqlite,
    test_v13e_housekeeping_pool_eviction_2tier,
    test_v13e_housekeeping_archive_low_score_on_revival,
    test_v13g_phase1_self_cannibalization_warning,
    test_config_table_round_trip_in_sqlite,
    test_config_manager_priority_env_over_db_over_default,
    test_config_manager_legacy_env_fallback,
    test_config_manager_install_defaults,
    test_keyword_usages_round_trip_in_sqlite,
    # ── [v13-B] keyword embedding + 씨드 중복 + 어뷰징 쿨다운 ──
    test_keyword_embedding_round_trip_in_sqlite,
    test_keyword_safety_seed_duplicate_finds_similar,
    test_keyword_safety_cooldown_blocks_recent_published_similar,
    test_keyword_safety_cooldown_passes_when_old_enough,
    test_safety_layer_start_lock_raises_on_cooldown_violation,
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
