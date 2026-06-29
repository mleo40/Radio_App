"""App-level message chunking + ACK/retry for small-MTU transports.

Some media carry only tiny frames (JS8Call ~80 bytes, MeshCore 134 bytes), so a
longer message must be split into several on-air frames and reassembled by the
receiver. This module provides the *pure* building blocks for that — wire framing,
segmentation and a reassembly buffer — plus a lightweight ACK helper so a sender
on a medium without native delivery confirmation (JS8) can retransmit lost parts.

The framing is deliberately compact (every header byte steals payload room on an
already tiny frame) and pure-ASCII so it survives text-only transports:

    chunk part :  ``RC|<gid>|<seq>/<total>|<payload>``
    ack frame  :  ``RA|<gid>|<bitmap>``

``gid`` is a short base36 group id, ``seq`` is 1-based. A single-frame message is
never wrapped — only messages that exceed the transport's ``max_message_size``
get chunked, so normal traffic carries zero overhead. Frames that don't parse as
a chunk/ack are passed through untouched, so this layer is invisible to every
other transport.

The router owns the timing (asyncio) and wiring; everything here is synchronous
and side-effect free so it can be unit-tested without a radio or event loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Wire markers. Kept to two ASCII chars + a pipe to minimise per-frame overhead.
_CHUNK_PREFIX = "RC|"
_ACK_PREFIX = "RA|"
_SEP = "|"

#: How long a partially-received group is kept before it's abandoned (seconds).
REASSEMBLY_TTL_S = 600.0


def group_id(msg_id: str) -> str:
    """Derive a short, stable group id from a message id.

    Uses the first 6 hex chars of the (hex) ``msg_id``; falls back to a hash for
    non-hex ids. Short on purpose — it rides in every chunk header.
    """
    token = "".join(c for c in msg_id.lower() if c in "0123456789abcdef")
    if len(token) >= 6:
        return token[:6]
    return format(abs(hash(msg_id)) % (36**4), "x")[:6] or "0"


def header_overhead(gid: str, total: int) -> int:
    """Bytes consumed by a chunk header for ``gid``/``total`` (UTF-8)."""
    # RC| + gid + | + seq + / + total + |   (seq is at most len(str(total)) wide)
    sample = f"{_CHUNK_PREFIX}{gid}{_SEP}{total}/{total}{_SEP}"
    return len(sample.encode("utf-8"))


def needs_chunking(content: str, limit: int) -> bool:
    """True when ``content`` (UTF-8) doesn't fit in a single ``limit``-byte frame."""
    return len(content.encode("utf-8")) > limit > 0


def _payload_budget(limit: int, gid: str, total: int) -> int:
    return max(1, limit - header_overhead(gid, total))


def _split_utf8(content: str, budget: int) -> list[str]:
    """Split ``content`` into pieces of ≤ ``budget`` UTF-8 bytes (never mid-rune)."""
    pieces: list[str] = []
    cur = ""
    cur_b = 0
    for ch in content:
        cb = len(ch.encode("utf-8"))
        if cur_b + cb > budget and cur:
            pieces.append(cur)
            cur, cur_b = "", 0
        cur += ch
        cur_b += cb
    if cur:
        pieces.append(cur)
    return pieces or [""]


def segment(content: str, limit: int, *, gid: str) -> list[str]:
    """Split ``content`` into wire-framed chunk parts that each fit ``limit`` bytes.

    Returns the list of ready-to-send frame strings (with headers). The number of
    parts is computed so that, accounting for the (total-dependent) header width,
    every frame fits. Returns a single unframed-fit list only via the caller's
    :func:`needs_chunking` gate — this function always frames.
    """
    # The header width depends on ``total``, which depends on how many pieces we
    # make — a small fixed-point loop converges in 1–2 iterations.
    total = 1
    while True:
        budget = _payload_budget(limit, gid, total)
        pieces = _split_utf8(content, budget)
        if len(pieces) <= total:
            break
        total = len(pieces)
    return [
        f"{_CHUNK_PREFIX}{gid}{_SEP}{i}/{len(pieces)}{_SEP}{piece}"
        for i, piece in enumerate(pieces, start=1)
    ]


