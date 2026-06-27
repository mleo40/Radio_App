"""Presence roster: who has been heard recently, across all transports.

The roster is derived entirely from the message store — no extra state,
no persistent table. Each entry represents the most-recent observed
activity from a given (callsign, transport) pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import MessageStore


@dataclass
class PresenceEntry:
    callsign: str
    transport: str
    last_seen: datetime
    last_snr: float | None    # from metadata.snr; None if not reported
    message_count: int
    last_content: str         # truncated preview of the most recent message


def get_roster(
    store: "MessageStore",
    *,
    since: datetime | None = None,
    transport: str | None = None,
    limit: int = 100,
) -> list[PresenceEntry]:
    """Return a list of recently-heard callsigns, most-recent first.

    ``since`` defaults to 24 hours ago when not specified.
    """
    if since is None:
        since = datetime.now(UTC) - timedelta(hours=24)

    clauses = ["timestamp >= ?"]
    params: list = [since.isoformat()]
    if transport:
        clauses.append("transport = ?")
        params.append(transport)

    where = " AND ".join(clauses)
    rows = store._conn.execute(
        f"""
        SELECT
            sender,
            transport,
            MAX(timestamp)                          AS last_seen,
            json_extract(
                (SELECT metadata FROM messages AS m2
                 WHERE m2.sender = m.sender
                   AND m2.transport = m.transport
                 ORDER BY timestamp DESC LIMIT 1),
                '$.snr'
            )                                       AS last_snr,
            COUNT(*)                                AS cnt,
            (SELECT content FROM messages AS m2
             WHERE m2.sender = m.sender
               AND m2.transport = m.transport
             ORDER BY timestamp DESC LIMIT 1)       AS last_content
        FROM messages AS m
        WHERE {where}
        GROUP BY sender, transport
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()

    result = []
    for row in rows:
        sender, tp, ts, snr, cnt, content = (
            row[0], row[1], row[2], row[3], row[4], row[5]
        )
        result.append(PresenceEntry(
            callsign=sender,
            transport=tp or "",
            last_seen=datetime.fromisoformat(ts),
            last_snr=float(snr) if snr is not None else None,
            message_count=cnt,
            last_content=(content or "")[:80],
        ))
    return result
