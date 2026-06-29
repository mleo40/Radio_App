"""Tests for config + database backup and restore."""

from __future__ import annotations

import sqlite3

import pytest

from radio_app.core.backup import (
    create_backup,
    read_manifest,
    restore_backup,
)


def _seed_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()


def _row_count(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_backup_includes_config_and_db(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[general]\ndisplay_name = \"Tester\"\n")
    db = tmp_path / "conversations.db"
    _seed_db(db)

    res = create_backup(cfg, db, tmp_path / "backups")
    assert res.path.exists()
    assert res.config_included is True
    assert res.db_included is True
    assert res.size_bytes > 0

    manifest = read_manifest(res.path)
    assert manifest["config"] is True
    assert manifest["database"] is True


def test_backup_without_db_still_works(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[general]\n")
    db = tmp_path / "missing.db"  # never created
    res = create_backup(cfg, db, tmp_path)
    assert res.config_included is True
    assert res.db_included is False


def test_restore_roundtrip_overwrites_and_stashes_safety_copy(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[general]\ndisplay_name = \"Original\"\n")
    db = tmp_path / "conversations.db"
    _seed_db(db)
    assert _row_count(db) == 3

    archive = create_backup(cfg, db, tmp_path / "backups").path

    # Mutate both files after the backup.
    cfg.write_text("[general]\ndisplay_name = \"Changed\"\n")
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()
    assert _row_count(db) == 0

    res = restore_backup(archive, cfg, db)
    assert res.config_restored is True
    assert res.db_restored is True
    # Files are back to their backed-up state.
    assert "Original" in cfg.read_text()
    assert _row_count(db) == 3
    # Safety copies of the pre-restore files were made.
    assert len(res.safety_copies) == 2
    assert all(p.exists() for p in res.safety_copies)


def test_restore_missing_archive_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore_backup(
            tmp_path / "nope.tar.gz",
            tmp_path / "config.toml",
            tmp_path / "db.sqlite",
        )


def test_db_snapshot_is_consistent_copy(tmp_path):
    """The DB in a backup is a real, queryable SQLite database (not corrupt)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[general]\n")
    db = tmp_path / "conversations.db"
    _seed_db(db)
    archive = create_backup(cfg, db, tmp_path).path

    # Restore into a fresh location and confirm the data survives.
    out_cfg = tmp_path / "restored" / "config.toml"
    out_db = tmp_path / "restored" / "conversations.db"
    restore_backup(archive, out_cfg, out_db)
    assert _row_count(out_db) == 3

