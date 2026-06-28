"""Backup and restore of the single config file + the SQLite database.

All persistent user state lives in just two files — ``config.toml`` (identity,
favorites, groups, transport settings) and ``conversations.db`` (message history
*and* the NomadNet page cache) — which makes backup/restore simple and complete.

The database is snapshotted through SQLite's online backup API rather than a raw
file copy, so the snapshot is crash-consistent even if the app is running and
writing at the time. Backups are single ``.tar.gz`` artifacts; restore extracts
them and copies the files into place after stashing a safety copy of whatever it
overwrites.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .._compat import UTC

_MANIFEST = "manifest.json"
_CONFIG_NAME = "config.toml"
_DB_NAME = "conversations.db"


@dataclass
class BackupResult:
    path: Path
    config_included: bool
    db_included: bool
    size_bytes: int


@dataclass
class RestoreResult:
    config_restored: bool
    db_restored: bool
    safety_copies: list[Path]


def _snapshot_db(db_path: Path, dest: Path) -> bool:
    """Crash-consistent copy of an SQLite DB via the online backup API."""
    if not db_path.exists():
        return False
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return True


def create_backup(
    config_path: str | Path,
    db_path: str | Path,
    out_dir: str | Path | None = None,
) -> BackupResult:
    """Write a timestamped ``.tar.gz`` containing the config + a DB snapshot.

    ``out_dir`` defaults to the config file's directory. Missing source files are
    simply skipped (and recorded in the manifest), so a fresh install with no DB
    yet still backs up cleanly.
    """
    config_path = Path(config_path).expanduser()
    db_path = Path(db_path).expanduser()
    out_dir = Path(out_dir).expanduser() if out_dir else config_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive = out_dir / f"radio_app-backup-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_included = False
        if config_path.exists():
            (tmp_dir / _CONFIG_NAME).write_bytes(config_path.read_bytes())
            config_included = True
        db_included = _snapshot_db(db_path, tmp_dir / _DB_NAME)

        manifest = {
            "created": datetime.now(UTC).isoformat(),
            "config": config_included,
            "database": db_included,
            "source_config": str(config_path),
            "source_database": str(db_path),
        }
        (tmp_dir / _MANIFEST).write_text(json.dumps(manifest, indent=2))

        with tarfile.open(archive, "w:gz") as tar:
            for name in (_MANIFEST, _CONFIG_NAME, _DB_NAME):
                member = tmp_dir / name
                if member.exists():
                    tar.add(member, arcname=name)

    return BackupResult(
        path=archive,
        config_included=config_included,
        db_included=db_included,
        size_bytes=archive.stat().st_size,
    )


def read_manifest(archive_path: str | Path) -> dict:
    """Return the backup's manifest dict (or ``{}`` if absent/unreadable)."""
    try:
        with tarfile.open(Path(archive_path).expanduser(), "r:gz") as tar:
            member = tar.extractfile(_MANIFEST)
            if member is None:
                return {}
            return json.loads(member.read().decode("utf-8"))
    except (OSError, tarfile.TarError, ValueError, KeyError):
        return {}


def _safe_extract_member(tar: tarfile.TarFile, name: str, dest: Path) -> bool:
    """Extract a single known member to ``dest``, guarding against path tricks."""
    try:
        member = tar.getmember(name)
    except KeyError:
        return False
    # Defensive: only ever extract the exact flat filenames we wrote.
    if member.name != name or member.isdir() or not member.isfile():
        return False
    src = tar.extractfile(member)
    if src is None:
        return False
    dest.write_bytes(src.read())
    return True


def restore_backup(
    archive_path: str | Path,
    config_path: str | Path,
    db_path: str | Path,
    *,
    make_safety_copy: bool = True,
) -> RestoreResult:
    """Restore config + DB from a backup archive, overwriting current files.

    Before overwriting, the existing config/DB are copied aside as
    ``<name>.pre-restore-<timestamp>`` so a bad restore is recoverable. Returns
    which files were restored and the safety copies made.
    """
    archive_path = Path(archive_path).expanduser()
    config_path = Path(config_path).expanduser()
    db_path = Path(db_path).expanduser()
    if not archive_path.exists():
        raise FileNotFoundError(f"backup archive not found: {archive_path}")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    safety: list[Path] = []

    def _stash(target: Path) -> None:
        if make_safety_copy and target.exists():
            backup = target.with_name(f"{target.name}.pre-restore-{stamp}")
            backup.write_bytes(target.read_bytes())
            safety.append(backup)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as tar:
            got_config = _safe_extract_member(tar, _CONFIG_NAME, tmp_dir / _CONFIG_NAME)
            got_db = _safe_extract_member(tar, _DB_NAME, tmp_dir / _DB_NAME)

        config_restored = False
        db_restored = False
        if got_config:
            _stash(config_path)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_bytes((tmp_dir / _CONFIG_NAME).read_bytes())
            config_restored = True
        if got_db:
            _stash(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_bytes((tmp_dir / _DB_NAME).read_bytes())
            db_restored = True

    return RestoreResult(
        config_restored=config_restored,
        db_restored=db_restored,
        safety_copies=safety,
    )

