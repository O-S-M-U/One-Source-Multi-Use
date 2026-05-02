"""SheetsStorage — Google Sheets 백엔드 (gspread)."""
from __future__ import annotations

from typing import List, Optional

from ..models import ContentRecord, KeywordPoolItem, ResearchHistoryRecord
from .base import BaseStorage


class SheetsStorage(BaseStorage):
    name = "sheets"

    def __init__(self, credentials_path, sheet_id=None, sheet_title=None,
                 ws_keyword_pool="keyword_pool", ws_content_db="content_db",
                 ws_history="research_history"):
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as e:
            raise RuntimeError("gspread/google-auth가 설치돼 있지 않습니다.") from e

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
        self._gc = gspread.authorize(creds)
        if sheet_id:
            self._sh = self._gc.open_by_key(sheet_id)
        elif sheet_title:
            try:
                self._sh = self._gc.open(sheet_title)
            except Exception:
                self._sh = self._gc.create(sheet_title)
        else:
            raise RuntimeError("sheet_id 또는 sheet_title 필요")

        self._ws_pool = self._ensure_ws(ws_keyword_pool, KeywordPoolItem.HEADER)
        self._ws_content = self._ensure_ws(ws_content_db, ContentRecord.HEADER)
        self._ws_history = self._ensure_ws(ws_history, ResearchHistoryRecord.HEADER)

    def _ensure_ws(self, title, header):
        try:
            ws = self._sh.worksheet(title)
        except Exception:
            ws = self._sh.add_worksheet(title=title, rows=1000, cols=max(20, len(header)))
            ws.update("A1", [header])
            return ws
        if ws.row_values(1) != header:
            ws.update("A1", [header])
        return ws

    @staticmethod
    def _rows_after_header(ws):
        values = ws.get_all_values()
        return values[1:] if values else []

    def list_pool(self):
        return [KeywordPoolItem.from_row(r) for r in self._rows_after_header(self._ws_pool)]

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
        self._ws_pool.clear()
        rows = [KeywordPoolItem.HEADER] + [it.to_row() for it in items]
        self._ws_pool.update("A1", rows)

    def list_content(self):
        return [ContentRecord.from_row(r) for r in self._rows_after_header(self._ws_content)]

    def append_content(self, record):
        self._ws_content.append_row(record.to_row(), value_input_option="USER_ENTERED")

    def replace_content(self, records):
        self._ws_content.clear()
        rows = [ContentRecord.HEADER] + [r.to_row() for r in records]
        self._ws_content.update("A1", rows)

    def append_history(self, record):
        self._ws_history.append_row(record.to_row(), value_input_option="USER_ENTERED")

    def list_history(self):
        return [ResearchHistoryRecord.from_row(r) for r in self._rows_after_header(self._ws_history)]
