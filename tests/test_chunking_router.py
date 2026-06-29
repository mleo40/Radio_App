"""Router integration tests for app-level chunking + ACK/retry.

A fake small-MTU transport records the frames the router hands it and can inject
inbound frames, so the whole split → reassemble → ACK → retransmit loop is
exercised without any radio or event-loop trickery beyond a short sleep.
"""

from __future__ import annotations

import asyncio

import pytest

from radio_app.core.chunking import (
    group_id,
    make_ack,
    parse_ack,
    parse_chunk,
    segment,
)
from radio_app.core.filters import FilterAction, FilterEngine, FilterRule
from radio_app.core.groups import GroupRegistry
from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.core.router import Router
from radio_app.core.store import MessageStore
from radio_app.transports.base import Transport, TransportCapabilities


class SmallMtuTransport(Transport):
    name = "smallmtu"

    def __init__(self, config=None, *, limit=40, confirms=False):
        super().__init__(config or {})
        self._limit = limit
        self._confirms = confirms
        self.sent: list[UnifiedMessage] = []
        self.drop_first_n = 0  # drop this many initial sends (simulate loss)

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=self._limit,
            supports_addressing=True,
            supports_groups=True,
            supports_encryption=False,
            supports_delivery_confirmation=self._confirms,
            supports_chunking=True,
            carries_operator_identity=True,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: UnifiedMessage) -> bool:
        if self.drop_first_n > 0:
            self.drop_first_n -= 1
            return False
        self.sent.append(msg)
        return True

    async def receive(self, msg: UnifiedMessage) -> None:
        await self._emit(msg)


@pytest.fixture()
def store(tmp_path):
    s = MessageStore(tmp_path / "chunk.db")
    yield s
    s.close()


def _router(store, transport, **kw):
    groups = GroupRegistry(groups={}, subscriptions=set(), show_unsubscribed=True)
    filters = FilterEngine([FilterRule(action=FilterAction.SHOW)], groups)
    return Router([transport], store, groups, filters, **kw)


def _chunk_frames(transport) -> list[str]:
    return [m.content for m in transport.sent if parse_chunk(m.content) is not None]


def test_long_direct_message_is_split_into_fitting_frames(store):
    t = SmallMtuTransport(limit=40, confirms=True)
    router = _router(store, t)
    asyncio.run(t.start())

    body = "This is a fairly long message that will not fit in one frame. " * 2
    msg = UnifiedMessage.direct("me", "N0CALL", body)
    ok = asyncio.run(router.send(msg, force_transport="smallmtu"))

    assert ok is True
    frames = _chunk_frames(t)
    assert len(frames) > 1
    for f in frames:
        assert len(f.encode("utf-8")) <= 40
    # The frames reassemble back to the original body.
    parts = sorted((parse_chunk(f) for f in frames), key=lambda p: p.seq)
    assert "".join(p.payload for p in parts) == body


def test_short_message_is_not_chunked(store):
    t = SmallMtuTransport(limit=40, confirms=True)
    router = _router(store, t)
    asyncio.run(t.start())

    msg = UnifiedMessage.direct("me", "N0CALL", "short")
    asyncio.run(router.send(msg, force_transport="smallmtu"))

    assert len(t.sent) == 1
    assert parse_chunk(t.sent[0].content) is None  # sent verbatim, no header


def test_inbound_chunks_reassemble_into_one_message(store):
    t = SmallMtuTransport(limit=40, confirms=True)
    captured: list[UnifiedMessage] = []
    router = _router(store, t)
    router.add_ui_callback(lambda m, a: captured.append(m))
    asyncio.run(t.start())

    body = "Reassemble me across several little radio frames please, thanks!" * 2

    async def drive():
        # Build frames exactly as the sender would, then inject them inbound.
        frames = segment(body, 40, gid=group_id("0011223344556677"))
        for frame in frames:
            await t.receive(
                UnifiedMessage(
                    sender="KE7XYZ",
                    content=frame,
                    address_type=AddressType.DIRECT,
                    recipient="me",
                )
            )

    asyncio.run(drive())

    # Exactly one fully-reassembled message surfaced and got persisted.
    full = [m for m in captured if m.content == body]
    assert len(full) == 1
    assert store.exists(full[0].msg_id)


