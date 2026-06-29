"""Offline cache of fetched NomadNet pages.

Every successful :meth:`NomadnetBrowser.fetch` upserts the decoded micron
``content`` here, keyed by ``(dest, path)``. When Reticulum is offline (or a
live fetch fails) the browser can serve the last-known snapshot instead, clearly
flagged with how old it is so a cached page is never mistaken for live.

Only *static* pages are cached: dynamic pages (rendered from request ``var=value``
``field_data``) would cache a single snapshot of one variable combination, which
is misleading, so the browser skips them.

The cache lives in the same SQLite database file as conversations (its own
connection, its own table), so it needs no separate file or configuration.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .._compat import UTC

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nomad_pages (
    dest        TEXT NOT NULL,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (dest, path)
);
"""

# RNS destination hashes are 16 bytes (32 hex chars). A shorter spec is a prefix.
_FULL_HASH_HEX = 32


@dataclass
class CachedPage:
    dest: str
    path: str
    content: str
    fetched_at: datetime
    ok: bool = True

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.fetched_at).total_seconds()

    @property
    def age_human(self) -> str:
        """A coarse, human-friendly age like ``3m``/``5h``/``2d``."""
        secs = max(0.0, self.age_seconds)
        if secs < 90:
            return f"{secs:.0f}s"
        if secs < 5400:
            return f"{secs / 60:.0f}m"
        if secs < 172800:
            return f"{secs / 3600:.0f}h"
        return f"{secs / 86400:.0f}d"


class NomadPageCache:
    """SQLite-backed store of the most recent micron source per ``(dest, path)``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, detect_types=0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writes ---------------------------------------------------------------

    def put(self, dest: str, path: str, content: str, ok: bool = True) -> None:
        """Upsert the latest snapshot for a page."""
        self._conn.execute(
            """
            INSERT INTO nomad_pages (dest, path, content, fetched_at, ok)
            VALUES (?,?,?,?,?)
            ON CONFLICT(dest, path) DO UPDATE SET
                content = excluded.content,
                fetched_at = excluded.fetched_at,
                ok = excluded.ok
            """,
            (
                dest.lower(),
                path,
                content,
                datetime.now(UTC).isoformat(),
                1 if ok else 0,
            ),
        )
        self._conn.commit()

    # -- reads ----------------------------------------------------------------

    def get(self, dest: str, path: str) -> CachedPage | None:
        """Return the cached page for ``(dest, path)``, if any.

        Tolerates a short hex prefix for ``dest`` (the same way node lists surface
        12-char hashes) by falling back to a unique prefix match, so an offline
        lookup can succeed before any announce has resolved the full hash.
        """
        dest_l = dest.strip().lower()
        row = self._conn.execute(
            "SELECT * FROM nomad_pages WHERE dest = ? AND path = ?",
            (dest_l, path),
        ).fetchone()
        if row is None and 0 < len(dest_l) < _FULL_HASH_HEX:
            rows = self._conn.execute(
                "SELECT * FROM nomad_pages WHERE dest LIKE ? AND path = ?",
                (dest_l + "%", path),
            ).fetchall()
            if len(rows) == 1:
                row = rows[0]
        return self._row_to_page(row) if row is not None else None

    def all(self) -> list[CachedPage]:
        rows = self._conn.execute(
            "SELECT * FROM nomad_pages ORDER BY fetched_at DESC"
        ).fetchall()
        return [self._row_to_page(r) for r in rows]

    # -- maintenance ----------------------------------------------------------

    def stats(self) -> dict:
        """Page count and total cached content size (bytes), plus age range."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(LENGTH(content)), 0) AS bytes,
                   MIN(fetched_at) AS oldest,
                   MAX(fetched_at) AS newest
            FROM nomad_pages
            """
        ).fetchone()
        return {
            "pages": row["n"] or 0,
            "content_bytes": row["bytes"] or 0,
            "oldest": row["oldest"],
            "newest": row["newest"],
        }

    def prune(self, older_than_days: int) -> int:
        """Delete cached pages older than ``older_than_days``. Returns rows removed."""
        if older_than_days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        cur = self._conn.execute(
            "DELETE FROM nomad_pages WHERE fetched_at < ?",
            (cutoff.isoformat(),),
        )
        self._conn.commit()
        return cur.rowcount

    def clear(self) -> int:
        """Delete every cached page. Returns rows removed."""
        cur = self._conn.execute("DELETE FROM nomad_pages")
        self._conn.commit()
        return cur.rowcount

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _row_to_page(row: sqlite3.Row) -> CachedPage:
        return CachedPage(
            dest=row["dest"],
            path=row["path"],
            content=row["content"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            ok=bool(row["ok"]),
        )

