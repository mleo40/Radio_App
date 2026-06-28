"""SQLite persistence for saved conversations.

Because every message is normalized to a :class:`UnifiedMessage` before it reaches
this layer, conversation history is uniform across transports automatically. A
single thread can interleave internet, LoRa and HF messages transparently.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .._compat import UTC
from .message import DeliveryStatus, UnifiedMessage

_SCHEDULED_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id          TEXT PRIMARY KEY,
    fire_at     TEXT NOT NULL,
    msg_json    TEXT NOT NULL,
    transport   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_fire_at ON scheduled_messages(fire_at)
    WHERE status = 'pending';
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id        TEXT PRIMARY KEY,
    thread_key    TEXT NOT NULL,
    sender        TEXT NOT NULL,
    recipient     TEXT,
    group_name    TEXT,
    address_type  TEXT NOT NULL,
    content       TEXT NOT NULL,
    transport     TEXT,
    status        TEXT NOT NULL,
    metadata      TEXT,
    timestamp     TEXT NOT NULL,
    received_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thread ON messages(thread_key, timestamp);
CREATE INDEX IF NOT EXISTS idx_group ON messages(group_name);
"""

# Full-text index over message bodies (+ sender/group) kept in sync with the
# ``messages`` table by triggers. An *external-content* FTS5 table stores only
# the index (not a copy of the text), mapping back via ``messages.rowid``. FTS5
# is a compile-time option; on a sqlite build without it, creation raises and we
# fall back to LIKE substring search (see MessageStore._init_fts / search_ranked).
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, sender, group_name,
    content='messages', content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, sender, group_name)
    VALUES (new.rowid, new.content, new.sender, new.group_name);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, sender, group_name)
    VALUES ('delete', old.rowid, old.content, old.sender, old.group_name);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, sender, group_name)
    VALUES ('delete', old.rowid, old.content, old.sender, old.group_name);
    INSERT INTO messages_fts(rowid, content, sender, group_name)
    VALUES (new.rowid, new.content, new.sender, new.group_name);
