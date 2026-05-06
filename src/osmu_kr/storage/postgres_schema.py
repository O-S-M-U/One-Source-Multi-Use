"""PostgreSQL + pgvector v9 스키마 (Neon 호스팅 기준).

[ 책임 ]
  · v9 spec 5개 테이블 DDL — SQLite 와 컬럼명·타입 동일.
  · summary_embedding 만 pgvector 의 vector(768) 타입.
  · pgvector 가 없는 환경(자체호스팅 등)은 자동 폴백 — text JSON 컬럼.

[ Neon 환경 ]
  · CREATE EXTENSION IF NOT EXISTS vector — Neon 은 pgvector 0.5+ 기본 제공.
  · 연결 문자열 형식: postgresql://user:pass@ep-xxx.region.aws.neon.tech/dbname?sslmode=require
  · pooled vs direct: psycopg3 ConnectionPool 은 pooled URL(`-pooler` suffix)에서도 안전.

[ 차이점 (vs SQLite) ]
  · INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY
  · TEXT → TEXT (동일)
  · REAL → DOUBLE PRECISION
  · INTEGER → INTEGER (동일)
  · summary_embedding_json (text) → summary_embedding (vector(768) 또는 text 폴백)
  · ON CONFLICT 절은 SQLite/Postgres 둘 다 지원하므로 동일 구문 사용 가능.

[ 인덱스 ]
  · summary_embedding 에 ivfflat 인덱스 — 자기잠식 1차 스크리닝의 ANN 검색에 필수.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)

EMBEDDING_DIM = 768


# ── DDL ─────────────────────────────────────────────────
DDL_EXTENSION = "CREATE EXTENSION IF NOT EXISTS vector;"

DDL_KEYWORDS = """
CREATE TABLE IF NOT EXISTS keywords (
    keyword_id            TEXT             PRIMARY KEY,
    keyword               TEXT             NOT NULL,
    seed_keyword          TEXT             NOT NULL DEFAULT '',
    status                TEXT             NOT NULL DEFAULT 'candidate',
    grade                 TEXT             NOT NULL DEFAULT '',
    profile               TEXT             NOT NULL DEFAULT '',
    weak_points           TEXT             NOT NULL DEFAULT '',
    is_alchemy            TEXT             NOT NULL DEFAULT 'N',
    original_keyword      TEXT             NOT NULL DEFAULT '',
    revival_count         INTEGER          NOT NULL DEFAULT 0,
    score                 DOUBLE PRECISION NOT NULL DEFAULT 0,
    search_volume         INTEGER          NOT NULL DEFAULT 0,
    competition           TEXT             NOT NULL DEFAULT '낮음',
    cpc                   DOUBLE PRECISION NOT NULL DEFAULT 0,
    commercial_intent     DOUBLE PRECISION NOT NULL DEFAULT 0,
    source                TEXT             NOT NULL DEFAULT 'heuristic',
    note                  TEXT             NOT NULL DEFAULT '',
    inprogress_locked_at  TEXT             NOT NULL DEFAULT '',
    published_at          TEXT             NOT NULL DEFAULT '',
    failed_at             TEXT             NOT NULL DEFAULT '',
    archived_at           TEXT             NOT NULL DEFAULT '',
    account_id            TEXT             NOT NULL DEFAULT '',
    last_status_reason    TEXT             NOT NULL DEFAULT '',
    last_evaluated_at     TEXT             NOT NULL DEFAULT '',
    created_at            TEXT             NOT NULL,
    updated_at            TEXT             NOT NULL
);
"""

# pgvector 사용 시 keywords 테이블에 embedding 컬럼 별도 추가 (있을 때만)
DDL_KEYWORDS_EMBEDDING_VECTOR = (
    "ALTER TABLE keywords ADD COLUMN IF NOT EXISTS embedding vector(768)"
)
DDL_KEYWORDS_EMBEDDING_TEXT = (
    "ALTER TABLE keywords ADD COLUMN IF NOT EXISTS embedding_json TEXT NOT NULL DEFAULT ''"
)
DDL_KEYWORDS_EMBED_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_keywords_embedding_ivfflat "
    "ON keywords USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
)

DDL_KEYWORD_EVALUATIONS = """
CREATE TABLE IF NOT EXISTS keyword_evaluations (
    id                  BIGSERIAL        PRIMARY KEY,
    keyword             TEXT             NOT NULL,
    grade               TEXT             NOT NULL DEFAULT '',
    total_score         DOUBLE PRECISION NOT NULL DEFAULT 0,
    profile             TEXT             NOT NULL DEFAULT '일반',
    datalab_score       DOUBLE PRECISION NOT NULL DEFAULT 0,
    datalab_direction   TEXT             NOT NULL DEFAULT '',
    blog_results        TEXT             NOT NULL DEFAULT '',
    blog_competition    TEXT             NOT NULL DEFAULT '',
    commercial_hits     TEXT             NOT NULL DEFAULT '',
    gtrends_score       DOUBLE PRECISION NOT NULL DEFAULT 0,
    weak_points         TEXT             NOT NULL DEFAULT '',
    is_alchemy          TEXT             NOT NULL DEFAULT 'N',
    original_keyword    TEXT             NOT NULL DEFAULT '',
    seed_keyword        TEXT             NOT NULL DEFAULT '',
    evaluator           TEXT             NOT NULL DEFAULT '',
    raw_signals_json    TEXT             NOT NULL DEFAULT '',
    score_breakdown_json TEXT            NOT NULL DEFAULT '',
    session_id          TEXT             NOT NULL DEFAULT '',
    result              TEXT             NOT NULL DEFAULT 'not_selected',
    created_at          TEXT             NOT NULL
);
"""

DDL_KEYWORD_USAGES = """
CREATE TABLE IF NOT EXISTS keyword_usages (
    id            TEXT      PRIMARY KEY,
    keyword_id    TEXT      NOT NULL,
    account_id    TEXT      NOT NULL DEFAULT '',
    blog_id       TEXT      NOT NULL DEFAULT '',
    contents_id   TEXT      NOT NULL DEFAULT '',
    status        TEXT      NOT NULL DEFAULT 'in_progress',
    started_at    TEXT      NOT NULL,
    published_at  TEXT      NOT NULL DEFAULT '',
    failed_at     TEXT      NOT NULL DEFAULT '',
    note          TEXT      NOT NULL DEFAULT ''
);
"""

DDL_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT     PRIMARY KEY,
    blog_id           TEXT     NOT NULL,
    login_id          TEXT     NOT NULL DEFAULT '',
    cookie_path       TEXT     NOT NULL DEFAULT '',
    cookie_updated_at TEXT     NOT NULL DEFAULT '',
    is_active         INTEGER  NOT NULL DEFAULT 1,
    note              TEXT     NOT NULL DEFAULT '',
    created_at        TEXT     NOT NULL
);
"""

