"""Database backups: a nightly ``pg_dump`` to disk, kept for a few days, optionally copied to
an S3-compatible bucket. Restore with ``carscan backup restore <file>``. See docs/backups.md."""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import structlog

from app.config import get_settings

log = structlog.get_logger()
KEEP = 7


def _plain_url(url: str | None = None) -> str:
    """psycopg's scheme is not what libpq wants."""
    parts = urlsplit(url or get_settings().database_url)
    return urlunsplit(("postgresql", parts.netloc, parts.path, parts.query, parts.fragment))


def _bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    try:  # the embedded Postgres used by the tests ships the client tools
        import pgserver

        cand = Path(pgserver.__file__).parent / "pginstall" / "bin" / name
        if cand.exists():
            return str(cand)
    except ImportError:
        pass
    raise FileNotFoundError(f"{name} not installed")


def create(directory: Path | None = None, url: str | None = None) -> Path:
    """Write a compressed custom-format dump: <dir>/carscan-YYYYMMDD-HHMM.dump.gz."""
    directory = directory or Path(get_settings().backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"carscan-{datetime.now(UTC):%Y%m%d-%H%M}.dump.gz"
    cmd = [_bin("pg_dump"), "--format=custom", "--no-owner", "--no-privileges", _plain_url(url)]
    # pg_dump writes to a pipe; we gzip the stream here (a GzipFile's fd would get raw bytes)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    with gzip.open(path, "wb") as out:
        shutil.copyfileobj(proc.stdout, out)
    _, err = proc.communicate()
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {err.decode(errors='replace')[-500:]}")
    log.info("backup.created", path=str(path), bytes=path.stat().st_size)
    return path


def prune(directory: Path | None = None, keep: int = KEEP) -> int:
    directory = directory or Path(get_settings().backup_dir)
    files = sorted(directory.glob("carscan-*.dump.gz"))
    for old in files[:-keep] if len(files) > keep else []:
        old.unlink()
    return max(0, len(files) - keep)


def restore(path: Path, url: str | None = None) -> None:
    """Restore a dump into the database of DATABASE_URL (or ``url``). Drops and recreates
    the objects it contains; run it against an empty database or accept the overwrite."""
    cmd = [
        _bin("pg_restore"),
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        _plain_url(url),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    with gzip.open(path, "rb") as src:
        shutil.copyfileobj(src, proc.stdin)
    proc.stdin.close()
    assert proc.stderr is not None
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {err.decode(errors='replace')[-800:]}")
    log.info("backup.restored", path=str(path))


def upload(path: Path) -> str | None:
    """Copy the dump to the S3-compatible bucket from BACKUP_S3_* when configured."""
    st = get_settings()
    if not st.backup_s3_bucket:
        return None
    import boto3  # optional dependency, only needed with a bucket

    client = boto3.client(
        "s3",
        endpoint_url=st.backup_s3_endpoint or None,
        aws_access_key_id=st.backup_s3_access_key,
        aws_secret_access_key=st.backup_s3_secret_key.get_secret_value(),
        region_name=st.backup_s3_region or None,
    )
    key = f"{st.backup_s3_prefix.strip('/')}/{path.name}" if st.backup_s3_prefix else path.name
    client.upload_file(str(path), st.backup_s3_bucket, key)
    log.info("backup.uploaded", bucket=st.backup_s3_bucket, key=key)
    return key


def nightly() -> dict[str, str | int]:
    path = create()
    key = upload(path)
    pruned = prune()
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "uploaded": key or "",
        "pruned": pruned,
    }


def listing(directory: Path | None = None) -> list[tuple[str, int]]:
    directory = directory or Path(get_settings().backup_dir)
    if not directory.exists():
        return []
    return [(p.name, p.stat().st_size) for p in sorted(directory.glob("carscan-*.dump.gz"))]


def env_for_tools() -> dict[str, str]:
    return dict(os.environ)
