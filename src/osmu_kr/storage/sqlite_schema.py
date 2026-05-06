"""SQLite v1 스키마 — v9 spec 5개 테이블.

[ 책임 ]
  · DDL 정의 + 초기화 + 멱등 적용.
  · v1: 단독 SQLite (로컬 파일).
  · v2+: PostgreSQL + pgvector 마이그레이션 시 컬럼명·타입을 그대로 매핑할 수 있도록
         v9 spec 의 필드명을 그대로 따른다.

[ 테이블 ]
  1) keywords            — 키워드 자체 + 점수·등급·풍부화 필드 (KeywordPoolItem 영속화)
  2) keyword_evaluations — 평가 시점별 스냅샷 (ResearchHistoryRecord 영속화)
  3) keyword_usages      — 키워드 사용 이력 (쿨다운·중복 진입 차단용, v1 빈 테이블)
  4) accounts            — Tistory 계정 + 쿠키 (multi-account v2, v1 빈 테이블)
  5) contents            — 콘텐츠 — Phase 1·2 산출물(JSON 컬럼) + refined_post

[ JSON 컬럼 ]
  · target_reader_json        — TargetReader (persona / knowledge_level / primary_intent)
  · paragraph_blueprint_json  — List[ParagraphBlock]
  · normalized_sources_json   — Dict[section_index → List[FactItem]]
  · summary_embedding_json    — List[float] (768-dim, ko-sroberta-multitask)
  · commercial_elements_json  — CommercialElements (recommendations/comparison/cta)

  v1 SQLite 는 JSON 텍스트로 저장, v2+ pgvector 로 summary_embedding 마이그.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


# ── DDL ─────────────────────────────────────────────────
DDL_KEYWORDS = """
CREATE TABLE IF NOT EXISTS keywords (
    keyword_id        TEXT    PRIMARY KEY,
    keyword           TEXT    NOT NULL,
    seed_keyword      TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'golden',
    grade             TEXT    NOT NULL DEFAULT '',
    profile           TEXT    NOT NULL DEFAULT '',
    weak_points       TEXT    NOT NULL DEFAULT '',
    is_alchemy        TEXT    NOT NULL DEFAULT 'N',
    original_keyword  TEXT    NOT NULL DEFAULT '',
    revival_count     INTEGER NOT NULL DEFAULT 0,
    score             REAL    NOT NULL DEFAULT 0,
    search_volume     INTEGER NOT NULL DEFAULT 0,
    competition       TEXT    NOT NULL DEFAULT '낮음',
    cpc               REAL    NOT NULL DEFAULT 0,
    commercial_intent REAL    NOT NULL DEFAULT 0,
    source            TEXT    NOT NULL DEFAULT 'heuristic',
    note              TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
"""

DDL_KEYWORD_EVALUATIONS = """
CREATE TABLE IF NOT EXISTS keyword_evaluations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword             TEXT    NOT NULL,
    grade               TEXT    NOT NULL DEFAULT '',
    total_score         REAL    NOT NULL DEFAULT 0,
    profile             TEXT    NOT NULL DEFAULT '일반',
    datalab_score       REAL    NOT NULL DEFAULT 0,
    datalab_direction   TEXT    NOT NULL DEFAULT '',
    blog_results        TEXT    NOT NULL DEFAULT '',
    blog_competition    TEXT    NOT NULL DEFAULT '',
    commercial_hits     TEXT    NOT NULL DEFAULT '',
    gtrends_score       REAL    NOT NULL DEFAULT 0,
    weak_points         TEXT    NOT NULL DEFAULT '',
    is_alchemy          TEXT    NOT NULL DEFAULT 'N',
    original_keyword    TEXT    NOT NULL DEFAULT '',
    seed_keyword        TEXT    NOT NULL DEFAULT '',
    evaluator           TEXT    NOT NULL DEFAULT '',
    raw_signals_json    TEXT    NOT NULL DEFAULT '',
    score_breakdown_json TEXT   NOT NULL DEFAULT '',
    session_id          TEXT    NOT NULL DEFAULT '',
    result              TEXT    NOT NULL DEFAULT 'not_selected',
    created_at          TEXT    NOT NULL
);
"""

DDL_KEYWORD_USAGES = """
CREATE TABLE IF NOT EXISTS keyword_usages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword       TEXT    NOT NULL,
    seed_keyword  TEXT    NOT NULL DEFAULT '',
    used_at       TEXT    NOT NULL,
    content_id    TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT ''
);
"""

DDL_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT    PRIMARY KEY,
    blog_id           TEXT    NOT NULL,
    login_id          TEXT    NOT NULL DEFAULT '',
    cookie_path       TEXT    NOT NULL DEFAULT '',
    cookie_updated_at TEXT    NOT NULL DEFAULT '',
    is_active         INTEGER NOT NULL DEFAULT 1,
    note              TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL
);
"""