# pgvector 사용 가능 시: vector(768)
# 폴백: text (json 직렬화)
def ddl_contents(use_vector: bool) -> str:
    embed_col = (
        f"summary_embedding         vector({EMBEDDING_DIM})"
        if use_vector
        else "summary_embedding         TEXT             NOT NULL DEFAULT ''"
    )
    return f"""
CREATE TABLE IF NOT EXISTS contents (
    id                       TEXT             PRIMARY KEY,
    keyword                  TEXT             NOT NULL,
    seed_keyword             TEXT             NOT NULL DEFAULT '',
    keyword_id               TEXT             NOT NULL DEFAULT '',
    original_source          TEXT             NOT NULL DEFAULT '',
    status                   TEXT             NOT NULL DEFAULT '대기중',
    title                    TEXT             NOT NULL DEFAULT '',
    title_final              TEXT             NOT NULL DEFAULT '',
    platform_url             TEXT             NOT NULL DEFAULT '',
    created_at               TEXT             NOT NULL,
    published_at             TEXT             NOT NULL DEFAULT '',
    raw_content              TEXT             NOT NULL DEFAULT '',
    refined_post             TEXT             NOT NULL DEFAULT '',
    image_urls               TEXT             NOT NULL DEFAULT '',
    error_log                TEXT             NOT NULL DEFAULT '',
    note                     TEXT             NOT NULL DEFAULT '',
    target_reader_json       TEXT             NOT NULL DEFAULT '',
    paragraph_blueprint_json TEXT             NOT NULL DEFAULT '',
    normalized_sources_json  TEXT             NOT NULL DEFAULT '',
    {embed_col},
    commercial_elements_json TEXT             NOT NULL DEFAULT '',
    publish_attempt_count    INTEGER          NOT NULL DEFAULT 0
);
"""


DDL_INDEXES_COMMON = [
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

# pgvector ivfflat 인덱스 — 자기잠식 검색 ANN 핵심
# (lists 는 row 가 1만 건 미만에선 100, 그 이상이면 sqrt(N) 권장)
DDL_INDEX_VECTOR = (
    "CREATE INDEX IF NOT EXISTS idx_contents_embedding_ivfflat "
    "ON contents USING ivfflat (summary_embedding vector_cosine_ops) WITH (lists = 100)"
)


def detect_pgvector(conn: "psycopg.Connection") -> bool:
    """pgvector 확장이 사용 가능한지 확인."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
            available = cur.fetchone() is not None
        return available
    except Exception as e:
        log.warning("[postgres_schema] pgvector 가용성 확인 실패: %s", e)
        return False


def initialize_schema(conn: "psycopg.Connection") -> bool:
    """모든 DDL 적용 — 멱등. pgvector 사용 여부를 반환."""
    use_vector = False
    with conn.cursor() as cur:
        # vector extension 시도 (없거나 권한 없으면 그냥 폴백)
        try:
            cur.execute(DDL_EXTENSION)
            use_vector = True
            log.info("[postgres_schema] pgvector 확장 활성")
        except Exception as e:
            log.warning("[postgres_schema] pgvector 비활성 → text 폴백: %s", e)
            conn.rollback()
            use_vector = False

        for ddl in (DDL_KEYWORDS, DDL_KEYWORD_EVALUATIONS,
                    DDL_KEYWORD_USAGES, DDL_ACCOUNTS,
                    ddl_contents(use_vector)):
            cur.execute(ddl)
        # keywords.embedding 컬럼 — pgvector 면 vector(768), 아니면 text
        try:
            if use_vector:
                cur.execute(DDL_KEYWORDS_EMBEDDING_VECTOR)
            else:
                cur.execute(DDL_KEYWORDS_EMBEDDING_TEXT)
        except Exception as e:
            log.info("[postgres_schema] keywords.embedding ALTER 보류: %s", e)
            conn.rollback()
        for idx in DDL_INDEXES_COMMON:
            cur.execute(idx)
        if use_vector:
            try:
                cur.execute(DDL_INDEX_VECTOR)
                cur.execute(DDL_KEYWORDS_EMBED_INDEX)
            except Exception as e:
                # ivfflat 인덱스는 row 0 일 때 종종 실패 — 무시 가능
                log.info("[postgres_schema] ivfflat 인덱스 생성 보류: %s", e)
                conn.rollback()
                conn.commit()  # 다른 DDL 은 살아있음
                return use_vector
    conn.commit()
    return use_vector
