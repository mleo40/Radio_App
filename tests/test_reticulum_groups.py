"""Tests for Reticulum group / broadcast channels.

Most of these need neither RNS nor radio hardware: the shared-key derivation and
payload codec are pure stdlib, and the send/receive routing is exercised by
stubbing the RNS-touching hooks (``_transmit_group`` / ``_dispatch_to_loop``).
The one path that genuinely needs RNS classes is guarded with ``importorskip``.
"""

from __future__ import annotations

import asyncio

from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.transports.reticulum_transport import (
    _BROADCAST_GROUP,
    ReticulumTransport,
    decode_group_payload,
    encode_group_payload,
    group_shared_key,
)


class _FakeDest:
    """Minimal stand-in for our local LXMF delivery destination."""

    def __init__(self, hex_hash: str) -> None:
        self.hash = bytes.fromhex(hex_hash)


# -- pure helpers -------------------------------------------------------------

def test_group_shared_key_is_deterministic_and_name_normalised():
    a = group_shared_key("EMS")
    assert len(a) == 32
    # Leading '@', case and surrounding space don't change the channel.
    assert group_shared_key("@ems") == a
    assert group_shared_key("  EmS ") == a
    # Different channel -> different key.
    assert group_shared_key("emsne") != a


def test_encode_decode_group_payload_round_trips():
    raw = encode_group_payload("aa" * 16, "Alice", "net in 5")
    parsed = decode_group_payload(raw)
    assert parsed == {"sender": "aa" * 16, "name": "Alice", "content": "net in 5"}


def test_decode_group_payload_rejects_garbage():
    assert decode_group_payload(b"not json") is None
    assert decode_group_payload(b'{"no":"content"}') is None
    assert decode_group_payload("a string, not bytes") is None
    assert decode_group_payload(b'["a","list"]') is None


# -- capabilities / identity --------------------------------------------------

def test_capabilities_advertise_groups_and_broadcast():
    caps = ReticulumTransport({}).capabilities()
    assert caps.supports_groups is True
    assert caps.supports_broadcast is True


def test_set_identity_normalises_and_dedupes_group_names():
    t = ReticulumTransport({})
    t.set_identity("N0CALL", ("@EMS", "EMS", "emsNE"))
    assert t._group_names == ("ems", "emsne")


# -- inbound (received GROUP packet -> UnifiedMessage) ------------------------

def _capture(config=None):
    t = ReticulumTransport(config or {})
    t._local_destination = _FakeDest("ab" * 16)
    captured: list[UnifiedMessage] = []
    t._dispatch_to_loop = captured.append  # type: ignore[assignment]
    return t, captured


def test_received_group_packet_builds_group_message():
    t, captured = _capture()
    payload = encode_group_payload("cc" * 16, "Carol", "hello group")
    t._group_packet_received("ems", payload, object())
    assert len(captured) == 1
    msg = captured[0]
    assert msg.address_type is AddressType.GROUP
    assert msg.group == "ems"
    assert msg.sender == "cc" * 16
    assert msg.content == "hello group"
    assert msg.metadata["display_name"] == "Carol"


def test_received_broadcast_packet_is_broadcast_not_group():
    t, captured = _capture()
    payload = encode_group_payload("cc" * 16, "Carol", "CQ CQ")
    t._group_packet_received(_BROADCAST_GROUP, payload, object())
    assert captured[0].address_type is AddressType.BROADCAST
    assert captured[0].group is None


def test_received_group_packet_ignores_our_own_echo():
    t, captured = _capture()
    # Sender == our local destination hash -> dropped (no echo).
    payload = encode_group_payload("ab" * 16, "Me", "loopback")
    t._group_packet_received("ems", payload, object())
    assert captured == []


def test_received_group_packet_ignores_garbage():
    t, captured = _capture()
    t._group_packet_received("ems", b"not a payload", object())
    assert captured == []


# -- outbound (send group/broadcast) -----------------------------------------

def _sendable(config=None):
    t, _ = _capture(config)
    t._running = True
    t._lxmf = object()
    sent: list[tuple[str, bytes]] = []

    def fake_transmit(norm, payload):
        sent.append((norm, payload))
        return True

    t._transmit_group = fake_transmit  # type: ignore[assignment]
    return t, sent


def test_send_group_encodes_and_routes_to_named_channel():
    t, sent = _sendable({"display_name": "Bob"})
    msg = UnifiedMessage.to_group("me", "EMS", "meet at noon")
    assert asyncio.run(t.send(msg)) is True
    assert len(sent) == 1
    norm, payload = sent[0]
    assert norm == "ems"
    parsed = decode_group_payload(payload)
    assert parsed["content"] == "meet at noon"
    assert parsed["sender"] == "ab" * 16   # our local hash
    assert parsed["name"] == "Bob"


def test_send_broadcast_routes_to_reserved_channel():
    t, sent = _sendable()
    msg = UnifiedMessage(
        sender="me", content="all stations", address_type=AddressType.BROADCAST
    )
    assert asyncio.run(t.send(msg)) is True
    assert sent[0][0] == _BROADCAST_GROUP


def test_send_group_rejects_oversize_payload():
    t, sent = _sendable()
    msg = UnifiedMessage.to_group("me", "EMS", "x" * 500)  # over the packet MDU
    assert asyncio.run(t.send(msg)) is False
    assert sent == []   # never handed to the radio


def test_send_when_not_running_returns_false():
    t, _ = _capture()
    msg = UnifiedMessage.to_group("me", "EMS", "hi")
    assert asyncio.run(t.send(msg)) is False


# -- is_reachable (needs RNS classes) ----------------------------------------

def test_is_reachable_true_for_group_when_up():
    import pytest

    pytest.importorskip("RNS")
    t = ReticulumTransport({})
    t._running = True
    grp = UnifiedMessage.to_group("me", "EMS", "hi")
    bcast = UnifiedMessage(
        sender="me", content="x", address_type=AddressType.BROADCAST
    )
    assert t.is_reachable(grp) is True
    assert t.is_reachable(bcast) is True


# -- real RNS integration: shared channel + encryption (needs RNS) -----------

def test_real_group_channel_shared_hash_and_crypto(tmp_path):
    """Exercise the real RNS GROUP destinations end-to-end (no interfaces).

    A node's IN and OUT destinations for a channel are both derived purely from
    the channel name, so they share one hash (any other node deriving from the
    same name lands on the same channel), and a payload encrypted on OUT is
    decrypted on IN with the shared key.
    """
    import pytest

    RNS = pytest.importorskip("RNS")
    cfg = tmp_path / "rns"
    cfg.mkdir()
    (cfg / "config").write_text(
        "[reticulum]\n  enable_transport = False\n"
        "[logging]\n  loglevel = 1\n[interfaces]\n"
    )
    RNS.Reticulum(configdir=str(cfg))

    class _Dest:
        hash = bytes.fromhex("ab" * 16)

    t = ReticulumTransport({"display_name": "Bob"})
    t._local_destination = _Dest()

    out = t._group_out_destination("ems")     # creates OUT (+ joins IN)
    din = t._groups_in["ems"]                  # the IN from the internal join
    assert out is not None and din is not None
    assert out.hash == din.hash                # deterministic shared channel

    payload = encode_group_payload("ab" * 16, "Bob", "net in 5")
    parsed = decode_group_payload(din.decrypt(out.encrypt(payload)))
    assert parsed == {"sender": "ab" * 16, "name": "Bob", "content": "net in 5"}