DDL_CONTENTS = """
CREATE TABLE IF NOT EXISTS contents (
    id                       TEXT    PRIMARY KEY,
    keyword                  TEXT    NOT NULL,
    seed_keyword             TEXT    NOT NULL DEFAULT '',
    keyword_id               TEXT    NOT NULL DEFAULT '',
    original_source          TEXT    NOT NULL DEFAULT '',
    status                   TEXT    NOT NULL DEFAULT '대기중',
    title                    TEXT    NOT NULL DEFAULT '',
    title_final              TEXT    NOT NULL DEFAULT '',
    platform_url             TEXT    NOT NULL DEFAULT '',
    created_at               TEXT    NOT NULL,
    published_at             TEXT    NOT NULL DEFAULT '',
    raw_content              TEXT    NOT NULL DEFAULT '',
    refined_post             TEXT    NOT NULL DEFAULT '',
    image_urls               TEXT    NOT NULL DEFAULT '',
    error_log                TEXT    NOT NULL DEFAULT '',
    note                     TEXT    NOT NULL DEFAULT '',
    target_reader_json       TEXT    NOT NULL DEFAULT '',
    paragraph_blueprint_json TEXT    NOT NULL DEFAULT '',
    normalized_sources_json  TEXT    NOT NULL DEFAULT '',
    summary_embedding_json   TEXT    NOT NULL DEFAULT '',
    commercial_elements_json TEXT    NOT NULL DEFAULT '',
    publish_attempt_count    INTEGER NOT NULL DEFAULT 0
);
"""

DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON keywords(keyword)",
    "CREATE INDEX IF NOT EXISTS idx_keywords_status  ON keywords(status)",
    "CREATE INDEX IF NOT EXISTS idx_evaluations_keyword ON keyword_evaluations(keyword)",
    "CREATE INDEX IF NOT EXISTS idx_evaluations_created ON keyword_evaluations(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usages_keyword ON keyword_usages(keyword)",
    "CREATE INDEX IF NOT EXISTS idx_usages_used_at ON keyword_usages(used_at)",
    "CREATE INDEX IF NOT EXISTS idx_contents_keyword ON contents(keyword)",
    "CREATE INDEX IF NOT EXISTS idx_contents_status  ON contents(status)",
    "CREATE INDEX IF NOT EXISTS idx_contents_created ON contents(created_at)",
]

DDL_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def initialize_schema(conn: sqlite3.Connection) -> None:
    """모든 DDL 적용 — 멱등. 새 DB 또는 기존 DB 어디서든 안전."""
    cur = conn.cursor()
    for ddl in (DDL_META, DDL_KEYWORDS, DDL_KEYWORD_EVALUATIONS,
                DDL_KEYWORD_USAGES, DDL_ACCOUNTS, DDL_CONTENTS):
        cur.execute(ddl)
    for idx in DDL_INDEXES:
        cur.execute(idx)
    cur.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)))
    conn.commit()


def open_connection(db_path: str) -> sqlite3.Connection:
    """SQLite 커넥션 열기 + 스키마 초기화. row_factory 는 sqlite3.Row."""
    p = Path(db_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    # 외래키·동시성 안정성을 위한 PRAGMA
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    initialize_schema(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """간단 트랜잭션 컨텍스트."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
