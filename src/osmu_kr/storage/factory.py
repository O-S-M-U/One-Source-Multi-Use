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


def _build_sqlite(cfg: Config) -> BaseStorage:
    """SQLite v1 백엔드 — config.sqlite_db_path (없으면 ./osmu.db)."""
    from .sqlite_local import SqliteStorage
    db_path = getattr(cfg, "sqlite_db_path", None) or "./osmu.db"
    return SqliteStorage(db_path=db_path)


def _build_postgres(cfg: Config) -> BaseStorage:
    """PostgreSQL + pgvector 백엔드 (Neon 등) — DATABASE_URL 필수."""
    from .postgres import PostgresStorage
    url = getattr(cfg, "database_url", None) or ""
    if not url:
        raise RuntimeError(
            "OSMU_DATABASE_URL 이 비어 있습니다. "
            "Neon/Supabase 등에서 발급받은 PostgreSQL 연결 문자열을 .env 에 설정하세요."
        )
    return PostgresStorage(
        database_url=url,
        pool_min=getattr(cfg, "db_pool_min", 1),
        pool_max=getattr(cfg, "db_pool_max", 4),
    )


def build_storage(cfg: Config) -> BaseStorage:
    """v13 운영 default — PostgreSQL.

    우선순위:
      1) backend 가 명시적으로 지정됐으면 그대로
      2) backend='auto' + DATABASE_URL 있음        → postgres
      3) backend='auto' + Google Sheets 자격 있음   → mirror
      4) backend='auto' + 둘 다 없음                → local (개발·테스트 폴백)
    """
    backend = cfg.resolved_backend()

    if backend == "auto":
        if getattr(cfg, "database_url", None):
            backend = "postgres"
        elif cfg.has_google_credentials and cfg.sheet_id:
            backend = "mirror"
        else:
            backend = "local"

    if backend == "sqlite":
        # 명시적 sqlite — 테스트/개발 전용
        return _build_sqlite(cfg)

    if backend == "postgres":
        return _build_postgres(cfg)

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
