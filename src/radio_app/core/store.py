"""SQLite persistence for saved conversations.

Because every message is normalized to a :class:`UnifiedMessage` before it reaches
this layer, conversation history is uniform across transports automatically. A
single thread can interleave internet, LoRa and HF messages transparently.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .message import DeliveryStatus, UnifiedMessage

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


class MessageStore:
    """Thin wrapper around SQLite for storing and querying messages."""

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
        limit: int = 200,
        newest_first: bool = True,
    ) -> list[UnifiedMessage]:
        """Flexible read-only history/search query. All filters are ANDed.

        ``sender`` and ``text`` match case-insensitive substrings; ``thread``,
        ``transport`` and ``group`` match exactly (``group`` tolerates a leading
        ``@``). ``since``/``until`` bound the message timestamp (inclusive) — the
        stored ISO-8601 UTC strings sort chronologically, so plain comparison is
        correct.

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
