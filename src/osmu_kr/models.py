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

# ── v13 키워드 상태 모델 (단순화) ────────────────────────
# v13 spec: keywords.status 는 active/archived 2단계.
# 작업 lifecycle (in_progress/published/failed) 은 keyword_usages 테이블이 책임짐.
KSTATUS_ACTIVE = "active"
KSTATUS_ARCHIVED = "archived"

NEW_STATUS_SET = {KSTATUS_ACTIVE, KSTATUS_ARCHIVED}

# ── 7-A 호환 알리어스 (deprecated, 점진 제거) ────────────
# 외부 코드/테스트에서 import 하던 이름들 — 모두 active 또는 archived 로 매핑.
KSTATUS_CANDIDATE = KSTATUS_ACTIVE
KSTATUS_INPROGRESS = KSTATUS_ACTIVE       # ★ v13: lifecycle 은 keyword_usages 로
KSTATUS_PUBLISHED = KSTATUS_ACTIVE        # ★ v13: 발행 사실은 keyword_usages.status
KSTATUS_FAILED = KSTATUS_ACTIVE           # ★ v13: 실패도 keyword_usages.status

# ── 마이그레이션 매핑 — legacy(v8 이전) + 7-A → v13 ──────
LEGACY_STATUS_MAP = {
    STATUS_GOLDEN:     KSTATUS_ACTIVE,
    STATUS_MEDIUM:     KSTATUS_ACTIVE,
    STATUS_USED:       KSTATUS_ACTIVE,    # v13: 발행됐어도 keyword 자체는 active
    STATUS_REJECTED:   KSTATUS_ARCHIVED,
    STATUS_EXPIRED:    KSTATUS_ARCHIVED,
    STATUS_DEPRECATED: KSTATUS_ARCHIVED,
    STATUS_REVIVING:   KSTATUS_ACTIVE,
    # 7-A 시기에 만들었던 5단계 → 단순화
    "candidate":  KSTATUS_ACTIVE,
    "inprogress": KSTATUS_ACTIVE,
    "published":  KSTATUS_ACTIVE,
    "failed":     KSTATUS_ACTIVE,
    "archived":   KSTATUS_ARCHIVED,
}


def normalize_status(status: str) -> str:
    """status 문자열 → 항상 v13 새 enum (active/archived)."""
    s = (status or "").strip().lower()
    if s in NEW_STATUS_SET:
        return s
    if s in LEGACY_STATUS_MAP:
        return LEGACY_STATUS_MAP[s]
    return KSTATUS_ACTIVE


# ── v13 keywords.status 허용 전이 ────────────────────────
ALLOWED_TRANSITIONS = {
    KSTATUS_ACTIVE:   {KSTATUS_ARCHIVED},
    KSTATUS_ARCHIVED: set(),    # 영구 제외 — 전이 없음
}


# ── v13 keyword_usages.status (작업 lifecycle) ──────────
USAGE_IN_PROGRESS = "in_progress"
USAGE_PUBLISHED = "published"
USAGE_FAILED = "failed"

USAGE_STATUS_SET = {USAGE_IN_PROGRESS, USAGE_PUBLISHED, USAGE_FAILED}

# in_progress → (published | failed)  — 한 번 결정되면 다시 못 돌아감
USAGE_ALLOWED_TRANSITIONS = {
    USAGE_IN_PROGRESS: {USAGE_PUBLISHED, USAGE_FAILED},
    USAGE_PUBLISHED:   set(),
    USAGE_FAILED:      set(),
}

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

    # ── 7단계-A 신규 — 안전장치 lifecycle ──
    # v13: 작업 lifecycle 은 keyword_usages 가 책임. 아래 컬럼은 호환·감사용.
    inprogress_locked_at: str = ""   # (legacy) — v13 에선 keyword_usages 가 사용
    published_at: str = ""           # (legacy) — v13 에선 keyword_usages.published_at
    failed_at: str = ""              # (legacy)
    archived_at: str = ""            # → archived 전이 시각 (v13 active/archived 에서 유효)
    account_id: str = ""             # 멀티 계정 v2 — v1 에선 빈 문자열 = '본인'
    last_status_reason: str = ""     # 최근 status 전이 사유 (디버깅·감사용)
    # ── v13-B 신규 — 키워드 임베딩 (씨드 중복 + 어뷰징 쿨다운 비교용) ──
    # 768-dim List[float] 의 JSON 직렬화 텍스트.
    # PostgreSQL 백엔드는 vector(768) 컬럼에 저장, SQLite/CSV 는 그대로 JSON.
    embedding_json: str = ""
    last_evaluated_at: str = ""      # housekeeping(re-evaluation) 트리거 기준

    HEADER = [
        "keyword_id", "seed_keyword", "keyword",
        "search_volume", "competition", "cpc",
        "commercial_intent", "score", "status",
        "created_at", "updated_at", "source", "note",
        # 풍부화 필드 — 기존 데이터 후방호환을 위해 끝에 추가
        "grade", "profile", "weak_points",
        "is_alchemy", "original_keyword", "revival_count",
        # 7단계-A 신규
        "inprogress_locked_at", "published_at", "failed_at", "archived_at",
        "account_id", "last_status_reason",
        # v13-B 신규
        "embedding_json", "last_evaluated_at",
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
            status=normalize_status(str(d["status"] or KSTATUS_CANDIDATE)),
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
            inprogress_locked_at=str(d.get("inprogress_locked_at") or ""),
            published_at=str(d.get("published_at") or ""),
            failed_at=str(d.get("failed_at") or ""),
            archived_at=str(d.get("archived_at") or ""),
            account_id=str(d.get("account_id") or ""),
            last_status_reason=str(d.get("last_status_reason") or ""),
            embedding_json=str(d.get("embedding_json") or ""),
            last_evaluated_at=str(d.get("last_evaluated_at") or ""),
        )

    def fill_grade(self) -> None:
        """score → grade 자동 채우기."""
        if not self.grade:
            self.grade = grade_from_score(self.score)


