"""KeywordResearcher 싱글턴 + 동기화 헬퍼."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st
from osmu_kr import Config, KeywordResearcher


@st.cache_resource(show_spinner=False)
def _build_researcher() -> KeywordResearcher:
    return KeywordResearcher(Config())


def get_researcher() -> KeywordResearcher:
    return _build_researcher()


def reload_researcher() -> KeywordResearcher:
    _build_researcher.clear()
    return _build_researcher()


def settings_snapshot() -> dict:
    cfg = get_researcher().cfg
    return {
        "storage_backend": cfg.resolved_backend(),
        "local_format": cfg.local_format,
        "local_xlsx_filename": cfg.local_xlsx_filename,
        "evaluator": cfg.evaluator,
        "POOL_MAX_SIZE": cfg.pool_max_size,
        "REVIVAL_DAYS": cfg.revival_days,
        "SEED_COOLDOWN_DAYS": cfg.seed_cooldown_days,
        "GOLDEN_THRESHOLD": cfg.golden_threshold,
        "MEDIUM_LOWER": cfg.medium_lower,
        "MEDIUM_UPPER": cfg.medium_upper,
        "sheet_id": cfg.sheet_id or "",
        "sheet_title": cfg.sheet_title,
        "credentials": cfg.google_credentials or "",
        "has_credentials": cfg.has_google_credentials,
        "data_dir": cfg.local_data_dir,
    }


def get_local_xlsx_path() -> str | None:
    cfg = get_researcher().cfg
    p = os.path.join(cfg.local_data_dir, cfg.local_xlsx_filename)
    return p if os.path.isfile(p) else None


def apply_settings(form_values: dict) -> None:
    mapping = {
        "storage_backend": "OSMU_STORAGE_BACKEND",
        "local_format": "OSMU_LOCAL_FORMAT",
        "local_xlsx_filename": "OSMU_LOCAL_XLSX",
        "evaluator": "OSMU_EVALUATOR",
        "POOL_MAX_SIZE": "OSMU_POOL_MAX_SIZE",
        "REVIVAL_DAYS": "OSMU_REVIVAL_DAYS",
        "SEED_COOLDOWN_DAYS": "OSMU_SEED_COOLDOWN_DAYS",
        "GOLDEN_THRESHOLD": "OSMU_GOLDEN_THRESHOLD",
        "MEDIUM_LOWER": "OSMU_MEDIUM_LOWER",
        "MEDIUM_UPPER": "OSMU_MEDIUM_UPPER",
        "sheet_id": "OSMU_SHEET_ID",
        "sheet_title": "OSMU_SHEET_TITLE",
        "credentials": "GOOGLE_APPLICATION_CREDENTIALS",
        "data_dir": "OSMU_LOCAL_DATA_DIR",
    }
    for k, env in mapping.items():
        if k in form_values:
            v = form_values[k]
            if v is None or v == "":
                os.environ.pop(env, None)
            else:
                os.environ[env] = str(v)
    reload_researcher()


def is_mirror_backend() -> bool:
    return get_researcher().storage.name == "mirror"


def sync_status() -> dict | None:
    rs = get_researcher()
    if rs.storage.name != "mirror":
        return None
    return rs.storage.status().to_dict()


def pull_from_sheets() -> dict:
    rs = get_researcher()
    if rs.storage.name != "mirror":
        return {"ok": False, "reason": "이 백엔드는 동기화를 지원하지 않습니다."}
    return rs.storage.pull_from_sheets()


def push_to_sheets() -> dict:
    rs = get_researcher()
    if rs.storage.name != "mirror":
        return {"ok": False, "reason": "이 백엔드는 동기화를 지원하지 않습니다."}
    return rs.storage.push_to_sheets()


def get_pool_dataframe():
    rs = get_researcher()
    rows = []
    for it in rs.storage.list_pool():
        rows.append({
            "keyword_id": it.keyword_id, "seed_keyword": it.seed_keyword,
            "keyword": it.keyword, "score": it.score,
            "search_volume": it.search_volume, "competition": it.competition,
            "cpc": it.cpc, "commercial_intent": round(it.commercial_intent, 2),
            "status": it.status, "source": it.source,
            "updated_at": it.updated_at, "note": it.note,
        })
    try:
        import pandas as pd
        return pd.DataFrame(rows)
    except ImportError:
        return rows


def get_content_dataframe():
    rs = get_researcher()
    rows = []
    for r in rs.storage.list_content():
        rows.append({
            "id": r.id, "keyword": r.keyword, "seed_keyword": r.seed_keyword,
            "keyword_id": r.keyword_id, "status": r.status,
            "title_final": r.title_final, "created_at": r.created_at,
            "platform_url": r.platform_url,
        })
    try:
        import pandas as pd
        return pd.DataFrame(rows)
    except ImportError:
        return rows
