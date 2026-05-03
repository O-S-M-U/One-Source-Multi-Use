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
