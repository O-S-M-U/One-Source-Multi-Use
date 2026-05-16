"""DB 백업 스크립트 (infra-4).

[ 흐름 ]
  · OSMU_STORAGE_BACKEND 자동 감지:
      - sqlite   → 파일 복사 (SQLite의 .backup API 사용해 WAL 안전 복사)
      - postgres → pg_dump 호출 (PATH 에 pg_dump 필요)
      - 그 외    → 로컬 파일 백업만 (data/*.xlsx, *.csv)
  · 일자별 파일명 + 보관 기간 자동 정리 (default 14일)

[ 사용 ]
  python scripts/backup_db.py [--out ./backups] [--retain-days 14]

  # cron 매일 새벽 4시 예시
  0 4 * * * cd /path/to/osmu && python scripts/backup_db.py >> backups.log 2>&1

[ 환경변수 ]
  · OSMU_BACKUP_DIR        — 기본 ./backups
  · OSMU_BACKUP_RETAIN_DAYS — 기본 14
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backup_db")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def backup_sqlite(db_path: str, out_dir: Path) -> Path:
    """SQLite .backup API 로 WAL 안전 복사 — 활성 트랜잭션도 OK."""
    import sqlite3
    src = Path(db_path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"SQLite DB 없음: {src}")
    out = out_dir / f"osmu_sqlite_{_ts()}.db"
    log.info("[sqlite] backup: %s → %s", src, out)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(out))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return out


def backup_postgres(database_url: str, out_dir: Path) -> Path:
    """pg_dump 로 plain SQL 덤프. pg_dump 가 PATH 에 있어야 함."""
    if shutil.which("pg_dump") is None:
        raise RuntimeError(
            "pg_dump 가 PATH 에 없습니다. PostgreSQL client 도구를 설치하세요 "
            "(예: brew install libpq && brew link --force libpq).",
        )
    out = out_dir / f"osmu_postgres_{_ts()}.sql"
    log.info("[postgres] pg_dump → %s", out)
    cmd = ["pg_dump", "--no-owner", "--no-acl",
           "--format=plain", "-f", str(out), database_url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump 실패 ({proc.returncode}): {proc.stderr[:500]}")
    return out


def backup_local_files(data_dir: str, out_dir: Path) -> list:
    """xlsx/csv 같은 로컬 파일 백업 — sqlite/postgres 운영자도 함께."""
    src = Path(data_dir).expanduser()
    saved: list = []
    if not src.is_dir():
        return saved
    for pattern in ("*.xlsx", "*.csv"):
        for f in src.glob(pattern):
            dst = out_dir / f"{f.stem}_{_ts()}{f.suffix}"
            shutil.copy2(f, dst)
            log.info("[local] file: %s → %s", f, dst)
            saved.append(dst)
    return saved


def prune_old(out_dir: Path, retain_days: int) -> int:
    """retain_days 보다 오래된 백업 삭제."""
    if retain_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retain_days)
    removed = 0
    for f in out_dir.iterdir():
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except Exception:
            continue
        if mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
            log.info("[prune] 오래된 백업 삭제: %s", f.name)
    return removed


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="backup_db")
    p.add_argument("--out", default=os.environ.get("OSMU_BACKUP_DIR", "./backups"))
    p.add_argument("--retain-days", type=int,
                    default=int(os.environ.get("OSMU_BACKUP_RETAIN_DAYS", 14)))
    args = p.parse_args(argv)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 운영 default = postgres. DATABASE_URL 있으면 무조건 그쪽 우선.
    backend = os.environ.get("OSMU_STORAGE_BACKEND", "").strip().lower()
    db_url = os.environ.get("OSMU_DATABASE_URL", "")
    if not backend or backend == "auto":
        backend = "postgres" if db_url else "local"
    log.info("backup 시작 — backend=%s out=%s", backend, out_dir)

    errors: list = []
    try:
        if backend == "postgres":
            if not db_url:
                raise RuntimeError(
                    "OSMU_DATABASE_URL 가 비어 있음 — Neon 등 PostgreSQL 연결 문자열 필요"
                )
            backup_postgres(db_url, out_dir)
        elif backend == "sqlite":
            # 명시적으로 sqlite 백엔드 지정한 경우 (테스트/로컬 디버깅)
            db_path = os.environ.get("OSMU_SQLITE_PATH", "./osmu.db")
            backup_sqlite(db_path, out_dir)
        else:
            log.info("backend=%s — DB 백업 건너뜀. 로컬 파일만 복사.", backend)
        # 어떤 백엔드든 로컬 파일도 백업 (xlsx/csv 가 있다면)
        backup_local_files(os.environ.get("OSMU_LOCAL_DATA_DIR", "./data"), out_dir)
    except Exception as e:
        log.error("backup 실패: %s", e)
        errors.append(str(e))

    pruned = prune_old(out_dir, args.retain_days)
    log.info("✅ 완료. (보관 %d일, 정리 %d건, 오류 %d건)",
              args.retain_days, pruned, len(errors))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
