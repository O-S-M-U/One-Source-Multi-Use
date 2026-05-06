"""SqliteStorage — v9 spec 5개 테이블 SQLite 단독 백엔드.

[ 책임 ]
  · BaseStorage 인터페이스 100% 구현 — KeywordPoolItem / ContentRecord / ResearchHistoryRecord.
  · v9 spec 의 풍부 필드(title, target_reader, paragraph_blueprint, normalized_sources,
    summary_embedding, commercial_elements) 를 contents 테이블 컬럼에 그대로 저장.
  · accounts / keyword_usages 는 v9 정렬용 — v1 에서는 헬퍼만 제공, 실 사용은 추후.

[ 트랜잭션 ]
  · upsert/insert 단건은 자동 commit. replace_pool 같은 일괄 작업은 트랜잭션으로 묶음.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import List, Optional

from ..models import (
    ContentRecord, KeywordPoolItem, ResearchHistoryRecord,
    normalize_status, now_utc, to_iso,
)
from .base import BaseStorage
from .sqlite_schema import open_connection, transaction

log = logging.getLogger(__name__)


class SqliteStorage(BaseStorage):
    """SQLite 단독 백엔드 (v1)."""
    name = "sqlite"

    def __init__(self, db_path: str = "./osmu.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        # 즉시 한 번 열어 스키마 보장
        _ = self.conn

    # ── 커넥션 lazy ──────────────────────────────────────
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_connection(self.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── KeywordPoolItem ↔ row 매핑 ─────────────────────────
    @staticmethod
    def _row_to_pool(row: sqlite3.Row) -> KeywordPoolItem:
        return KeywordPoolItem(
            keyword_id=row["keyword_id"],
            seed_keyword=row["seed_keyword"],
            keyword=row["keyword"],
            search_volume=int(row["search_volume"] or 0),
            competition=row["competition"] or "낮음",
            cpc=float(row["cpc"] or 0),
            commercial_intent=float(row["commercial_intent"] or 0),
            score=float(row["score"] or 0),
            status=normalize_status(row["status"] or "candidate"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source=row["source"] or "heuristic",
            note=row["note"] or "",
            grade=row["grade"] or "",
            profile=row["profile"] or "",
            weak_points=row["weak_points"] or "",
            is_alchemy=row["is_alchemy"] or "N",
            original_keyword=row["original_keyword"] or "",
            revival_count=int(row["revival_count"] or 0),
            inprogress_locked_at=row["inprogress_locked_at"] or "",
            published_at=row["published_at"] or "",
            failed_at=row["failed_at"] or "",
            archived_at=row["archived_at"] or "",
            account_id=row["account_id"] or "",
            last_status_reason=row["last_status_reason"] or "",
        )

    # ── pool ────────────────────────────────────────────
    def list_pool(self) -> List[KeywordPoolItem]:
        cur = self.conn.execute(
            "SELECT * FROM keywords ORDER BY created_at",
        )
        return [self._row_to_pool(r) for r in cur.fetchall()]

    def get_pool(self, keyword_id: str) -> Optional[KeywordPoolItem]:
        if not keyword_id:
            return None
        cur = self.conn.execute(
            "SELECT * FROM keywords WHERE keyword_id = ?", (keyword_id,),
        )
        row = cur.fetchone()
        return self._row_to_pool(row) if row else None

    def upsert_pool(self, item: KeywordPoolItem) -> None:
        item.updated_at = to_iso(now_utc())
        if not item.created_at:
            item.created_at = item.updated_at
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO keywords (
                    keyword_id, keyword, seed_keyword, status, grade, profile,
                    weak_points, is_alchemy, original_keyword, revival_count,
                    score, search_volume, competition, cpc, commercial_intent,
                    source, note,
                    inprogress_locked_at, published_at, failed_at, archived_at,
                    account_id, last_status_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?,
                          ?, ?)
                ON CONFLICT(keyword_id) DO UPDATE SET
                    keyword=excluded.keyword,
                    seed_keyword=excluded.seed_keyword,
                    status=excluded.status,
                    grade=excluded.grade,
                    profile=excluded.profile,
                    weak_points=excluded.weak_points,
                    is_alchemy=excluded.is_alchemy,
                    original_keyword=excluded.original_keyword,
                    revival_count=excluded.revival_count,
                    score=excluded.score,
                    search_volume=excluded.search_volume,
                    competition=excluded.competition,
                    cpc=excluded.cpc,
                    commercial_intent=excluded.commercial_intent,
                    source=excluded.source,
                    note=excluded.note,
                    inprogress_locked_at=excluded.inprogress_locked_at,
                    published_at=excluded.published_at,
                    failed_at=excluded.failed_at,
                    archived_at=excluded.archived_at,
                    account_id=excluded.account_id,
                    last_status_reason=excluded.last_status_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    item.keyword_id, item.keyword, item.seed_keyword,
                    item.status, item.grade, item.profile,
                    item.weak_points, item.is_alchemy, item.original_keyword,
                    item.revival_count,
                    item.score, item.search_volume, item.competition, item.cpc,
                    item.commercial_intent,
                    item.source, item.note,
                    item.inprogress_locked_at, item.published_at,
                    item.failed_at, item.archived_at,
                    item.account_id, item.last_status_reason,
                    item.created_at, item.updated_at,
                ),
            )

    def delete_pool(self, keyword_id: str) -> bool:
        if not keyword_id:
            return False
        with transaction(self.conn):
            cur = self.conn.execute(
                "DELETE FROM keywords WHERE keyword_id = ?", (keyword_id,),
            )
            return cur.rowcount > 0

    def replace_pool(self, items: List[KeywordPoolItem]) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM keywords")
            for it in items:
                if not it.created_at:
                    it.created_at = to_iso(now_utc())
                if not it.updated_at:
                    it.updated_at = it.created_at
                self.conn.execute(
                    """
                    INSERT INTO keywords (
                        keyword_id, keyword, seed_keyword, status, grade, profile,
                        weak_points, is_alchemy, original_keyword, revival_count,
                        score, search_volume, competition, cpc, commercial_intent,
                        source, note,
                        inprogress_locked_at, published_at, failed_at, archived_at,
                        account_id, last_status_reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?,
                              ?, ?)
                    """,
                    (
                        it.keyword_id, it.keyword, it.seed_keyword,
                        it.status, it.grade, it.profile,
                        it.weak_points, it.is_alchemy, it.original_keyword,
                        it.revival_count,
                        it.score, it.search_volume, it.competition, it.cpc,
                        it.commercial_intent,
                        it.source, it.note,
                        it.inprogress_locked_at, it.published_at,
                        it.failed_at, it.archived_at,
                        it.account_id, it.last_status_reason,
                        it.created_at, it.updated_at,
                    ),
                )

    # ── ContentRecord ↔ row 매핑 ────────────────────────
    @staticmethod
    def _row_to_content(row: sqlite3.Row) -> ContentRecord:
        return ContentRecord(
            id=row["id"],
            keyword=row["keyword"],
            seed_keyword=row["seed_keyword"] or "",
            keyword_id=row["keyword_id"] or "",
            original_source=row["original_source"] or "",
            status=row["status"] or "대기중",
            title_final=row["title_final"] or "",
            platform_url=row["platform_url"] or "",
            created_at=row["created_at"],
            published_at=row["published_at"] or "",
            raw_content=row["raw_content"] or "",
            refined_post=row["refined_post"] or "",
            image_urls=row["image_urls"] or "",
            error_log=row["error_log"] or "",
            note=row["note"] or "",
            title=row["title"] or "",
            target_reader_json=row["target_reader_json"] or "",
            paragraph_blueprint_json=row["paragraph_blueprint_json"] or "",
            normalized_sources_json=row["normalized_sources_json"] or "",
            summary_embedding_json=row["summary_embedding_json"] or "",
            commercial_elements_json=row["commercial_elements_json"] or "",
            publish_attempt_count=int(row["publish_attempt_count"] or 0),
        )

    # ── content ─────────────────────────────────────────
    def list_content(self) -> List[ContentRecord]:
        cur = self.conn.execute(
            "SELECT * FROM contents ORDER BY created_at",
        )
        return [self._row_to_content(r) for r in cur.fetchall()]

    def append_content(self, record: ContentRecord) -> None:
        if not record.id:
            raise ValueError("ContentRecord.id 필수")
        if not record.created_at:
            record.created_at = to_iso(now_utc())
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO contents (
                    id, keyword, seed_keyword, keyword_id, original_source,
                    status, title, title_final, platform_url,
                    created_at, published_at, raw_content, refined_post,
                    image_urls, error_log, note,
                    target_reader_json, paragraph_blueprint_json,
                    normalized_sources_json, summary_embedding_json,
                    commercial_elements_json, publish_attempt_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id, record.keyword, record.seed_keyword,
                    record.keyword_id, record.original_source,
                    record.status, record.title, record.title_final, record.platform_url,
                    record.created_at, record.published_at,
                    record.raw_content, record.refined_post,
                    record.image_urls, record.error_log, record.note,
                    record.target_reader_json, record.paragraph_blueprint_json,
                    record.normalized_sources_json, record.summary_embedding_json,
                    record.commercial_elements_json, record.publish_attempt_count,
                ),
            )

    def replace_content(self, records: List[ContentRecord]) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM contents")
            for r in records:
                if not r.created_at:
                    r.created_at = to_iso(now_utc())
                self.conn.execute(
                    """
                    INSERT INTO contents (
                        id, keyword, seed_keyword, keyword_id, original_source,
                        status, title, title_final, platform_url,
                        created_at, published_at, raw_content, refined_post,
                        image_urls, error_log, note,
                        target_reader_json, paragraph_blueprint_json,
                        normalized_sources_json, summary_embedding_json,
                        commercial_elements_json, publish_attempt_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.id, r.keyword, r.seed_keyword,
                        r.keyword_id, r.original_source,
                        r.status, r.title, r.title_final, r.platform_url,
                        r.created_at, r.published_at,
                        r.raw_content, r.refined_post,
                        r.image_urls, r.error_log, r.note,
                        r.target_reader_json, r.paragraph_blueprint_json,
                        r.normalized_sources_json, r.summary_embedding_json,
                        r.commercial_elements_json, r.publish_attempt_count,
                    ),
                )

    def delete_content(self, content_id: str) -> bool:
        if not content_id:
            return False
        with transaction(self.conn):
            cur = self.conn.execute(
                "DELETE FROM contents WHERE id = ?", (content_id,),
            )
            return cur.rowcount > 0

    def update_content(self, content_id: str, **fields) -> bool:
        if not content_id:
            return False
        protected = {"id", "created_at"}
        # ContentRecord 필드만 허용 + 보호 필드 제거
        allowed = set(ContentRecord.HEADER) - protected
        cleaned = {k: v for k, v in fields.items() if k in allowed}
        if not cleaned:
            return False
        cols = ", ".join(f"{k} = ?" for k in cleaned)
        params = list(cleaned.values()) + [content_id]
        with transaction(self.conn):
            cur = self.conn.execute(
                f"UPDATE contents SET {cols} WHERE id = ?", params,
            )
            return cur.rowcount > 0

    # ── research_history ────────────────────────────────
    def append_history(self, record: ResearchHistoryRecord) -> None:
        if not record.created_at:
            record.created_at = to_iso(now_utc())
        with transaction(self.conn):
            self.conn.execute(
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.keyword, record.grade, record.total_score, record.profile,
                    record.datalab_score, record.datalab_direction,
                    record.blog_results, record.blog_competition,
                    record.commercial_hits, record.gtrends_score,
                    record.weak_points, record.is_alchemy, record.original_keyword,
                    record.seed_keyword, record.evaluator,
                    "", "", "", "not_selected", record.created_at,
                ),
            )

    def list_history(self) -> List[ResearchHistoryRecord]:
        cur = self.conn.execute(
            "SELECT * FROM keyword_evaluations ORDER BY created_at",
        )
        out: List[ResearchHistoryRecord] = []
        for r in cur.fetchall():
            out.append(ResearchHistoryRecord(
                keyword=r["keyword"],
                grade=r["grade"] or "",
                total_score=float(r["total_score"] or 0),
                profile=r["profile"] or "일반",
                datalab_score=float(r["datalab_score"] or 0),
                datalab_direction=r["datalab_direction"] or "",
                blog_results=r["blog_results"] or "",
                blog_competition=r["blog_competition"] or "",
                commercial_hits=r["commercial_hits"] or "",
                gtrends_score=float(r["gtrends_score"] or 0),
                weak_points=r["weak_points"] or "",
                is_alchemy=r["is_alchemy"] or "N",
                original_keyword=r["original_keyword"] or "",
                seed_keyword=r["seed_keyword"] or "",
                evaluator=r["evaluator"] or "",
                created_at=r["created_at"],
            ))
        return out

    # ── 향후 확장용 헬퍼 (v1 에선 사용 안 함) ───────────
    def record_usage(self, keyword: str, *, seed_keyword: str = "",
                      content_id: str = "", note: str = "") -> None:
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO keyword_usages
                  (keyword, seed_keyword, used_at, content_id, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (keyword, seed_keyword, to_iso(now_utc()), content_id, note),
            )