END;
"""

# Sentinel markers FTS5 ``snippet()`` wraps around matched terms; the UI swaps
# these control chars for its own highlight markup (they can't occur in text).
SNIPPET_OPEN = "\x01"
SNIPPET_CLOSE = "\x02"


@dataclass
class ScheduledEntry:
    """A message queued for future transmission."""

    id: str
    fire_at: datetime
    message: UnifiedMessage
    transport: str | None


@dataclass(frozen=True)
class SearchHit:
    """A ranked search result: the message, a highlighted snippet, its thread."""

    message: UnifiedMessage
    snippet: str
    thread_key: str


@dataclass(frozen=True)
class ThreadSummary:
    """A per-conversation rollup for the cross-mode "All chats" archive."""

    thread_key: str
    transport: str
    count: int
    last_ts: str
    last_sender: str
    last_content: str
    last_status: str


class MessageStore:
    """Thin wrapper around SQLite for storing and querying messages."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, detect_types=0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.executescript(_SCHEDULED_SCHEMA)
        self._conn.commit()
        self._init_fts()

    def _init_fts(self) -> None:
        """Create the FTS5 index + sync triggers, backfilling on first creation.

        Sets ``self.fts_enabled``. If this sqlite build lacks FTS5 the creation
        raises ``OperationalError`` ("no such module: fts5") and we degrade to
        LIKE search — no data is lost, queries are just unranked.
        """
        self.fts_enabled = False
        try:
            existed = (
                self._conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='messages_fts'"
                ).fetchone()
                is not None
            )
            self._conn.executescript(_FTS_SCHEMA)
            if not existed:
                # Populate the index from any pre-existing rows (migration).
                self._conn.execute(
                    "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')"
                )
            self._conn.commit()
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False

    def close(self) -> None:
        self._conn.close()


    # -- writes ---------------------------------------------------------------

    def save(self, msg: UnifiedMessage, thread_key: str | None = None) -> bool:
        """Insert a message. Returns False if it was a duplicate (by msg_id)."""
        try:
            self._conn.execute(
                """
                INSERT INTO messages (
                    msg_id, thread_key, sender, recipient, group_name,
                    address_type, content, transport, status, metadata,
                    timestamp, received_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    msg.msg_id,
                    thread_key or msg.thread_key,
                    msg.sender,
                    msg.recipient,
                    msg.group,
                    msg.address_type.value,
                    msg.content,
                    msg.transport,
                    msg.status.value,
                    json.dumps(msg.metadata),
                    msg.timestamp.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate msg_id -> already stored (dedup)

    def update_status(
        self,
        msg_id: str,
        status: DeliveryStatus,
        transport: str | None = None,
    ) -> None:
        """Update a message's delivery status, and optionally its transport.

        The transport is set on outbound messages once the carrying medium is
        known (it isn't at first insert), so conversations get attributed to the
        right mode in the thread list.
        """
        if transport:
            self._conn.execute(
                "UPDATE messages SET status = ?, transport = ? WHERE msg_id = ?",
                (status.value, transport, msg_id),
            )
        else:
            self._conn.execute(
                "UPDATE messages SET status = ? WHERE msg_id = ?",
                (status.value, msg_id),
            )
        self._conn.commit()

    # -- reads ----------------------------------------------------------------

    def exists(self, msg_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM messages WHERE msg_id = ? LIMIT 1", (msg_id,)
        )
        return cur.fetchone() is not None

    def threads(self) -> list[tuple[str, int, str]]:
        """Return (thread_key, message_count, last_timestamp), newest first."""
        cur = self._conn.execute(
            """
            SELECT thread_key, COUNT(*) AS n, MAX(timestamp) AS last_ts
            FROM messages GROUP BY thread_key ORDER BY last_ts DESC
            """
        )
        return [(r["thread_key"], r["n"], r["last_ts"]) for r in cur.fetchall()]

    def thread_summaries(self, limit: int = 1000) -> list[ThreadSummary]:
        """Per-conversation rollups across ALL transports, newest activity first.

        Powers the cross-mode "All chats" archive (the superset of the per-mode
        thread list). For each thread it returns the message count, last-activity
        time, the carrying transport (most recent non-empty), and a preview of
        the latest message (sender, body, status) so the UI can show who/what
        without a second read. One grouped query + one detail query per thread —
        the thread count is small in practice (the per-mode pane already loops
        per thread).
        """
        rows = self._conn.execute(
            """
            SELECT thread_key, COUNT(*) AS n, MAX(timestamp) AS last_ts
            FROM messages GROUP BY thread_key ORDER BY last_ts DESC LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        out: list[ThreadSummary] = []
        for r in rows:
            key = r["thread_key"]
            last = self._conn.execute(
                """
                SELECT sender, content, status, transport,
                    (SELECT transport FROM messages
                     WHERE thread_key = ? AND transport != ''
                     ORDER BY timestamp DESC LIMIT 1) AS resolved_transport
                FROM messages WHERE thread_key = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (key, key),
            ).fetchone()
            out.append(
                ThreadSummary(
                    thread_key=key,
                    transport=(last["resolved_transport"] if last else "") or "",
                    count=r["n"] or 0,
                    last_ts=r["last_ts"] or "",
                    last_sender=(last["sender"] if last else "") or "",
                    last_content=(last["content"] if last else "") or "",
                    last_status=(last["status"] if last else "") or "",
                )
            )
        return out

    def read_thread(self, thread_key: str, limit: int = 200) -> list[UnifiedMessage]:
        cur = self._conn.execute(
            """
            SELECT * FROM messages WHERE thread_key = ?
            ORDER BY timestamp ASC LIMIT ?
            """,
            (thread_key, limit),
        )
        return [self._row_to_message(r) for r in cur.fetchall()]

    def read_transport(
        self, transport: str, limit: int = 200
    ) -> list[UnifiedMessage]:
        """Return a transport's most recent messages, oldest-first.

        Powers the "all messages" firehose a chat mode shows when no specific
        conversation is selected (e.g. JS8Call with no callsign/@group chosen).
        We take the newest ``limit`` rows then re-sort ascending so the view
        reads top-to-bottom like a normal conversation.
        """
        cur = self._conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages WHERE transport = ?
                ORDER BY timestamp DESC LIMIT ?
            ) ORDER BY timestamp ASC
            """,
            (transport, limit),
        )
        return [self._row_to_message(r) for r in cur.fetchall()]

    def thread_transport(self, thread_key: str) -> str:
        """Return the most recent non-empty transport seen in a thread."""
        cur = self._conn.execute(
            """
            SELECT transport FROM messages
            WHERE thread_key = ? AND transport != ''
            ORDER BY timestamp DESC LIMIT 1
            """,
            (thread_key,),
        )
        row = cur.fetchone()
        return row[0] if row else ""

    def search(self, term: str, limit: int = 100) -> list[UnifiedMessage]:
        cur = self._conn.execute(
            """
            SELECT * FROM messages WHERE content LIKE ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (f"%{term}%", limit),
        )
        return [self._row_to_message(r) for r in cur.fetchall()]

    def query(
        self,
        *,
        thread: str | None = None,
        transport: str | None = None,
        sender: str | None = None,
        group: str | None = None,
        text: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        status: str | None = None,
        snr_min: float | None = None,
        limit: int = 200,
        newest_first: bool = True,
    ) -> list[UnifiedMessage]:
        """Flexible read-only history/search query. All filters are ANDed.

        ``sender`` and ``text`` match case-insensitive substrings; ``thread``,
        ``transport`` and ``group`` match exactly (``group`` tolerates a leading
        ``@``). ``since``/``until`` bound the message timestamp (inclusive) — the
        stored ISO-8601 UTC strings sort chronologically, so plain comparison is
        correct.

        ``status`` filters by delivery status string (e.g. "received", "sent").
        ``snr_min`` filters by SNR value stored in metadata (JS8Call-style),
        keeping only messages where ``metadata.snr >= snr_min``.

        ``limit`` always bounds the *most recent* matches (newest N); pass
        ``newest_first=False`` to return those N oldest-first for a
        conversation-style read.
        """
        clauses: list[str] = []
        params: list[object] = []
        if thread:
            clauses.append("thread_key = ?")
            params.append(thread)
        if transport:
            clauses.append("transport = ?")
            params.append(transport)
        if sender:
            clauses.append("LOWER(sender) LIKE LOWER(?)")
            params.append(f"%{sender}%")
        if group:
            clauses.append("group_name = ?")
            params.append(group.lstrip("@"))
        if text:
            clauses.append("content LIKE ?")
            params.append(f"%{text}%")
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        if status:
            clauses.append("status = ?")
            params.append(status)
        if snr_min is not None:
            clauses.append("CAST(json_extract(metadata, '$.snr') AS REAL) >= ?")
            params.append(snr_min)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, limit))
        rows = self._conn.execute(
            f"SELECT * FROM messages{where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        msgs = [self._row_to_message(r) for r in rows]
        if not newest_first:
            msgs.reverse()
        return msgs

    def search_ranked(
        self,
        term: str,
        *,
        limit: int = 100,
        transport: str | None = None,
        group: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[SearchHit]:
        """Full-text search returning ranked hits with highlighted snippets.

        Uses the FTS5 index (BM25 ``rank`` ordering + ``snippet()`` highlights)
        when available, transparently falling back to a LIKE substring scan
        (newest-first, snippet built around the first match) otherwise. Optional
        ``transport``/``group``/``since``/``until`` filters are ANDed in. The
        single-word case is prefix-matched ("brid" finds "bridge"); multi-word
        terms match as an ordered phrase.
        """
        term = (term or "").strip()
        if not term:
            return []
        if self.fts_enabled:
            hits = self._search_fts(term, limit, transport, group, since, until)
            if hits is not None:
                return hits
        return self._search_like(term, limit, transport, group, since, until)

    def _search_fts(
        self,
        term: str,
        limit: int,
        transport: str | None,
        group: str | None,
        since: datetime | None,
        until: datetime | None,
    ) -> list[SearchHit] | None:
        clauses = ["messages_fts MATCH ?"]
        params: list[object] = [self._fts_match_query(term)]
        if transport:
            clauses.append("m.transport = ?")
            params.append(transport)
        if group:
            clauses.append("m.group_name = ?")
            params.append(group.lstrip("@"))
        if since is not None:
            clauses.append("m.timestamp >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("m.timestamp <= ?")
            params.append(until.isoformat())
        params.append(max(1, limit))
        sql = (
            "SELECT m.*, "
            f"snippet(messages_fts, 0, '{SNIPPET_OPEN}', '{SNIPPET_CLOSE}', "
            "'…', 12) AS snip "
            "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
            f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?"
        )
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Malformed MATCH expression for this term — let the caller fall back.
            return None
        return [
            SearchHit(
                self._row_to_message(r),
                r["snip"] or r["content"],
                r["thread_key"],
            )
            for r in rows
        ]

    def _search_like(
        self,
        term: str,
        limit: int,
        transport: str | None,
        group: str | None,
        since: datetime | None,
        until: datetime | None,
    ) -> list[SearchHit]:
        clauses = ["content LIKE ?"]
        params: list[object] = [f"%{term}%"]
        if transport:
            clauses.append("transport = ?")
            params.append(transport)
        if group:
            clauses.append("group_name = ?")
            params.append(group.lstrip("@"))
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        params.append(max(1, limit))
        rows = self._conn.execute(
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            SearchHit(
                self._row_to_message(r),
                self._like_snippet(r["content"], term),
                r["thread_key"],
            )
            for r in rows
        ]

    @staticmethod
    def _fts_match_query(term: str) -> str:
        """Build a safe FTS5 MATCH expression from free user text.

        A single alphanumeric word becomes a prefix query (``brid`` → ``brid*``);
        anything else is phrase-quoted token-by-token (embedded quotes escaped),
        which neutralises FTS5 operators so a user can't accidentally write
        query syntax.
        """
        tokens = [t for t in term.split() if t]
        if not tokens:
            return '""'
        if len(tokens) == 1 and tokens[0].isalnum():
            return tokens[0] + "*"
        return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)

    @staticmethod
    def _like_snippet(content: str, term: str, *, width: int = 60) -> str:
        """Build a highlighted snippet around the first case-insensitive match."""
        low = content.lower()
        idx = low.find(term.lower())
        if idx < 0:
            return content[:width] + ("…" if len(content) > width else "")
        start = max(0, idx - width // 3)
        end = min(len(content), idx + len(term) + width // 2)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(content) else ""
        head = content[start:idx]
        match = content[idx : idx + len(term)]
        tail = content[idx + len(term) : end]
        return f"{prefix}{head}{SNIPPET_OPEN}{match}{SNIPPET_CLOSE}{tail}{suffix}"

    def delete_thread(self, thread_key: str) -> int:
        """Delete every message in a conversation. Returns rows removed.

        Used to "close" a conversation from the UI: threads are derived purely
        from stored messages, so removing the messages removes the thread.
        """
        cur = self._conn.execute(
            "DELETE FROM messages WHERE thread_key = ?", (thread_key,)
        )
        self._conn.commit()
        return cur.rowcount

    def purge_older_than(self, days: int) -> int:
        """Delete messages older than ``days``. Returns rows removed."""
        if days <= 0:
            return 0
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        cur = self._conn.execute(
            "SELECT msg_id, timestamp FROM messages",
        )
        to_delete = [
            r["msg_id"]
            for r in cur.fetchall()
            if datetime.fromisoformat(r["timestamp"]).timestamp() < cutoff
        ]
        self._conn.executemany(
            "DELETE FROM messages WHERE msg_id = ?", ((m,) for m in to_delete)
        )
        self._conn.commit()
        return len(to_delete)

    # -- maintenance ----------------------------------------------------------

    def stats(self) -> dict:
        """Summarise the message store for ``db stats`` / the Health board.

        Returns message + thread counts, the oldest/newest timestamps, and the
        on-disk size of the database file (which holds every table, including the
        NomadNet page cache).
        """
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n, MIN(timestamp) AS oldest, MAX(timestamp) AS newest
            FROM messages
            """
        ).fetchone()
        threads = self._conn.execute(
            "SELECT COUNT(DISTINCT thread_key) AS t FROM messages"
        ).fetchone()
        return {
            "messages": row["n"] or 0,
            "threads": threads["t"] or 0,
            "oldest": row["oldest"],
            "newest": row["newest"],
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def vacuum(self) -> int:
        """Rebuild the database to reclaim free pages. Returns bytes freed.

        SQLite never shrinks the file on its own after deletes, so without a
        periodic VACUUM the database only grows. VACUUM cannot run inside a
        transaction, so we drop to autocommit for the duration.
        """
        before = self.path.stat().st_size if self.path.exists() else 0
        prev_isolation = self._conn.isolation_level
        try:
            self._conn.isolation_level = None  # autocommit so VACUUM is allowed
            self._conn.execute("VACUUM")
        finally:
            self._conn.isolation_level = prev_isolation
        after = self.path.stat().st_size if self.path.exists() else 0
        return max(0, before - after)


    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> UnifiedMessage:
        return UnifiedMessage.from_dict(
            {
                "msg_id": row["msg_id"],
                "sender": row["sender"],
                "recipient": row["recipient"],
                "group": row["group_name"],
                "address_type": row["address_type"],
                "content": row["content"],
                "transport": row["transport"],
                "status": row["status"],
                "metadata": json.loads(row["metadata"] or "{}"),
                "timestamp": row["timestamp"],
            }
        )

    # -- scheduled messages ---------------------------------------------------

    def schedule_add(
        self,
        msg: UnifiedMessage,
        fire_at: datetime,
        transport: str | None = None,
    ) -> str:
        """Persist a scheduled message; return its id."""
        self._conn.execute(
            "INSERT INTO scheduled_messages "
            "(id, fire_at, msg_json, transport, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                msg.msg_id,
                fire_at.isoformat(),
                json.dumps(msg.to_dict()),
                transport,
                datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()
        return msg.msg_id

    def schedule_pending(self, up_to: datetime | None = None) -> list[ScheduledEntry]:
        """Return pending scheduled messages, optionally only those due by ``up_to``."""
        clause = "status = 'pending'"
        params: list = []
        if up_to is not None:
            clause += " AND fire_at <= ?"
            params.append(up_to.isoformat())
        rows = self._conn.execute(
            f"SELECT id, fire_at, msg_json, transport FROM scheduled_messages "
            f"WHERE {clause} ORDER BY fire_at",
            params,
        ).fetchall()
        result = []
        for row in rows:
            msg = UnifiedMessage.from_dict(json.loads(row[2]))
            result.append(ScheduledEntry(
                id=row[0],
                fire_at=datetime.fromisoformat(row[1]),
                message=msg,
                transport=row[3],
            ))
        return result

    def schedule_cancel(self, entry_id: str) -> bool:
        """Mark a pending scheduled message as cancelled. Returns True if found."""
        cur = self._conn.execute(
            "UPDATE scheduled_messages SET status='cancelled' "
            "WHERE id=? AND status='pending'",
            (entry_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def schedule_mark_sent(self, entry_id: str, *, success: bool) -> None:
        """Update a scheduled message's status after a send attempt."""
        status = "sent" if success else "failed"
        self._conn.execute(
            "UPDATE scheduled_messages SET status=? WHERE id=?",
            (status, entry_id),
        )
        self._conn.commit()
