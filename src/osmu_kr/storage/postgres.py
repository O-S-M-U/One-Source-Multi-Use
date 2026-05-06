"""PostgresStorage — v9 spec 5개 테이블 PostgreSQL + pgvector 백엔드 (Neon 권장).

[ 설계 ]
  · BaseStorage 인터페이스 100% — SqliteStorage 와 동일 시그니처.
  · summary_embedding 은 pgvector 의 vector(768) 또는 text(json) 폴백.
  · psycopg3 Connection — 짧은 쿼리는 단일 커넥션으로 충분. 동시성 늘면 ConnectionPool 로 확장.
  · 테스트 환경이 PostgreSQL 미사용일 때 import 자체가 실패하면 안 되므로,
    psycopg/pgvector 는 lazy import.

[ 자기잠식 1차 스크리닝 ]
  · find_similar_contents(embedding, top_k=5) — pgvector cosine ANN.
  · 미설치 환경(text 폴백)에선 NotImplementedError. 사용자가 명시적으로 “자기잠식 검색 불가” 신호 받음.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Tuple

from ..models import (
    ContentRecord, KeywordPoolItem, KeywordUsage, ResearchHistoryRecord,
    USAGE_IN_PROGRESS, normalize_status, now_utc, to_iso,
)
from .base import BaseStorage
from .postgres_schema import (
    EMBEDDING_DIM, detect_pgvector, initialize_schema,
)

log = logging.getLogger(__name__)


class PostgresStorage(BaseStorage):
    """PostgreSQL + pgvector 단독 백엔드 (Neon 등)."""
    name = "postgres"

    def __init__(self, database_url: str,
                 *, pool_min: int = 1, pool_max: int = 4):
        if not database_url:
            raise ValueError("database_url 이 비어 있습니다 — OSMU_DATABASE_URL 확인")
        try:
            import psycopg  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "psycopg(>=3.1) 이 설치돼 있어야 합니다. "
                "pip install 'psycopg[binary]' pgvector"
            ) from e

        self.database_url = database_url
        self.pool_min = pool_min
        self.pool_max = pool_max
        self._conn = None
        self._use_vector = False
        # 즉시 연결 + 스키마 초기화
        _ = self.conn

    # ── 커넥션 lazy ──────────────────────────────────────
    @property
    def conn(self):
        if self._conn is None:
            import psycopg
            self._conn = psycopg.connect(self.database_url, autocommit=False)
            self._use_vector = initialize_schema(self._conn)
            if self._use_vector:
                # pgvector adapter 등록 — vector 타입 입출력
                try:
                    from pgvector.psycopg import register_vector
                    register_vector(self._conn)
                except Exception as e:
                    log.warning("[postgres] pgvector adapter 등록 실패 → text 폴백: %s", e)
                    self._use_vector = False
        return self._conn

    @property
    def use_vector(self) -> bool:
        _ = self.conn
        return self._use_vector

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ── 헬퍼 ─────────────────────────────────────────────
    def _exec(self, sql: str, params: Tuple = ()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            self.conn.commit()
            return cur

    def _embedding_for_db(self, embedding_json: str) -> Any:
        """ContentRecord.summary_embedding_json (text) → DB 값.

        - pgvector 사용 시: list[float] 로 디코딩
        - 텍스트 폴백 시: 원본 JSON 텍스트 그대로
        """
        if not embedding_json:
            return None if self.use_vector else ""
        if not self.use_vector:
            return embedding_json
        try:
            vec = json.loads(embedding_json)
            if not isinstance(vec, list):
                return None
            if len(vec) != EMBEDDING_DIM:
                # 차원 불일치 — 저장 거부, None 으로 (자기잠식 검색에서 제외)
                log.warning("[postgres] embedding 차원 %d ≠ %d → NULL 저장",
                              len(vec), EMBEDDING_DIM)
                return None
            return vec
        except Exception as e:
            log.warning("[postgres] embedding json 파싱 실패: %s", e)
            return None

    @staticmethod
    def _embedding_from_db(value: Any) -> str:
        """DB 값 → ContentRecord.summary_embedding_json."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        # pgvector list 또는 numpy array
        try:
            return json.dumps(list(value), ensure_ascii=False)
        except Exception:
            return ""

    # ── pool ────────────────────────────────────────────
    def _row_to_pool(self, row: tuple) -> KeywordPoolItem:
        (keyword_id, keyword, seed_keyword, status, grade, profile,
         weak_points, is_alchemy, original_keyword, revival_count,
         score, search_volume, competition, cpc, commercial_intent,
         source, note,
         inprogress_locked_at, published_at, failed_at, archived_at,
         account_id, last_status_reason,
         last_evaluated_at, embedding_value,
         created_at, updated_at) = row
        return KeywordPoolItem(
            keyword_id=keyword_id, seed_keyword=seed_keyword, keyword=keyword,
            search_volume=int(search_volume or 0),
            competition=competition or "낮음",
            cpc=float(cpc or 0),
            commercial_intent=float(commercial_intent or 0),
            score=float(score or 0),
            status=normalize_status(status or "candidate"),
            created_at=created_at,
            updated_at=updated_at,
            source=source or "heuristic",
            note=note or "",
            grade=grade or "",
            profile=profile or "",
            weak_points=weak_points or "",
            is_alchemy=is_alchemy or "N",
            original_keyword=original_keyword or "",
            revival_count=int(revival_count or 0),
            inprogress_locked_at=inprogress_locked_at or "",
            published_at=published_at or "",
            failed_at=failed_at or "",
            archived_at=archived_at or "",
            account_id=account_id or "",
            last_status_reason=last_status_reason or "",
            last_evaluated_at=last_evaluated_at or "",
            embedding_json=self._embedding_from_db(embedding_value),
        )

    @property
    def _pool_cols(self) -> str:
        # pgvector 활성 시 keywords.embedding (vector), 아니면 embedding_json (text)
        embed_col = "embedding" if self.use_vector else "embedding_json"
        return (
            "keyword_id, keyword, seed_keyword, status, grade, profile, "
            "weak_points, is_alchemy, original_keyword, revival_count, "
            "score, search_volume, competition, cpc, commercial_intent, "
            "source, note, "
            "inprogress_locked_at, published_at, failed_at, archived_at, "
            "account_id, last_status_reason, "
            f"last_evaluated_at, {embed_col}, "
            "created_at, updated_at"
        )

    _POOL_COLS = ""    # 호환 — 더 이상 사용되지 않음

    def list_pool(self) -> List[KeywordPoolItem]:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT {self._pool_cols} FROM keywords ORDER BY created_at")
            return [self._row_to_pool(r) for r in cur.fetchall()]

    def get_pool(self, keyword_id: str) -> Optional[KeywordPoolItem]:
        if not keyword_id:
            return None
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT {self._pool_cols} FROM keywords WHERE keyword_id = %s",
                        (keyword_id,))
            row = cur.fetchone()
            return self._row_to_pool(row) if row else None

    def upsert_pool(self, item: KeywordPoolItem) -> None:
        item.updated_at = to_iso(now_utc())
        if not item.created_at:
            item.created_at = item.updated_at
        embed_val = self._embedding_for_db(item.embedding_json)
        embed_col = "embedding" if self.use_vector else "embedding_json"
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO keywords ({self._pool_cols})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s,
                        %s, %s)
                ON CONFLICT (keyword_id) DO UPDATE SET
                    keyword=EXCLUDED.keyword,
                    seed_keyword=EXCLUDED.seed_keyword,
                    status=EXCLUDED.status,
                    grade=EXCLUDED.grade,
                    profile=EXCLUDED.profile,
                    weak_points=EXCLUDED.weak_points,
                    is_alchemy=EXCLUDED.is_alchemy,
                    original_keyword=EXCLUDED.original_keyword,
                    revival_count=EXCLUDED.revival_count,
                    score=EXCLUDED.score,
                    search_volume=EXCLUDED.search_volume,
                    competition=EXCLUDED.competition,
                    cpc=EXCLUDED.cpc,
                    commercial_intent=EXCLUDED.commercial_intent,
                    source=EXCLUDED.source,
                    note=EXCLUDED.note,
                    inprogress_locked_at=EXCLUDED.inprogress_locked_at,
                    published_at=EXCLUDED.published_at,
                    failed_at=EXCLUDED.failed_at,
                    archived_at=EXCLUDED.archived_at,
                    account_id=EXCLUDED.account_id,
                    last_status_reason=EXCLUDED.last_status_reason,
                    last_evaluated_at=EXCLUDED.last_evaluated_at,
                    {embed_col}=EXCLUDED.{embed_col},
                    updated_at=EXCLUDED.updated_at
                """,
                (item.keyword_id, item.keyword, item.seed_keyword,
                 item.status, item.grade, item.profile,
                 item.weak_points, item.is_alchemy, item.original_keyword,
                 item.revival_count,
                 item.score, item.search_volume, item.competition,
                 item.cpc, item.commercial_intent,
                 item.source, item.note,
                 item.inprogress_locked_at, item.published_at,
                 item.failed_at, item.archived_at,
                 item.account_id, item.last_status_reason,
                 item.last_evaluated_at, embed_val,
                 item.created_at, item.updated_at),
            )
        self.conn.commit()

    def delete_pool(self, keyword_id: str) -> bool:
        if not keyword_id:
            return False
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM keywords WHERE keyword_id = %s", (keyword_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def replace_pool(self, items: List[KeywordPoolItem]) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM keywords")
            for it in items:
                if not it.created_at:
                    it.created_at = to_iso(now_utc())
                if not it.updated_at:
                    it.updated_at = it.created_at
                embed_val = self._embedding_for_db(it.embedding_json)
                cur.execute(
                    f"INSERT INTO keywords ({self._pool_cols}) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    " %s, %s, %s, %s, %s, %s, %s, "
                    " %s, %s, %s, %s, %s, %s, "
                    " %s, %s, "
                    " %s, %s)",
                    (it.keyword_id, it.keyword, it.seed_keyword,
                     it.status, it.grade, it.profile,
                     it.weak_points, it.is_alchemy, it.original_keyword,
                     it.revival_count,
                     it.score, it.search_volume, it.competition,
                     it.cpc, it.commercial_intent,
                     it.source, it.note,
                     it.inprogress_locked_at, it.published_at,
                     it.failed_at, it.archived_at,
                     it.account_id, it.last_status_reason,
                     it.last_evaluated_at, embed_val,
                     it.created_at, it.updated_at),
                )
        self.conn.commit()

    # ── content ─────────────────────────────────────────
    _CONTENT_COLS = """
        id, keyword, seed_keyword, keyword_id, original_source,
        status, title, title_final, platform_url,
        created_at, published_at, raw_content, refined_post,
        image_urls, error_log, note,
        target_reader_json, paragraph_blueprint_json,
        normalized_sources_json, summary_embedding,
        commercial_elements_json, publish_attempt_count
    """

    def _row_to_content(self, row: tuple) -> ContentRecord:
        (id_, keyword, seed_keyword, keyword_id, original_source,
         status, title, title_final, platform_url,
         created_at, published_at, raw_content, refined_post,
         image_urls, error_log, note,
         target_reader_json, paragraph_blueprint_json,
         normalized_sources_json, summary_embedding,
         commercial_elements_json, publish_attempt_count) = row
        return ContentRecord(
            id=id_, keyword=keyword,
            seed_keyword=seed_keyword or "",
            keyword_id=keyword_id or "",
            original_source=original_source or "",
            status=status or "대기중",
            title=title or "",
            title_final=title_final or "",
            platform_url=platform_url or "",
            created_at=created_at,
            published_at=published_at or "",
            raw_content=raw_content or "",
            refined_post=refined_post or "",
            image_urls=image_urls or "",
            error_log=error_log or "",
            note=note or "",
            target_reader_json=target_reader_json or "",
            paragraph_blueprint_json=paragraph_blueprint_json or "",
            normalized_sources_json=normalized_sources_json or "",
            summary_embedding_json=self._embedding_from_db(summary_embedding),
            commercial_elements_json=commercial_elements_json or "",
            publish_attempt_count=int(publish_attempt_count or 0),
        )

    def list_content(self) -> List[ContentRecord]:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT {self._CONTENT_COLS} FROM contents ORDER BY created_at")
            return [self._row_to_content(r) for r in cur.fetchall()]

    def append_content(self, record: ContentRecord) -> None:
        if not record.id:
            raise ValueError("ContentRecord.id 필수")
        if not record.created_at:
            record.created_at = to_iso(now_utc())
        embed_val = self._embedding_for_db(record.summary_embedding_json)
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO contents ({self._CONTENT_COLS}) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                " %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (record.id, record.keyword, record.seed_keyword,
                 record.keyword_id, record.original_source,
                 record.status, record.title, record.title_final, record.platform_url,
                 record.created_at, record.published_at,
                 record.raw_content, record.refined_post,
                 record.image_urls, record.error_log, record.note,
                 record.target_reader_json, record.paragraph_blueprint_json,
                 record.normalized_sources_json, embed_val,
                 record.commercial_elements_json, record.publish_attempt_count),
            )
        self.conn.commit()

    def replace_content(self, records: List[ContentRecord]) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM contents")
            for r in records:
                if not r.created_at:
                    r.created_at = to_iso(now_utc())
                embed_val = self._embedding_for_db(r.summary_embedding_json)
                cur.execute(
                    f"INSERT INTO contents ({self._CONTENT_COLS}) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    " %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (r.id, r.keyword, r.seed_keyword,
                     r.keyword_id, r.original_source,
                     r.status, r.title, r.title_final, r.platform_url,
                     r.created_at, r.published_at,
                     r.raw_content, r.refined_post,
                     r.image_urls, r.error_log, r.note,
                     r.target_reader_json, r.paragraph_blueprint_json,
                     r.normalized_sources_json, embed_val,
                     r.commercial_elements_json, r.publish_attempt_count),
                )
        self.conn.commit()

    def delete_content(self, content_id: str) -> bool:
        if not content_id:
            return False
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM contents WHERE id = %s", (content_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def update_content(self, content_id: str, **fields) -> bool:
        if not content_id:
            return False
        protected = {"id", "created_at"}
        # ContentRecord 필드만 허용
        allowed = set(ContentRecord.HEADER) - protected
        cleaned = {k: v for k, v in fields.items() if k in allowed}
        if not cleaned:
            return False
        # summary_embedding_json 은 vector 컬럼으로 매핑되므로 별도 처리
        sql_assigns = []
        params: List[Any] = []
        for k, v in cleaned.items():
            if k == "summary_embedding_json":
                sql_assigns.append("summary_embedding = %s")
                params.append(self._embedding_for_db(v or ""))
            else:
                sql_assigns.append(f"{k} = %s")
                params.append(v)
        params.append(content_id)
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE contents SET {', '.join(sql_assigns)} WHERE id = %s", params,
            )
            self.conn.commit()
            return cur.rowcount > 0

    # ── research_history (keyword_evaluations 테이블) ────
    def append_history(self, record: ResearchHistoryRecord) -> None:
        if not record.created_at:
            record.created_at = to_iso(now_utc())
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO keyword_evaluations (
                    keyword, grade, total_score, profile,
                    datalab_score, datalab_direction,
                    blog_results, blog_competition,
                    commercial_hits, gtrends_score,
                    weak_points, is_alchemy, original_keyword,
                    seed_keyword, evaluator,
                    raw_signals_json, score_breakdown_json,
                    session_id, result, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (record.keyword, record.grade, record.total_score, record.profile,
                 record.datalab_score, record.datalab_direction,
                 record.blog_results, record.blog_competition,
                 record.commercial_hits, record.gtrends_score,
                 record.weak_points, record.is_alchemy, record.original_keyword,
                 record.seed_keyword, record.evaluator,
                 "", "", "", "not_selected", record.created_at),
            )
        self.conn.commit()

    def list_history(self) -> List[ResearchHistoryRecord]:
        cols = """
            keyword, grade, total_score, profile,
            datalab_score, datalab_direction,
            blog_results, blog_competition,
            commercial_hits, gtrends_score,
            weak_points, is_alchemy, original_keyword,
            seed_keyword, evaluator, created_at
        """
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM keyword_evaluations ORDER BY created_at")
            out: List[ResearchHistoryRecord] = []
            for r in cur.fetchall():
                out.append(ResearchHistoryRecord(
                    keyword=r[0],
                    grade=r[1] or "",
                    total_score=float(r[2] or 0),
                    profile=r[3] or "일반",
                    datalab_score=float(r[4] or 0),
                    datalab_direction=r[5] or "",
                    blog_results=r[6] or "",
                    blog_competition=r[7] or "",
                    commercial_hits=r[8] or "",
                    gtrends_score=float(r[9] or 0),
                    weak_points=r[10] or "",
                    is_alchemy=r[11] or "N",
                    original_keyword=r[12] or "",
                    seed_keyword=r[13] or "",
                    evaluator=r[14] or "",
                    created_at=r[15],
                ))
            return out

    # ── v13-B keyword embedding ANN (씨드 중복 + 어뷰징) ─
    def find_similar_keywords(self, embedding: List[float],
                                *, top_k: int = 10,
                                exclude_id: str = "",
                                only_status: str = "active",
                                ) -> List[Tuple[KeywordPoolItem, float]]:
        """keywords.embedding 코사인 ANN — 씨드 중복 / 어뷰징 쿨다운 비교.

        Returns: [(KeywordPoolItem, similarity 0..1), ...] 유사도 내림차순.
        """
        if not self.use_vector:
            raise NotImplementedError(
                "pgvector 가 비활성 — keyword embedding ANN 검색은 pgvector 환경에서만 가능."
            )
        if not embedding or len(embedding) != EMBEDDING_DIM:
            return []
        sql = (
            f"SELECT {self._pool_cols}, "
            "       1 - (embedding <=> %s::vector) / 2 AS similarity "
            "FROM keywords "
            "WHERE embedding IS NOT NULL "
            + ("AND status = %s " if only_status else "")
            + ("AND keyword_id <> %s " if exclude_id else "")
            + "ORDER BY embedding <=> %s::vector ASC "
            + "LIMIT %s"
        )
        params: List[Any] = [embedding]
        if only_status:
            params.append(only_status)
        if exclude_id:
            params.append(exclude_id)
        params += [embedding, top_k]
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            out: List[Tuple[KeywordPoolItem, float]] = []
            for row in cur.fetchall():
                item = self._row_to_pool(row[:-1])
                sim = float(row[-1]) if row[-1] is not None else 0.0
                out.append((item, sim))
            return out

    # ── 자기잠식 1차 스크리닝 (pgvector ANN) ─────────────
    def find_similar_contents(self, embedding: List[float],
                                *, top_k: int = 5,
                                exclude_id: str = "") -> List[Tuple[ContentRecord, float]]:
        """summary_embedding 코사인 유사도 ANN — v9 spec ‘자기잠식 1차 스크리닝’.

        Returns: [(ContentRecord, similarity_score 0..1), ...] 유사도 내림차순.
        """
        if not self.use_vector:
            raise NotImplementedError(
                "pgvector 가 비활성 — 자기잠식 의미 검색은 pgvector 환경에서만 가능합니다.",
            )
        if not embedding or len(embedding) != EMBEDDING_DIM:
            return []
        # cosine_distance 는 0(같음)..2(반대). similarity = 1 - distance/2 로 0..1.
        sql = (
            f"SELECT {self._CONTENT_COLS}, "
            "       1 - (summary_embedding <=> %s::vector) / 2 AS similarity "
            "FROM contents "
            "WHERE summary_embedding IS NOT NULL "
            + ("AND id <> %s " if exclude_id else "")
            + "ORDER BY summary_embedding <=> %s::vector ASC "
            + "LIMIT %s"
        )
        params: List[Any] = [embedding]
        if exclude_id:
            params.append(exclude_id)
        params += [embedding, top_k]
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            out: List[Tuple[ContentRecord, float]] = []
            for row in cur.fetchall():
                rec = self._row_to_content(row[:-1])
                sim = float(row[-1]) if row[-1] is not None else 0.0
                out.append((rec, sim))
            return out

    # ── v13 keyword_usages CRUD ────────────────────────
    _USAGE_COLS = (
        "id, keyword_id, account_id, blog_id, contents_id, "
        "status, started_at, published_at, failed_at, note"
    )

    @staticmethod
    def _row_to_usage(row: tuple) -> KeywordUsage:
        (id_, keyword_id, account_id, blog_id, contents_id,
         status, started_at, published_at, failed_at, note) = row
        return KeywordUsage(
            id=id_, keyword_id=keyword_id, account_id=account_id or "",
            blog_id=blog_id or "", contents_id=contents_id or "",
            status=status or USAGE_IN_PROGRESS, started_at=started_at,
            published_at=published_at or "", failed_at=failed_at or "",
            note=note or "",
        )

    def list_usages(self) -> List[KeywordUsage]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._USAGE_COLS} FROM keyword_usages ORDER BY started_at"
            )
            return [self._row_to_usage(r) for r in cur.fetchall()]

    def get_active_usage(self, keyword_id: str) -> Optional[KeywordUsage]:
        if not keyword_id:
            return None
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._USAGE_COLS} FROM keyword_usages "
                "WHERE keyword_id = %s AND status = %s "
                "ORDER BY started_at DESC LIMIT 1",
                (keyword_id, USAGE_IN_PROGRESS),
            )
            row = cur.fetchone()
            return self._row_to_usage(row) if row else None

    def list_usages_by_keyword(self, keyword_id: str) -> List[KeywordUsage]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._USAGE_COLS} FROM keyword_usages "
                "WHERE keyword_id = %s ORDER BY started_at",
                (keyword_id,),
            )
            return [self._row_to_usage(r) for r in cur.fetchall()]

    def upsert_usage(self, usage: KeywordUsage) -> None:
        if not usage.id:
            with self.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM keyword_usages")
                n = cur.fetchone()[0] or 0
            usage.id = f"u{n + 1:06d}"
        if not usage.started_at:
            usage.started_at = to_iso(now_utc())
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO keyword_usages ({self._USAGE_COLS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  keyword_id=EXCLUDED.keyword_id,
                  account_id=EXCLUDED.account_id,
                  blog_id=EXCLUDED.blog_id,
                  contents_id=EXCLUDED.contents_id,
                  status=EXCLUDED.status,
                  published_at=EXCLUDED.published_at,
                  failed_at=EXCLUDED.failed_at,
                  note=EXCLUDED.note
                """,
                (usage.id, usage.keyword_id, usage.account_id, usage.blog_id,
                 usage.contents_id, usage.status, usage.started_at,
                 usage.published_at, usage.failed_at, usage.note),
            )
        self.conn.commit()
