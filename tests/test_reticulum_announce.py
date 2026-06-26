"""Tests for Reticulum announce classification (peer vs site)."""

import pytest

RNS = pytest.importorskip("RNS")  # transport module needs RNS/LXMF importable

from radio_app.transports.reticulum_transport import (  # noqa: E402
    _AnnounceHandler,
    _NodeAnnounceHandler,
    _PeerAnnounceHandler,
)


class _FakeTransport:
    """Minimal stand-in capturing what the announce handlers do."""

    name = "reticulum"

    def __init__(self):
        self._nodes = {}
        self._peers = {}
        self.dispatched = []

    def _dispatch_to_loop(self, msg):
        self.dispatched.append(msg)


def _hash(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr)


def test_peer_handler_records_peer():
    t = _FakeTransport()
    h = _PeerAnnounceHandler(t)
    dest = "aa" * 16
    h.received_announce(_hash(dest), None, b"Alice")
    assert dest in t._peers
    assert t._peers[dest]["name"] == "Alice"


def test_node_handler_records_site():
    t = _FakeTransport()
    h = _NodeAnnounceHandler(t)
    dest = "bb" * 16
    h.received_announce(_hash(dest), None, b"Chat-Hispano")
    assert dest in t._nodes
    assert t._nodes[dest]["name"] == "Chat-Hispano"


def test_generic_handler_tags_peer_aspect():
    t = _FakeTransport()
    dest = "cc" * 16
    t._peers[dest] = {"name": "Bob", "last_seen": None}
    _AnnounceHandler(t).received_announce(_hash(dest), None, b"Bob")
    assert len(t.dispatched) == 1
    assert t.dispatched[0].metadata["aspect"] == "peer"


def test_generic_handler_tags_site_aspect():
    t = _FakeTransport()
    dest = "dd" * 16
    t._nodes[dest] = {"name": "SiteX", "last_seen": None}
    _AnnounceHandler(t).received_announce(_hash(dest), None, b"SiteX")
    assert t.dispatched[0].metadata["aspect"] == "site"


def test_generic_handler_tags_other_aspect_when_unknown():
    t = _FakeTransport()
    dest = "ee" * 16
    _AnnounceHandler(t).received_announce(_hash(dest), None, b"")
    assert t.dispatched[0].metadata["aspect"] == "other"


def test_aspect_filters_are_distinct():
    assert _PeerAnnounceHandler.aspect_filter == "lxmf.delivery"
    assert _NodeAnnounceHandler.aspect_filter == "nomadnetwork.node"


def test_classify_dest_distinguishes_node_peer_unknown():
    from radio_app.transports.reticulum_transport import ReticulumTransport

    t = ReticulumTransport({})
    site = "aa" * 16
    peer = "bb" * 16
    t._nodes[site] = {"name": "SiteX", "last_seen": None}
    t._peers[peer] = {"name": "Alice", "last_seen": None}
    assert t.classify_dest(site) == "node"
    assert t.classify_dest(peer) == "peer"
    # Prefix-tolerant in both directions (short <-> full).
    assert t.classify_dest(site[:12]) == "node"
    assert t.classify_dest(peer[:12]) == "peer"
    # A hash we've never heard is genuinely ambiguous.
    assert t.classify_dest("cc" * 16) == ""
    assert t.classify_dest("") == ""