def test_partial_chunks_do_not_surface(store):
    t = SmallMtuTransport(limit=40, confirms=True)
    captured: list[UnifiedMessage] = []
    router = _router(store, t)
    router.add_ui_callback(lambda m, a: captured.append(m))
    asyncio.run(t.start())

    async def drive():
        # Only the first of two parts arrives.
        await t.receive(
            UnifiedMessage(
                sender="KE7XYZ",
                content="RC|abcd12|1/2|hello ",
                address_type=AddressType.DIRECT,
                recipient="me",
            )
        )

    asyncio.run(drive())
    assert captured == []  # nothing shown until the group is complete


def test_receiver_acks_when_no_native_confirmation(store):
    # confirms=False → the router should emit an ACK frame back to the sender.
    t = SmallMtuTransport(limit=40, confirms=False)
    _router(store, t)  # wires the router's on_receive into the transport
    asyncio.run(t.start())

    async def drive():
        await t.receive(
            UnifiedMessage(
                sender="KE7XYZ",
                content="RC|feed01|1/2|hello ",
                address_type=AddressType.DIRECT,
                recipient="me",
            )
        )
        await asyncio.sleep(0.05)  # let the scheduled ACK task run

    asyncio.run(drive())
    acks = [parse_ack(m.content) for m in t.sent if parse_ack(m.content)]
    assert acks
    assert acks[-1].gid == "feed01"
    assert 1 in acks[-1].received


def test_receiver_does_not_ack_when_medium_confirms(store):
    t = SmallMtuTransport(limit=40, confirms=True)
    _router(store, t)  # wires on_receive
    asyncio.run(t.start())

    async def drive():
        await t.receive(
            UnifiedMessage(
                sender="KE7XYZ",
                content="RC|aa11bb|1/2|hello ",
                address_type=AddressType.DIRECT,
                recipient="me",
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(drive())
    assert not any(parse_ack(m.content) for m in t.sent)


def test_unacked_chunks_are_retransmitted(store):
    t = SmallMtuTransport(limit=40, confirms=False)
    router = _router(store, t, ack_timeout=0.02, ack_retries=2)
    asyncio.run(t.start())

    async def drive():
        body = "Retransmit this message because no ACK ever comes back here." * 2
        await router.send(
            UnifiedMessage.direct("me", "N0CALL", body),
            force_transport="smallmtu",
        )
        first_round = len(_chunk_frames(t))
        await asyncio.sleep(0.05)  # past one ack_timeout with no ACK
        return first_round

    first_round = asyncio.run(drive())
    assert len(_chunk_frames(t)) > first_round  # parts were resent


def test_full_ack_stops_retransmission(store):
    t = SmallMtuTransport(limit=40, confirms=False)
    router = _router(store, t, ack_timeout=0.03, ack_retries=3)
    asyncio.run(t.start())

    async def drive():
        body = "Stop resending once the peer acknowledges every single part now." * 2
        msg = UnifiedMessage.direct("me", "N0CALL", body)
        await router.send(msg, force_transport="smallmtu")
        frames = [parse_chunk(f) for f in _chunk_frames(t)]
        total = frames[0].total
        gid = frames[0].gid
        # Peer ACKs all parts before the first retry timer fires.
        await t.receive(
            UnifiedMessage(
                sender="N0CALL",
                content=make_ack(gid, set(range(1, total + 1)), total),
                address_type=AddressType.DIRECT,
                recipient="me",
            )
        )
        count_after_ack = len(_chunk_frames(t))
        await asyncio.sleep(0.12)  # well past several ack_timeouts
        return count_after_ack

    count_after_ack = asyncio.run(drive())
    assert len(_chunk_frames(t)) == count_after_ack  # no retransmits after full ACK


def test_late_duplicate_group_is_not_shown_twice(store):
    t = SmallMtuTransport(limit=40, confirms=True)
    captured: list[UnifiedMessage] = []
    router = _router(store, t)
    router.add_ui_callback(lambda m, a: captured.append(m))
    asyncio.run(t.start())

    def part(seq, total, payload):
        return UnifiedMessage(
            sender="KE7XYZ",
            content=f"RC|dup001|{seq}/{total}|{payload}",
            address_type=AddressType.DIRECT,
            recipient="me",
        )

    async def drive():
        await t.receive(part(1, 2, "foo "))
        await t.receive(part(2, 2, "bar"))
        # A late duplicate of an already-completed group.
        await t.receive(part(2, 2, "bar"))

    asyncio.run(drive())
    full = [m for m in captured if m.content == "foo bar"]
    assert len(full) == 1