@dataclass
class KeywordUsage:
    """v13 keyword_usages — 블로그별 키워드 사용 이력 (작업 lifecycle).

    필드:
      · id           : 자동 생성 (또는 storage 가 부여)
      · keyword_id   : keywords 참조
      · account_id   : 사용자 계정 (v1: 단일계정이면 빈 값 가능)
      · blog_id      : 블로그 식별자 (자기잠식 체크 단위)
      · contents_id  : contents 테이블 참조 (Phase 1 후 채움)
      · status       : in_progress / published / failed
      · started_at   : in_progress 시작 시각 (lock 기준)
      · published_at : 발행 시각 (180일 재사용 카운트 기준; failed 면 빈 문자열)
      · failed_at    : 실패 시각 (감사용)
      · note         : 진행 메모 / 실패 사유 등
    """
    id: str = ""
    keyword_id: str = ""
    account_id: str = ""
    blog_id: str = ""
    contents_id: str = ""
    status: str = USAGE_IN_PROGRESS
    started_at: str = field(default_factory=lambda: to_iso(now_utc()))
    published_at: str = ""
    failed_at: str = ""
    note: str = ""

    HEADER = [
        "id", "keyword_id", "account_id", "blog_id", "contents_id",
        "status", "started_at", "published_at", "failed_at", "note",
    ]

    def to_row(self) -> list:
        d = asdict(self)
        return [d[k] for k in self.HEADER]

    @classmethod
    def from_row(cls, row: list) -> "KeywordUsage":
        padded = list(row) + [""] * (len(cls.HEADER) - len(row))
        d = dict(zip(cls.HEADER, padded))
        return cls(**{k: str(v) if v is not None else "" for k, v in d.items()})

    def is_active_lock(self) -> bool:
        """in_progress 상태에서 잠금 활성 여부."""
        return self.status == USAGE_IN_PROGRESS


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
    # ── v9 spec 풍부 필드 (5단계 신규) ──
    # Phase 1·2 산출물 — 모두 JSON 직렬화 텍스트로 저장 (csv/xlsx/sheets 호환)
    title: str = ""                         # v9: h1 제목 (title_final 과 분리)
    target_reader_json: str = ""            # TargetReader JSON
    paragraph_blueprint_json: str = ""      # List[ParagraphBlock] JSON
    normalized_sources_json: str = ""       # Dict[section_index → List[FactItem]] JSON
    summary_embedding_json: str = ""        # List[float] JSON (768-dim)
    commercial_elements_json: str = ""      # CommercialElements JSON
    publish_attempt_count: int = 0

    HEADER = [
        "id", "keyword", "seed_keyword", "keyword_id", "original_source",
        "status", "title_final", "platform_url",
        "created_at", "published_at", "raw_content", "refined_post",
        "image_urls", "error_log", "note",
        # v9 풍부 필드 — 기존 csv/xlsx 후방호환 위해 끝에 추가
        "title",
        "target_reader_json", "paragraph_blueprint_json",
        "normalized_sources_json", "summary_embedding_json",
        "commercial_elements_json", "publish_attempt_count",
    ]

    def to_row(self) -> list:
        d = asdict(self)
        return [d[k] for k in self.HEADER]

    @classmethod
    def from_row(cls, row: list) -> "ContentRecord":
        padded = list(row) + [""] * (len(cls.HEADER) - len(row))
        d = dict(zip(cls.HEADER, padded))
        # publish_attempt_count 만 int 로 변환, 나머지는 str
        out = {}
        for k, v in d.items():
            if k == "publish_attempt_count":
                try:
                    out[k] = int(float(v or 0))
                except (TypeError, ValueError):
                    out[k] = 0
            else:
                out[k] = str(v) if v is not None else ""
        return cls(**out)
