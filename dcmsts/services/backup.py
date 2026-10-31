"""Backup, verification, retention and restore (UC-20) for the SQLite deployment.

A backup is a zip with a consistent database snapshot (SQLite online-backup API), all evidence uploads
and a SHA-256 manifest. For PostgreSQL deployments use pg_dump/managed snapshots instead (see README).
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from ..models import utcnow


class BackupError(RuntimeError):
    pass


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sqlite_path(db_uri):
    if not db_uri.startswith("sqlite:///"):
        raise BackupError("Built-in backup supports SQLite only; use pg_dump for PostgreSQL.")
    return db_uri[len("sqlite:///"):]


def create_backup(db_file, upload_dir, backup_dir, retention):
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
    target = backup_dir / f"dcmsts-{stamp}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "dcmsts.db")
        src = sqlite3.connect(db_file)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        manifest = {"created_at": utcnow().isoformat(), "files": {"dcmsts.db": _sha256(snap)}}
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "dcmsts.db")
            if upload_dir and os.path.isdir(upload_dir):
                for p in sorted(Path(upload_dir).iterdir()):
                    if p.is_file():
                        arc = f"uploads/{p.name}"
                        z.write(p, arc)
                        manifest["files"][arc] = _sha256(p)
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
    ok, detail = verify_backup(target)
    if not ok:
        target.rename(target.with_suffix(".corrupt"))
        raise BackupError(f"Backup failed verification: {detail}")
    purge_old(backup_dir, retention)
    return target


def verify_backup(path):
    """Check every checksum and test-restore the database to a temp file. Returns (ok, detail)."""
    try:
        with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as tmp:
            manifest = json.loads(z.read("manifest.json"))
            for name, digest in manifest["files"].items():
                if hashlib.sha256(z.read(name)).hexdigest() != digest:
                    return False, f"checksum mismatch for {name}"
            z.extract("dcmsts.db", tmp)
            conn = sqlite3.connect(os.path.join(tmp, "dcmsts.db"))
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                audit_rows = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            finally:
                conn.close()
            if result != "ok":
                return False, f"integrity_check: {result}"
            return True, f"{len(manifest['files'])} files verified, {audit_rows} audit entries"
    except (zipfile.BadZipFile, KeyError, sqlite3.DatabaseError, json.JSONDecodeError) as exc:
        return False, str(exc)


def purge_old(backup_dir, retention):
    backups = sorted(Path(backup_dir).glob("dcmsts-*.zip"))
    for old in backups[:-retention] if retention > 0 else []:
        old.unlink()


def restore_backup(path, target_db, target_uploads):
    """Restore into the given locations (e.g. a staging copy). Stop the app before restoring production."""
    ok, detail = verify_backup(path)
    if not ok:
        raise BackupError(f"Refusing to restore unverified backup: {detail}")
    Path(target_db).parent.mkdir(parents=True, exist_ok=True)
    Path(target_uploads).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as tmp:
        z.extractall(tmp)
        shutil.copyfile(os.path.join(tmp, "dcmsts.db"), target_db)
        up = Path(tmp, "uploads")
        if up.is_dir():
            for p in up.iterdir():
                shutil.copyfile(p, Path(target_uploads, p.name))
    return detail
