"""ConfigManager (v13-D) — 런타임 임계치 조회의 단일 진입점.

[ 우선순위 ]
  1) 환경변수 (dot notation 자동 매핑 + legacy alias 도 fallback)
  2) DB config 테이블
  3) DEFAULTS 딕셔너리

[ dot notation ↔ 환경변수 매핑 ]
  · `keyword.pool_max_size` → `OSMU_KEYWORD_POOL_MAX_SIZE`
  · 점은 underscore, 알파벳은 대문자, 앞에 OSMU_ prefix.
  · 추가로 LEGACY_ENV 매핑이 있으면 그쪽도 fallback.

[ 사용 ]
  cm = ConfigManager(storage)
  pool_max = cm.get_int("keyword.pool_max_size")
  cm.set("keyword.golden_threshold", 65)   # DB 에 저장
  cm.dump()                                # 19개 항목 현재값 + 출처 dict
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from .storage.base import BaseStorage

log = logging.getLogger(__name__)


# ── DEFAULTS — v13 spec 19개 항목 ──────────────────────
DEFAULTS: Dict[str, Any] = {
    # keyword.*
    "keyword.pool_max_size":                 50,
    "keyword.pool_eviction_score_threshold": 45,
    "keyword.pool_eviction_eval_count":      3,
    "keyword.revival_days":                  30,
    "keyword.reuse_days":                    180,
    "keyword.similarity_cooldown_threshold": 0.85,
    "keyword.similarity_cooldown_days":      3,
    "keyword.recent_density_window_days":    14,
    "keyword.golden_threshold":              60,
    "keyword.seed_duplicate_threshold":      0.93,
    # collector.*
    "collector.min_blogs":                    3,
    "collector.similarity_warning_threshold": 0.75,
    # checker.*
    "checker.plagiarism_overall_threshold":   0.15,
    "checker.plagiarism_sentence_threshold":  0.35,
    "checker.min_char_count":                 1500,
    # publisher.*
    "publisher.daily_limit":                  2,
    "publisher.min_draft_minutes":            30,
    "publisher.similarity_cooldown_threshold": 0.85,
    "publisher.similarity_cooldown_days":     3,
    # housekeeping.*
    "housekeeping.inprogress_timeout_hours":  24,    # ops-5: in_progress lock 자동 해제
    # publisher 다양성 회피 (score-6 / v13 spec d)
    "publisher.diversity_window_days":        7,
    "publisher.diversity_group_size":         5,    # 최근 N편 검사
    "publisher.diversity_similarity":         0.8,  # cosine 임계
    "publisher.diversity_max_in_group":       3,    # 동일 그룹 N편 이상이면 경고
    # publisher 재시도 (score-7)
    "publisher.max_retries":                  3,
    "publisher.retry_backoff_seconds":        30,
    # Anthropic 모델 선택 (infra-5) — 코드 수정 없이 모델 전환
    "anthropic.model.interpret":              "claude-haiku-4-5-20251001",
    "anthropic.model.blueprint":              "claude-sonnet-4-6",
    "anthropic.model.writer":                 "claude-sonnet-4-6",
}

# ── legacy 환경변수 호환 — 기존 이름이 환경에 박혀있어도 인식 ─────
LEGACY_ENV: Dict[str, str] = {
    "keyword.pool_max_size":          "OSMU_POOL_MAX_SIZE",
    "keyword.revival_days":           "OSMU_REVIVAL_DAYS",
    "keyword.reuse_days":             "OSMU_SEED_COOLDOWN_DAYS",  # 의미 다르나 가장 가까움
    "keyword.golden_threshold":       "OSMU_GOLDEN_THRESHOLD",
}


def dot_to_env(key: str) -> str:
    """`keyword.pool_max_size` → `OSMU_KEYWORD_POOL_MAX_SIZE`."""
    return "OSMU_" + key.replace(".", "_").upper()


def _coerce(value: str, target_type: type):
    if value is None:
        return None
    if target_type is bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    try:
        return target_type(value)
    except (TypeError, ValueError):
        return None


# ── ConfigManager ───────────────────────────────────────
class ConfigManager:
    def __init__(self, storage: BaseStorage,
                 *, defaults: Optional[Dict[str, Any]] = None):
        self.storage = storage
        self.defaults: Dict[str, Any] = dict(defaults or DEFAULTS)
        self._cache_clear()

    def _cache_clear(self) -> None:
        self._cache: Dict[str, tuple] = {}    # key → (value, source)

    # ── 조회 ──────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        """env > DB > DEFAULTS > default. value 는 str 또는 raw default 타입."""
        if key in self._cache:
            return self._cache[key][0]
        # 1) env (dot notation)
        env_v = os.environ.get(dot_to_env(key))
        if env_v is not None:
            self._cache[key] = (env_v, "env")
            return env_v
        # 1b) legacy env
        legacy_name = LEGACY_ENV.get(key)
        if legacy_name:
            env_v = os.environ.get(legacy_name)
            if env_v is not None:
                self._cache[key] = (env_v, f"env_legacy:{legacy_name}")
                return env_v
        # 2) DB
        try:
            db_v = self.storage.get_config(key)
        except Exception as e:
            log.warning("[ConfigManager] DB 조회 실패 (%s): %s", key, e)
            db_v = None
        if db_v is not None:
            self._cache[key] = (db_v, "db")
            return db_v
        # 3) defaults
        if key in self.defaults:
            v = self.defaults[key]
            self._cache[key] = (v, "default")
            return v
        # 4) caller default
        return default

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key, default)
        c = _coerce(v, int) if isinstance(v, str) else v
        return c if c is not None else default

    def get_float(self, key: str, default: float = 0.0) -> float:
        v = self.get(key, default)
        c = _coerce(v, float) if isinstance(v, str) else v
        return float(c) if c is not None else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    def get_source(self, key: str) -> str:
        """value 의 출처 — env / env_legacy:* / db / default / unknown."""
        if key not in self._cache:
            self.get(key)   # populate
        return self._cache.get(key, (None, "unknown"))[1]

    # ── 쓰기 ──────────────────────────────────────────
    def set(self, key: str, value: Any) -> None:
        """DB config 테이블에 저장. env 가 우선이라 env 가 있으면 ‘덮어 보이지’ 않음을 주의."""
        try:
            self.storage.set_config(key, str(value))
        except Exception as e:
            log.warning("[ConfigManager] DB 쓰기 실패 (%s): %s", key, e)
        self._cache_clear()

    def reset(self, key: str) -> None:
        """DB 에서 삭제. 이후 env 또는 default 로 떨어짐."""
        try:
            self.storage.delete_config(key)
        except Exception:
            pass
        self._cache_clear()

    # ── 유틸 ──────────────────────────────────────────
    def dump(self) -> List[dict]:
        """모든 DEFAULTS 키의 현재값 + 출처."""
        out = []
        for k in self.defaults.keys():
            v = self.get(k)
            out.append({"key": k, "value": v, "source": self.get_source(k)})
        return out

    def install_defaults(self, *, overwrite: bool = False) -> int:
        """DB 에 DEFAULTS 19개 항목을 한 번에 적재 (초기 부트스트랩용).

        overwrite=False 면 이미 DB 에 있는 키는 건드리지 않음.
        Returns: 새로 설치된 항목 수.
        """
        installed = 0
        for k, v in self.defaults.items():
            try:
                existing = self.storage.get_config(k)
            except Exception:
                existing = None
            if existing is None or overwrite:
                self.storage.set_config(k, str(v))
                installed += 1
        self._cache_clear()
        return installed
