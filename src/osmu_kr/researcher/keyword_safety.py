"""keyword_safety (v13-B) — keyword embedding 기반 정책.

[ 책임 ]
  · 씨드 입력 중복 확인  (seed_duplicate_threshold = 0.93)
  · 어뷰징 쿨다운 체크   (similarity_cooldown_threshold = 0.85, days = 3)

[ 입력/출력 ]
  · 비교 대상 모두 keywords.embedding (vector(768)) 끼리.
  · 결과는 raw match list — 정책(차단/cooldown/허용) 분기는 caller 가 결정.

[ 백엔드 호환 ]
  · PostgresStorage 면 find_similar_keywords (pgvector ANN) 사용.
  · 그 외 (SQLite/CSV) 면 in-memory cosine 으로 폴백 — 풀 50개 수준이면 충분.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from ..content_generator.embedder import BaseEmbedder, build_embedder, cosine
from ..models import (
    KSTATUS_ACTIVE, KeywordPoolItem, USAGE_PUBLISHED,
    from_iso, normalize_status, now_utc, to_iso,
)
from ..storage.base import BaseStorage

log = logging.getLogger(__name__)


# ── 매치 결과 ──────────────────────────────────────────
@dataclass
class KeywordMatch:
    """하나의 유사 키워드 + 메타.

    similarity     : 0..1 코사인 유사도
    item           : KeywordPoolItem
    is_published   : 같은 blog 에서 published 됐는지 (자기잠식 신호)
    last_published : 가장 최근 published_at (없으면 빈 문자열)
    """
    similarity: float
    item: KeywordPoolItem
    is_published: bool = False
    last_published: str = ""

    @property
    def keyword_id(self) -> str:
        return self.item.keyword_id

    @property
    def keyword(self) -> str:
        return self.item.keyword


@dataclass
class CooldownConflict:
    """어뷰징 쿨다운 위반."""
    match: KeywordMatch
    days_remaining: float


# ── 핵심 클래스 ─────────────────────────────────────────
class KeywordSafety:
    """v13-B 정책 모듈."""

    def __init__(self, storage: BaseStorage,
                 *, embedder: Optional[BaseEmbedder] = None):
        self.storage = storage
        self._embedder = embedder

    @property
    def embedder(self) -> BaseEmbedder:
        if self._embedder is None:
            self._embedder = build_embedder()
        return self._embedder

    # ── 인코딩 ─────────────────────────────────────────
    def encode_text(self, text: str) -> List[float]:
        """문자열 → 768-dim 임베딩. 내부 캐시는 두지 않음 (호출 시점에 생성)."""
        if not text:
            return [0.0] * 768
        return self.embedder.encode(text) or [0.0] * 768

    def ensure_keyword_embedding(self, item: KeywordPoolItem) -> KeywordPoolItem:
        """KeywordPoolItem 에 embedding 이 없으면 자동 생성 + 저장.

        upsert_pool 호출까지는 하지 않음 — 호출자가 결정.
        """
        if item.embedding_json:
            return item
        vec = self.encode_text(item.keyword)
        item.embedding_json = json.dumps(vec, ensure_ascii=False)
        return item

    # ── 씨드 중복 확인 ─────────────────────────────────
    def find_seed_duplicates(self, seed: str,
                              *, threshold: float = 0.93,
                              top_k: int = 5,
                              blog_id: str = "") -> List[KeywordMatch]:
        """씨드 텍스트 → 유사도 ≥ threshold 인 기존 keywords 목록.

        같은 blog 에서 published 된 적이 있으면 자기잠식 신호 표시.
        """
        seed = (seed or "").strip()
        if not seed:
            return []
        seed_emb = self.encode_text(seed)
        return self._search_similar(
            seed_emb, threshold=threshold, top_k=top_k, blog_id=blog_id,
            only_status=KSTATUS_ACTIVE,
        )

    # ── 어뷰징 쿨다운 ──────────────────────────────────
    def check_abuse_cooldown(self, candidate_keyword_id: str,
                              *, threshold: float = 0.85,
                              days: float = 3.0,
                              blog_id: str = "") -> Optional[CooldownConflict]:
        """후보 키워드의 embedding 을 같은 blog 의 최근 published 키워드들과 비교.

        - 비교 대상: keyword_usages.status='published' AND blog_id 매칭 (없으면 전체)
                     의 keyword 들의 embedding.
        - 유사도 ≥ threshold 이고 발행이 days 이내면 CooldownConflict.
        - 가장 강한 위반(유사도 max) 한 건만 반환.
        """
        candidate = self.storage.get_pool(candidate_keyword_id)
        if candidate is None or not candidate.embedding_json:
            return None
        try:
            cand_vec = json.loads(candidate.embedding_json)
        except Exception:
            return None
        if not isinstance(cand_vec, list):
            return None

        cutoff = now_utc() - timedelta(days=days)
        # blog 의 최근 published 키워드 목록
        published_kids = []
        for u in self.storage.list_usages():
            if u.status != USAGE_PUBLISHED:
                continue
            if blog_id and u.blog_id != blog_id:
                continue
            try:
                pub_dt = from_iso(u.published_at) if u.published_at else None
            except Exception:
                pub_dt = None
            if pub_dt is None or pub_dt < cutoff:
                continue
            published_kids.append((u.keyword_id, u.published_at))

        worst: Optional[CooldownConflict] = None
        for kid, pub_at in published_kids:
            if kid == candidate_keyword_id:
                continue
            other = self.storage.get_pool(kid)
            if other is None or not other.embedding_json:
                continue
            try:
                other_vec = json.loads(other.embedding_json)
            except Exception:
                continue
            sim = cosine(cand_vec, other_vec)
            if sim >= threshold:
                # days_remaining = 발행 + days 까지 남은 일 수
                try:
                    pub_dt = from_iso(pub_at)
                    delta_days = max(0.0, days - (now_utc() - pub_dt).total_seconds() / 86400)
                except Exception:
                    delta_days = days
                m = KeywordMatch(similarity=sim, item=other,
                                  is_published=True, last_published=pub_at)
                conflict = CooldownConflict(match=m, days_remaining=delta_days)
                if worst is None or sim > worst.match.similarity:
                    worst = conflict
        return worst

    # ── 내부 — backend 분기 ────────────────────────────
    def _search_similar(self, query_emb: List[float],
                         *, threshold: float, top_k: int,
                         blog_id: str, only_status: str
                         ) -> List[KeywordMatch]:
        # PostgresStorage + pgvector 면 ANN, 아니면 in-memory
        results: List[KeywordMatch] = []
        backend_name = getattr(self.storage, "name", "")
        use_pg = (backend_name == "postgres" and getattr(self.storage, "use_vector", False))

        if use_pg:
            try:
                pairs = self.storage.find_similar_keywords(
                    query_emb, top_k=top_k * 2, only_status=only_status,
                )
            except Exception as e:
                log.warning("[keyword_safety] pgvector ANN 실패 → in-memory 폴백: %s", e)
                pairs = self._fallback_similar(query_emb, only_status=only_status)
        else:
            pairs = self._fallback_similar(query_emb, only_status=only_status)

        # blog 단위 published 여부 체크
        published_map = {}   # keyword_id → 가장 최근 published_at
        for u in self.storage.list_usages():
            if u.status != USAGE_PUBLISHED:
                continue
            if blog_id and u.blog_id != blog_id:
                continue
            prev = published_map.get(u.keyword_id, "")
            if not prev or u.published_at > prev:
                published_map[u.keyword_id] = u.published_at

        for item, sim in pairs:
            if sim < threshold:
                continue
            pub_at = published_map.get(item.keyword_id, "")
            results.append(KeywordMatch(
                similarity=sim, item=item,
                is_published=bool(pub_at), last_published=pub_at,
            ))
        results.sort(key=lambda m: m.similarity, reverse=True)
        return results[:top_k]

    def _fallback_similar(self, query_emb: List[float], *, only_status: str
                           ) -> List[tuple]:
        """SQLite/CSV/sheets — in-memory cosine. 풀이 작으면 충분."""
        out: List[tuple] = []
        for item in self.storage.list_pool():
            if only_status and normalize_status(item.status) != only_status:
                continue
            if not item.embedding_json:
                continue
            try:
                emb = json.loads(item.embedding_json)
            except Exception:
                continue
            if not isinstance(emb, list) or len(emb) != len(query_emb):
                continue
            sim = cosine(query_emb, emb)
            out.append((item, sim))
        out.sort(key=lambda p: p[1], reverse=True)
        return out
