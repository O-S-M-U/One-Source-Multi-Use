"""스토리지 팩토리."""
from __future__ import annotations

import logging

from ..config import Config
from .base import BaseStorage
from .csv_local import LocalCsvStorage

log = logging.getLogger(__name__)


def _try_build_sheets(cfg: Config):
    try:
        from .sheets import SheetsStorage
        if not cfg.google_credentials:
            return None
        return SheetsStorage(
            credentials_path=cfg.google_credentials,
            sheet_id=cfg.sheet_id,
            sheet_title=cfg.sheet_title,
            ws_keyword_pool=cfg.ws_keyword_pool,
            ws_content_db=cfg.ws_content_db,
        )
    except Exception as e:
        log.warning("[factory] Sheets 초기화 실패: %s", e)
        return None


def _build_local(cfg: Config) -> BaseStorage:
    fmt = (cfg.local_format or "xlsx").lower()
    if fmt == "xlsx":
        try:
            from .xlsx_local import LocalXlsxStorage
            return LocalXlsxStorage(data_dir=cfg.local_data_dir,
                                     filename=cfg.local_xlsx_filename)
        except ImportError as e:
            log.warning("[factory] openpyxl 미설치 → CSV 폴백: %s", e)
    return LocalCsvStorage(data_dir=cfg.local_data_dir)


def build_storage(cfg: Config) -> BaseStorage:
    backend = cfg.resolved_backend()

    if backend == "auto":
        backend = "mirror" if (cfg.has_google_credentials and cfg.sheet_id) else "local"

    if backend == "sheets":
        sh = _try_build_sheets(cfg)
        if sh is not None:
            return sh
        return _build_local(cfg)

    if backend == "mirror":
        from .mirror import MirrorStorage
        local = _build_local(cfg)

        def _factory():
            sh = _try_build_sheets(cfg)
            if sh is None:
                raise RuntimeError("Sheets 자격증명/시트 정보가 없거나 잘못됐습니다.")
            return sh
        return MirrorStorage(local=local, sheets_factory=_factory)

    return _build_local(cfg)
