"""데이터 모델."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

ISO = "%Y-%m-%dT%H:%M:%S%z"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime(ISO)


def from_iso(s: str) -> datetime:
    if not s:
        return now_utc()
    try:
        return datetime.strptime(s, ISO)
    except ValueError:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


STATUS_GOLDEN = "golden"
STATUS_MEDIUM = "medium"
STATUS_REJECTED = "rejected"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"
STATUS_DEPRECATED = "deprecated"   # 부활 심사 미달
STATUS_REVIVING = "reviving"       # 재평가 진행 중

GRADE_GOLDEN = "황금"
GRADE_GOOD = "좋은"
GRADE_MEDIUM = "보통"
GRADE_FAIL = "미달"
GRADE_ORDER = {GRADE_GOLDEN: 4, GRADE_GOOD: 3, GRADE_MEDIUM: 2, GRADE_FAIL: 1}


def grade_from_score(score: float) -> str:
    if score >= 80:
        return GRADE_GOLDEN
    if score >= 60:
        return GRADE_GOOD
    if score >= 40:
        return GRADE_MEDIUM
    return GRADE_FAIL


@dataclass
class Evaluation:
    search_volume: int = 0
    competition: str = "낮음"
    cpc: float = 0.0
    commercial_intent: float = 0.0
    score: float = 0.0
    raw: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "search_volume": self.search_volume,
            "competition": self.competition,
            "cpc": self.cpc,
            "commercial_intent": round(self.commercial_intent, 3),
            "score": round(self.score, 2),
        }


@dataclass
class KeywordPoolItem:
    keyword_id: str
    seed_keyword: str
    keyword: str
    search_volume: int = 0
    competition: str = "낮음"
    cpc: float = 0.0
    commercial_intent: float = 0.0
    score: float = 0.0
    status: str = STATUS_GOLDEN
    created_at: str = field(default_factory=lambda: to_iso(now_utc()))
    updated_at: str = field(default_factory=lambda: to_iso(now_utc()))
    source: str = "heuristic"
    note: str = ""
    # ── golden_keyword.py 호환 풍부화 필드 ──
    grade: str = ""                 # 황금 / 좋은 / 보통 / 미달
    profile: str = ""               # 일반 / 롱테일
    weak_points: str = ""           # 상업의도부족, 경쟁도높음, 트렌드낮음 (CSV)
    is_alchemy: str = "N"           # Y/N
    original_keyword: str = ""      # 알케미 변형의 원본 키워드
    revival_count: int = 0          # 부활 심사 통과 횟수

    HEADER = [
        "keyword_id", "seed_keyword", "keyword",
        "search_volume", "competition", "cpc",
        "commercial_intent", "score", "status",
        "created_at", "updated_at", "source", "note",
        # 풍부화 필드 — 기존 데이터 후방호환을 위해 끝에 추가
        "grade", "profile", "weak_points",
        "is_alchemy", "original_keyword", "revival_count",
    ]

    def to_row(self) -> list:
        d = asdict(self)
        return [d[k] for k in self.HEADER]

    @classmethod
    def from_row(cls, row: list) -> "KeywordPoolItem":
        padded = list(row) + [""] * (len(cls.HEADER) - len(row))
        d = dict(zip(cls.HEADER, padded))
        return cls(
            keyword_id=str(d["keyword_id"]),
            seed_keyword=str(d["seed_keyword"]),
            keyword=str(d["keyword"]),
            search_volume=int(float(d["search_volume"] or 0)),
            competition=str(d["competition"] or "낮음"),
            cpc=float(d["cpc"] or 0),
            commercial_intent=float(d["commercial_intent"] or 0),
            score=float(d["score"] or 0),
            status=str(d["status"] or STATUS_GOLDEN),
            created_at=str(d["created_at"] or to_iso(now_utc())),
            updated_at=str(d["updated_at"] or to_iso(now_utc())),
            source=str(d["source"] or "heuristic"),
            note=str(d["note"] or ""),
            grade=str(d.get("grade") or ""),
            profile=str(d.get("profile") or ""),
            weak_points=str(d.get("weak_points") or ""),
            is_alchemy=str(d.get("is_alchemy") or "N"),
            original_keyword=str(d.get("original_keyword") or ""),
            revival_count=int(float(d.get("revival_count") or 0)),
        )

    def fill_grade(self) -> None:
        """score → grade 자동 채우기."""
        if not self.grade:
            self.grade = grade_from_score(self.score)


@dataclass
class ResearchHistoryRecord:
    """분석 시점별 스냅샷 — keyword_research 별도 시트.

    골든키워드 분석기 호환: 같은 키워드를 여러 번 분석한 이력을
    누적하여 트렌드 변화 추적 가능.
    """
    keyword: str
    grade: str = ""
    total_score: float = 0.0
    profile: str = "일반"
    datalab_score: float = 0.0
    datalab_direction: str = ""
    blog_results: str = ""
    blog_competition: str = ""
    commercial_hits: str = ""
    gtrends_score: float = 0.0
    weak_points: str = ""
    is_alchemy: str = "N"
    original_keyword: str = ""
    seed_keyword: str = ""
    evaluator: str = ""
    created_at: str = field(default_factory=lambda: to_iso(now_utc()))

    HEADER = [
        "keyword", "grade", "total_score", "profile",
        "datalab_score", "datalab_direction",
        "blog_results", "blog_competition",
        "commercial_hits", "gtrends_score",
        "weak_points", "is_alchemy", "original_keyword",
        "seed_keyword", "evaluator", "created_at",
    ]

    def to_row(self) -> list:
        d = asdict(self)
        return [d[k] for k in self.HEADER]

    @classmethod
    def from_row(cls, row: list) -> "ResearchHistoryRecord":
        padded = list(row) + [""] * (len(cls.HEADER) - len(row))
        d = dict(zip(cls.HEADER, padded))
        return cls(
            keyword=str(d["keyword"]),
            grade=str(d["grade"] or ""),
            total_score=float(d["total_score"] or 0),
            profile=str(d["profile"] or "일반"),
            datalab_score=float(d["datalab_score"] or 0),
            datalab_direction=str(d["datalab_direction"] or ""),
            blog_results=str(d["blog_results"] or ""),
            blog_competition=str(d["blog_competition"] or ""),
            commercial_hits=str(d["commercial_hits"] or ""),
            gtrends_score=float(d["gtrends_score"] or 0),
            weak_points=str(d["weak_points"] or ""),
            is_alchemy=str(d["is_alchemy"] or "N"),
            original_keyword=str(d["original_keyword"] or ""),
            seed_keyword=str(d["seed_keyword"] or ""),
            evaluator=str(d["evaluator"] or ""),
            created_at=str(d["created_at"] or to_iso(now_utc())),
        )


@dataclass
class ContentRecord:
    id: str
    keyword: str
    seed_keyword: str = ""
    keyword_id: str = ""
    original_source: str = ""
    status: str = "대기중"
    title_final: str = ""
    platform_url: str = ""
    created_at: str = field(default_factory=lambda: to_iso(now_utc()))
    published_at: str = ""
    raw_content: str = ""
    refined_post: str = ""
    image_urls: str = ""
    error_log: str = ""
    note: str = ""

    HEADER = [
        "id", "keyword", "seed_keyword", "keyword_id", "original_source",
        "status", "title_final", "platform_url",
        "created_at", "published_at", "raw_content", "refined_post",
        "image_urls", "error_log", "note",
    ]

    def to_row(self) -> list:
        d = asdict(self)
        return [d[k] for k in self.HEADER]

    @classmethod
    def from_row(cls, row: list) -> "ContentRecord":
        padded = list(row) + [""] * (len(cls.HEADER) - len(row))
        d = dict(zip(cls.HEADER, padded))
        return cls(**{k: str(v) if v is not None else "" for k, v in d.items()})