@dataclass(frozen=True)
class ChunkPart:
    """A parsed inbound chunk frame."""

    gid: str
    seq: int
    total: int
    payload: str


def parse_chunk(text: str) -> ChunkPart | None:
    """Parse a wire frame into a :class:`ChunkPart`, or None if it isn't one."""
    if not text.startswith(_CHUNK_PREFIX):
        return None
    body = text[len(_CHUNK_PREFIX) :]
    # gid | seq/total | payload   (payload may itself contain '|', so split 2x)
    try:
        gid, counts, payload = body.split(_SEP, 2)
        seq_s, total_s = counts.split("/", 1)
        seq, total = int(seq_s), int(total_s)
    except ValueError:
        return None
    if not gid or seq < 1 or total < 1 or seq > total:
        return None
    return ChunkPart(gid=gid, seq=seq, total=total, payload=payload)


def make_ack(gid: str, received: set[int], total: int) -> str:
    """Build an ACK frame reporting which sequence numbers arrived.

    The bitmap is a base36 integer whose bit ``seq-1`` is set when ``seq`` was
    received — compact even for many parts.
    """
    bits = 0
    for seq in received:
        if 1 <= seq <= total:
            bits |= 1 << (seq - 1)
    return f"{_ACK_PREFIX}{gid}{_SEP}{_to_base36(bits)}"


@dataclass(frozen=True)
class AckFrame:
    """A parsed inbound ACK frame."""

    gid: str
    received: frozenset[int]


def parse_ack(text: str) -> AckFrame | None:
    """Parse an ACK frame, or None if ``text`` isn't one."""
    if not text.startswith(_ACK_PREFIX):
        return None
    body = text[len(_ACK_PREFIX) :]
    try:
        gid, bitmap = body.split(_SEP, 1)
        bits = _from_base36(bitmap)
    except ValueError:
        return None
    if not gid:
        return None
    received = {i + 1 for i in range(bits.bit_length()) if bits >> i & 1}
    return AckFrame(gid=gid, received=frozenset(received))


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def _from_base36(s: str) -> int:
    return int(s, 36)


@dataclass
class _Pending:
    total: int
    parts: dict[int, str]
    created: float


@dataclass
class Reassembler:
    """Buffers inbound chunk parts until a full message can be rebuilt.

    Keyed by ``(source, gid)`` so two senders using the same gid never collide.
    Stale partials are dropped after :data:`REASSEMBLY_TTL_S`.
    """

    ttl_s: float = REASSEMBLY_TTL_S
    _buf: dict[tuple[str, str], _Pending] = field(default_factory=dict)

    def add(
        self, source: str, part: ChunkPart, *, now: float | None = None
    ) -> str | None:
        """Add a part; return the full reassembled content once complete, else None."""
        ts = time.monotonic() if now is None else now
        self._expire(ts)
        key = (source, part.gid)
        pending = self._buf.get(key)
        if pending is None:
            pending = _Pending(total=part.total, parts={}, created=ts)
            self._buf[key] = pending
        pending.parts[part.seq] = part.payload
        if len(pending.parts) >= pending.total:
            del self._buf[key]
            return "".join(pending.parts[i] for i in range(1, pending.total + 1))
        return None

    def received_seqs(self, source: str, gid: str) -> set[int]:
        """Sequence numbers received so far for a (source, gid) partial."""
        pending = self._buf.get((source, gid))
        return set(pending.parts) if pending else set()

    def _expire(self, now: float) -> None:
        stale = [k for k, p in self._buf.items() if now - p.created > self.ttl_s]
        for k in stale:
            del self._buf[k]

