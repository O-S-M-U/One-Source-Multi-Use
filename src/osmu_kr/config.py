"""환경 변수 기반 런타임 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _s(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw not in (None, "") else default


@dataclass
class Config:
    storage_backend: str = field(default_factory=lambda: _s("OSMU_STORAGE_BACKEND", "auto"))
    google_credentials: Optional[str] = field(
        default_factory=lambda: os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )
    sheet_id: Optional[str] = field(default_factory=lambda: os.getenv("OSMU_SHEET_ID"))
    sheet_title: str = field(default_factory=lambda: _s("OSMU_SHEET_TITLE", "OSMU_content_db"))
    ws_keyword_pool: str = field(default_factory=lambda: _s("OSMU_WS_KEYWORD_POOL", "keyword_pool"))
    ws_content_db: str = field(default_factory=lambda: _s("OSMU_WS_CONTENT_DB", "content_db"))
    local_data_dir: str = field(default_factory=lambda: _s("OSMU_LOCAL_DATA_DIR", "./data"))
    local_format: str = field(default_factory=lambda: _s("OSMU_LOCAL_FORMAT", "xlsx"))
    local_xlsx_filename: str = field(default_factory=lambda: _s("OSMU_LOCAL_XLSX", "osmu_workbook.xlsx"))

    evaluator: str = field(default_factory=lambda: _s("OSMU_EVALUATOR", "heuristic"))

    pool_max_size: int = field(default_factory=lambda: _i("OSMU_POOL_MAX_SIZE", 200))
    revival_days: float = field(default_factory=lambda: _f("OSMU_REVIVAL_DAYS", 30.0))
    seed_cooldown_days: float = field(default_factory=lambda: _f("OSMU_SEED_COOLDOWN_DAYS", 7.0))
    golden_threshold: float = field(default_factory=lambda: _f("OSMU_GOLDEN_THRESHOLD", 70.0))
    medium_lower: float = field(default_factory=lambda: _f("OSMU_MEDIUM_LOWER", 40.0))
    medium_upper: float = field(default_factory=lambda: _f("OSMU_MEDIUM_UPPER", 70.0))
    avoid_consecutive_topic: bool = True

    @property
    def has_google_credentials(self) -> bool:
        return bool(self.google_credentials and os.path.isfile(self.google_credentials))

    def resolved_backend(self) -> str:
        if self.storage_backend == "auto":
            return "mirror" if (self.has_google_credentials and self.sheet_id) else "local"
        return self.storage_backend

    def summary(self) -> str:
        return (
            f"backend={self.resolved_backend()} format={self.local_format} "
            f"evaluator={self.evaluator} POOL_MAX_SIZE={self.pool_max_size} "
            f"REVIVAL_DAYS={self.revival_days} SEED_COOLDOWN={self.seed_cooldown_days} "
            f"GOLDEN={self.golden_threshold} MEDIUM=[{self.medium_lower},{self.medium_upper}]"
        )
