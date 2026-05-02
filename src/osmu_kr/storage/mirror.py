"""MirrorStorage — Local + Google Sheets 양방향 동기화."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from ..models import ContentRecord, KeywordPoolItem
from .base import BaseStorage

log = logging.getLogger(__name__)


@dataclass
class SyncStatus:
    sheets_enabled: bool
    last_pull_at: Optional[str] = None
    last_push_at: Optional[str] = None
    last_error: Optional[str] = None
    pending_writes: int = 0
    sheet_url: Optional[str] = None

    def to_dict(self):
        return {
            "sheets_enabled": self.sheets_enabled,
            "last_pull_at": self.last_pull_at,
            "last_push_at": self.last_push_at,
            "last_error": self.last_error,
            "pending_writes": self.pending_writes,
            "sheet_url": self.sheet_url,
        }


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


class MirrorStorage(BaseStorage):
    name = "mirror"

    def __init__(self, local: BaseStorage, sheets_factory=None, meta_path=None):
        self.local = local
        self._sheets_factory = sheets_factory
        self._sheets: Optional[BaseStorage] = None
        self._sheets_init_attempted = False
        data_dir = getattr(local, "data_dir", "./data")
        self.meta_path = meta_path or os.path.join(data_dir, "_sync_meta.json")
        self._status = self._load_status()

    def _load_status(self) -> SyncStatus:
        if os.path.isfile(self.meta_path):
            try:
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return SyncStatus(**{**SyncStatus(False).to_dict(), **d})
            except Exception:
                pass
        return SyncStatus(sheets_enabled=False)

    def _save_status(self):
        try:
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump(self._status.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def status(self):
        return self._status

    def _get_sheets(self):
        if self._sheets is not None:
            return self._sheets
        if self._sheets_init_attempted:
            return None
        self._sheets_init_attempted = True
        if not self._sheets_factory:
            self._status.sheets_enabled = False
            return None
        try:
            self._sheets = self._sheets_factory()
            self._status.sheets_enabled = True
            sh = getattr(self._sheets, "_sh", None)
            if sh is not None:
                url = getattr(sh, "url", None)
                if url:
                    self._status.sheet_url = url
            self._save_status()
        except Exception as e:
            self._status.sheets_enabled = False
            self._status.last_error = f"sheets init failed: {e}"
            self._save_status()
            log.warning("[mirror] Sheets 초기화 실패 → 로컬 단독 동작: %s", e)
        return self._sheets

    def _safe_sheets_call(self, fn_name, *a, **kw):
        sh = self._get_sheets()
        if sh is None:
            self._status.pending_writes += 1
            self._save_status()
            return False
        try:
            getattr(sh, fn_name)(*a, **kw)
            self._status.last_push_at = _now()
            self._status.last_error = None
            self._save_status()
            return True
        except Exception as e:
            self._status.last_error = f"{fn_name} 실패: {e}"
            self._status.pending_writes += 1
            self._save_status()
            return False

    def list_pool(self): return self.local.list_pool()
    def get_pool(self, kid): return self.local.get_pool(kid)

    def upsert_pool(self, item):
        self.local.upsert_pool(item)
        self._safe_sheets_call("upsert_pool", item)

    def delete_pool(self, keyword_id):
        ok = self.local.delete_pool(keyword_id)
        self._safe_sheets_call("delete_pool", keyword_id)
        return ok

    def replace_pool(self, items):
        self.local.replace_pool(items)
        self._safe_sheets_call("replace_pool", items)

    def list_content(self): return self.local.list_content()

    def append_content(self, record):
        self.local.append_content(record)
        self._safe_sheets_call("append_content", record)

    def replace_content(self, records):
        self.local.replace_content(records)
        self._safe_sheets_call("replace_content", records)

    def pull_from_sheets(self):
        sh = self._get_sheets()
        if sh is None:
            return {"ok": False, "reason": "sheets_unavailable"}
        try:
            t0 = time.time()
            pool = sh.list_pool()
            content = sh.list_content()
            self.local.replace_pool(pool)
            self.local.replace_content(content)
            dt = time.time() - t0
            self._status.last_pull_at = _now()
            self._status.last_error = None
            self._save_status()
            return {"ok": True, "pool_count": len(pool),
                    "content_count": len(content), "elapsed": round(dt, 2)}
        except Exception as e:
            self._status.last_error = f"pull 실패: {e}"
            self._save_status()
            return {"ok": False, "reason": str(e)}

    def push_to_sheets(self):
        sh = self._get_sheets()
        if sh is None:
            return {"ok": False, "reason": "sheets_unavailable"}
        try:
            t0 = time.time()
            pool = self.local.list_pool()
            content = self.local.list_content()
            sh.replace_pool(pool)
            sh.replace_content(content)
            dt = time.time() - t0
            self._status.last_push_at = _now()
            self._status.last_error = None
            self._status.pending_writes = 0
            self._save_status()
            return {"ok": True, "pool_count": len(pool),
                    "content_count": len(content), "elapsed": round(dt, 2)}
        except Exception as e:
            self._status.last_error = f"push 실패: {e}"
            self._save_status()
            return {"ok": False, "reason": str(e)}
