"""LocalCsvStorage — 로컬 CSV 백엔드."""
from __future__ import annotations

import csv
import os
from typing import List, Optional

from ..models import ContentRecord, KeywordPoolItem, ResearchHistoryRecord
from .base import BaseStorage


class LocalCsvStorage(BaseStorage):
    name = "local"

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.pool_path = os.path.join(self.data_dir, "keyword_pool.csv")
        self.content_path = os.path.join(self.data_dir, "content_db.csv")
        self.history_path = os.path.join(self.data_dir, "research_history.csv")
        self._ensure_header(self.pool_path, KeywordPoolItem.HEADER)
        self._ensure_header(self.content_path, ContentRecord.HEADER)
        self._ensure_header(self.history_path, ResearchHistoryRecord.HEADER)

    @staticmethod
    def _ensure_header(path: str, header: list) -> None:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)

    @staticmethod
    def _read_rows(path: str) -> List[list]:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        return rows[1:] if rows else []

    @staticmethod
    def _write_all(path: str, header: list, rows: List[list]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)

    def list_pool(self):
        return [KeywordPoolItem.from_row(r) for r in self._read_rows(self.pool_path)]

    def get_pool(self, keyword_id):
        for it in self.list_pool():
            if it.keyword_id == keyword_id:
                return it
        return None

    def upsert_pool(self, item):
        items = self.list_pool()
        for i, it in enumerate(items):
            if it.keyword_id == item.keyword_id:
                items[i] = item
                break
        else:
            items.append(item)
        self.replace_pool(items)

    def delete_pool(self, keyword_id):
        items = self.list_pool()
        new_items = [it for it in items if it.keyword_id != keyword_id]
        if len(new_items) == len(items):
            return False
        self.replace_pool(new_items)
        return True

    def replace_pool(self, items):
        self._write_all(self.pool_path, KeywordPoolItem.HEADER, [it.to_row() for it in items])

    def list_content(self):
        return [ContentRecord.from_row(r) for r in self._read_rows(self.content_path)]

    def append_content(self, record):
        with open(self.content_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(record.to_row())

    def replace_content(self, records):
        self._write_all(self.content_path, ContentRecord.HEADER, [r.to_row() for r in records])

    # ── research_history ──
    def append_history(self, record):
        with open(self.history_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(record.to_row())

    def list_history(self):
        return [ResearchHistoryRecord.from_row(r) for r in self._read_rows(self.history_path)]
