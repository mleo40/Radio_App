"""Unit tests for the app-level chunking / reassembly / ACK primitives.

Pure and synchronous — no radio, no event loop. The router integration that
drives these over a transport is covered in test_chunking_router.py.
"""

from __future__ import annotations

from radio_app.core.chunking import (
    ChunkPart,
    Reassembler,
    group_id,
    make_ack,
    needs_chunking,
    parse_ack,
    parse_chunk,
    segment,
)


def test_needs_chunking_threshold():
    assert needs_chunking("x" * 81, 80) is True
    assert needs_chunking("x" * 80, 80) is False
    assert needs_chunking("", 80) is False
    # A zero/negative limit never reports "needs chunking" (guards bad config).
    assert needs_chunking("anything", 0) is False


def test_group_id_is_stable_and_short():
    mid = "a3f29c10ffee00112233445566778899"
    gid = group_id(mid)
    assert gid == "a3f29c"
    assert group_id(mid) == gid  # deterministic
    # Non-hex ids still yield a short token.
    assert 1 <= len(group_id("not-hex-id")) <= 6


def test_segment_parts_fit_byte_limit():
    content = "Hello world, this is a long message split across frames!" * 3
    limit = 40
    gid = group_id("deadbeef")
    parts = segment(content, limit, gid=gid)
    assert len(parts) > 1
    # Every framed part fits the transport's byte budget.
    for p in parts:
        assert len(p.encode("utf-8")) <= limit


def test_segment_roundtrips_through_reassembler():
    content = "The quick brown fox jumps over the lazy dog. " * 5
    gid = group_id("cafe1234")
    parts = segment(content, 50, gid=gid)
    r = Reassembler()
    out = None
    for raw in parts:
        part = parse_chunk(raw)
        assert part is not None
        out = r.add("KE7XYZ", part)
    assert out == content


def test_reassembler_handles_out_of_order_parts():
    content = "alpha-bravo-charlie-delta-echo-foxtrot-golf-hotel-india"
    gid = group_id("00ff00ff")
    parts = [parse_chunk(p) for p in segment(content, 24, gid=gid)]
    r = Reassembler()
    # Deliver reversed; reassembly is sequence-aware, not arrival-order.
    result = None
    for part in reversed(parts):
        result = r.add("N0CALL", part)
    assert result == content


def test_reassembler_isolates_sources_with_same_gid():
    gid = "shared"
    p1 = ChunkPart(gid=gid, seq=1, total=2, payload="AA")
    p2 = ChunkPart(gid=gid, seq=2, total=2, payload="BB")
    r = Reassembler()
    # Same gid from two different senders must not cross-contaminate.
    assert r.add("alice", p1) is None
    assert r.add("bob", p1) is None
    assert r.add("alice", p2) == "AABB"
    assert r.add("bob", p2) == "AABB"


def test_reassembler_expires_stale_partials():
    r = Reassembler(ttl_s=10.0)
    p1 = ChunkPart(gid="g", seq=1, total=2, payload="X")
    assert r.add("op", p1, now=0.0) is None
    # The second part arrives after the TTL — the partial was dropped, so this
    # starts a fresh (still incomplete) group rather than completing the old one.
    p2 = ChunkPart(gid="g", seq=2, total=2, payload="Y")
    assert r.add("op", p2, now=100.0) is None


def test_parse_chunk_rejects_non_frames():
    assert parse_chunk("just a normal message") is None
    assert parse_chunk("RC|onlygid") is None
    assert parse_chunk("RC|g|notcounts|body") is None
    assert parse_chunk("RC|g|3/2|body") is None  # seq > total
    assert parse_chunk("RC|g|0/2|body") is None  # seq < 1


def test_parse_chunk_preserves_pipes_in_payload():
    # The payload itself may contain '|', so only the first two seps are headers.
    part = parse_chunk("RC|abc|1/2|a|b|c")
    assert part is not None
    assert part.payload == "a|b|c"


def test_ack_roundtrip_reports_received_seqs():
    frame = make_ack("abc", {1, 3, 4}, total=4)
    parsed = parse_ack(frame)
    assert parsed is not None
    assert parsed.gid == "abc"
    assert parsed.received == frozenset({1, 3, 4})


def test_ack_full_set():
    frame = make_ack("g", {1, 2, 3}, total=3)
    parsed = parse_ack(frame)
    assert parsed is not None
    assert parsed.received == frozenset({1, 2, 3})


def test_ack_and_chunk_frames_are_distinguishable():
    chunk = segment("a big body that must be split into two", 28, gid="g")[0]
    assert parse_ack(chunk) is None
    ack = make_ack("g", {1}, total=2)
    assert parse_chunk(ack) is None


def test_received_seqs_tracks_progress():
    r = Reassembler()
    r.add("op", ChunkPart(gid="g", seq=1, total=3, payload="A"))
    r.add("op", ChunkPart(gid="g", seq=3, total=3, payload="C"))
    assert r.received_seqs("op", "g") == {1, 3}
    assert r.received_seqs("op", "missing") == set()

