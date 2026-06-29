"""Net control / roll-call session manager.

A *net session* is a named, timed gathering of radio operators on a shared
frequency or channel.  Net control opens the session, stations check in, net
control logs each check-in, then closes with a summary.

All session state is held in-memory during a run; sessions and check-ins are
persisted to two SQLite tables so the log survives app restarts.

Usage pattern
-------------
::

    net = NetManager(store)

    session = net.open("EMS Morning Net", transport="js8call", net_control="KC1QKM")
    net.check_in("KE0XYZ")
    net.check_in("W1AW", note="FN42 no traffic")
    closed = net.close()
    # closed.check_ins has the full list; closed.duration_min is elapsed minutes

"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .._compat import UTC

if TYPE_CHECKING:
    from .store import MessageStore

_NET_SCHEMA = """
CREATE TABLE IF NOT EXISTS net_sessions (
    session_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    transport   TEXT NOT NULL DEFAULT '',
    net_control TEXT NOT NULL DEFAULT '',
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS net_check_ins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES net_sessions(session_id),
    callsign      TEXT NOT NULL,
    checked_in_at TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nci_session ON net_check_ins(session_id, checked_in_at);
"""


@dataclass
class CheckIn:
    callsign: str
    checked_in_at: datetime
    note: str = ""
    row_id: int | None = None


@dataclass
class NetSession:
    session_id: str
    name: str
    transport: str
    net_control: str
    opened_at: datetime
    closed_at: datetime | None = None
    status: str = "open"
    check_ins: list[CheckIn] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def duration_min(self) -> float | None:
        """Elapsed minutes from open to close, or None if still open."""
        if self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds() / 60


class NetManager:
    """Manages net sessions backed by SQLite.

    Instantiated once in ``App`` alongside the message store.  Sessions
    persist across restarts; only one session may be *open* at a time.
    """

    def __init__(self, store: MessageStore) -> None:
        self._store = store
        self._conn = store._conn
        self._active: NetSession | None = None
        self._init_schema()
        self._reload_active()

    # -- schema / init ---------------------------------------------------------

    def _init_schema(self) -> None:
        for stmt in (s.strip() for s in _NET_SCHEMA.split(";") if s.strip()):
            self._conn.execute(stmt)
        self._conn.commit()

    def _reload_active(self) -> None:
        """Restore an in-progress session (if any) from the last run."""
        row = self._conn.execute(
            "SELECT session_id, name, transport, net_control, opened_at "
            "FROM net_sessions WHERE status='open' ORDER BY opened_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return
        sid, name, transport, nc, opened = row
        self._active = NetSession(
            session_id=sid,
            name=name,
            transport=transport or "",
            net_control=nc or "",
            opened_at=datetime.fromisoformat(opened),
            check_ins=self._load_check_ins(sid),
        )

    def _load_check_ins(self, session_id: str) -> list[CheckIn]:
        rows = self._conn.execute(
            "SELECT callsign, checked_in_at, note, id "
            "FROM net_check_ins WHERE session_id=? ORDER BY checked_in_at",
            (session_id,),
        ).fetchall()
        return [
            CheckIn(
                callsign=r[0],
                checked_in_at=datetime.fromisoformat(r[1]),
                note=r[2] or "",
                row_id=r[3],
            )
            for r in rows
        ]

    # -- queries ---------------------------------------------------------------

    @property
    def active(self) -> NetSession | None:
        return self._active

    def recent_sessions(self, limit: int = 10) -> list[NetSession]:
        """Return the most recent sessions (open or closed), newest first."""
        rows = self._conn.execute(
            "SELECT session_id, name, transport, net_control, opened_at, closed_at, status "
            "FROM net_sessions ORDER BY opened_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            sid, name, transport, nc, opened, closed, status = row
            result.append(NetSession(
                session_id=sid,
                name=name,
                transport=transport or "",
                net_control=nc or "",
                opened_at=datetime.fromisoformat(opened),
                closed_at=datetime.fromisoformat(closed) if closed else None,
                status=status,
                check_ins=self._load_check_ins(sid),
            ))
        return result

    # -- mutations -------------------------------------------------------------

    def open(
        self,
        name: str,
        *,
        transport: str = "",
        net_control: str = "",
    ) -> NetSession:
        """Open a new net session.

        Raises ``ValueError`` if a session is already open.
        """
        if self._active and self._active.is_open:
            raise ValueError(
                f"Net '{self._active.name}' is already open — close it first."
            )
        now = datetime.now(UTC)
        sid = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO net_sessions "
            "(session_id, name, transport, net_control, opened_at, status) "
            "VALUES (?,?,?,?,?,?)",
            (sid, name, transport, net_control, now.isoformat(), "open"),
        )
        self._conn.commit()
        self._active = NetSession(
            session_id=sid,
            name=name,
            transport=transport,
            net_control=net_control,
            opened_at=now,
        )
        return self._active

    def check_in(self, callsign: str, note: str = "") -> CheckIn:
        """Log a check-in for *callsign*.

        Raises ``ValueError`` if no session is currently open.
        """
        if self._active is None or not self._active.is_open:
            raise ValueError("No open net session — use 'net open <name>' first.")
        now = datetime.now(UTC)
        cur = self._conn.execute(
            "INSERT INTO net_check_ins (session_id, callsign, checked_in_at, note) "
            "VALUES (?,?,?,?)",
            (self._active.session_id, callsign.upper(), now.isoformat(), note),
        )
        self._conn.commit()
        ci = CheckIn(
            callsign=callsign.upper(),
            checked_in_at=now,
            note=note,
            row_id=cur.lastrowid,
        )
        self._active.check_ins.append(ci)
        return ci

    def close(self) -> NetSession:
        """Close the active net session.

        Raises ``ValueError`` if no session is currently open.
        """
        if self._active is None or not self._active.is_open:
            raise ValueError("No open net session.")
        now = datetime.now(UTC)
        self._conn.execute(
            "UPDATE net_sessions SET closed_at=?, status='closed' WHERE session_id=?",
            (now.isoformat(), self._active.session_id),
        )
        self._conn.commit()
        self._active.closed_at = now
        self._active.status = "closed"
        closed = self._active
        self._active = None
        return closed
