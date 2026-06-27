"""Core tests that run without any radio hardware or external apps.

A small in-memory FakeTransport exercises the full router path: best-transport
selection, group fan-out, inbound filtering, de-duplication and persistence.
"""

from __future__ import annotations

import asyncio

import pytest

from radio_app.core.filters import FilterAction, FilterEngine, FilterRule
from radio_app.core.groups import Group, GroupRegistry
from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.core.router import Router
from radio_app.core.selector import SelectionMode
from radio_app.core.store import MessageStore
from radio_app.transports.base import Transport, TransportCapabilities


class FakeTransport(Transport):
    name = "fake"

    def __init__(self, config=None, *, encrypted=True, latency=1.0):
        super().__init__(config or {})
        self._encrypted = encrypted
        self._latency = latency
        self.sent: list[UnifiedMessage] = []

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=100_000,
            supports_addressing=True,
            supports_groups=True,
            supports_encryption=self._encrypted,
            supports_delivery_confirmation=True,
            is_realtime=True,
            typical_latency_s=self._latency,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: UnifiedMessage) -> bool:
        self.sent.append(msg)
        return True

    async def receive(self, msg: UnifiedMessage) -> None:
        await self._emit(msg)


@pytest.fixture()
def store(tmp_path):
    s = MessageStore(tmp_path / "test.db")
    yield s
    s.close()


def _make_router(store, transports, rules=None, subs=None):
    groups = GroupRegistry(
        groups={"EMS": Group("EMS", transports=["fake"])},
        subscriptions=subs if subs is not None else {"EMS"},
        show_unsubscribed=False,
    )
    rules = rules or [FilterRule(action=FilterAction.SHOW)]
    filters = FilterEngine(rules, groups)
    return Router(transports, store, groups, filters)


def test_direct_send_persists_and_selects(store):
    t = FakeTransport()
    router = _make_router(store, [t])
    asyncio.run(t.start())

    msg = UnifiedMessage.direct("me", "N0CALL", "hello")
    ok = asyncio.run(router.send(msg))

    assert ok is True
    assert len(t.sent) == 1
    threads = store.threads()
    assert any(key == "N0CALL" for key, _, _ in threads)


def test_forced_send_persists_transport_for_thread_scoping(store):
    """A forced send (the TUI path) must persist the transport.

    Regression: outbound messages were saved with an empty transport, so the
    thread-list filter (which scopes conversations by transport) hid them - only
    threads that also had an inbound message were visible.
    """
    t = FakeTransport()
    asyncio.run(t.start())
    router = _make_router(store, [t])

    msg = UnifiedMessage.direct("me", "a1b2c3d4e5f60718", "hi")
    ok = asyncio.run(router.send(msg, force_transport="fake"))

    assert ok is True
    # Persisted with the carrying transport, so thread_transport resolves it.
    assert store.thread_transport(msg.thread_key) == "fake"
    stored = store.read_thread(msg.thread_key)
    assert stored and stored[0].transport == "fake"


def test_auto_send_persists_transport_on_success(store):
    t = FakeTransport()
    asyncio.run(t.start())
    router = _make_router(store, [t])

    msg = UnifiedMessage.direct("me", "N0CALL", "hello")
    asyncio.run(router.send(msg))  # auto-select, no force

    assert store.thread_transport("N0CALL") == "fake"


def test_delete_thread_removes_conversation(store):
    a = UnifiedMessage.direct("me", "N0CALL", "one")
    b = UnifiedMessage.direct("me", "N0CALL", "two")
    other = UnifiedMessage.direct("me", "W1AW", "hi")
    for m in (a, b, other):
        store.save(m)
    removed = store.delete_thread("N0CALL")
    assert removed == 2
    assert store.read_thread("N0CALL") == []
    # Other conversations are untouched.
    assert len(store.read_thread("W1AW")) == 1
    # Deleting a non-existent thread is a harmless no-op.
    assert store.delete_thread("nobody") == 0


def test_read_transport_returns_all_mode_messages_oldest_first(store):
    """read_transport() powers the 'all messages' (no-conversation) firehose."""
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    # Two js8call threads plus an unrelated meshcore message.
    msgs = [
        UnifiedMessage(sender="W1AW", content="first", transport="js8call",
                       recipient="me", timestamp=base),
        UnifiedMessage(sender="N0CALL", content="second", transport="js8call",
                       recipient="me", timestamp=base + timedelta(minutes=1)),
        UnifiedMessage(sender="bbbb", content="mesh", transport="meshcore",
                       recipient="me", timestamp=base + timedelta(minutes=2)),
    ]
    for m in msgs:
        store.save(m)
    js8 = store.read_transport("js8call")
    # Only js8call messages, oldest-first.
    assert [m.content for m in js8] == ["first", "second"]
    assert all(m.transport == "js8call" for m in js8)
    # A transport with nothing stored yields an empty list.
    assert store.read_transport("reticulum") == []



def test_secure_mode_rejects_plaintext(store):
    plaintext = FakeTransport(encrypted=False)
    asyncio.run(plaintext.start())
    router = _make_router(store, [plaintext])

    msg = UnifiedMessage.direct("me", "N0CALL", "secret")
    ok = asyncio.run(router.send(msg, mode=SelectionMode.SECURE))

    assert ok is False  # no encrypted transport available
    assert plaintext.sent == []


def test_group_fanout(store):
    t = FakeTransport()
    asyncio.run(t.start())
    router = _make_router(store, [t])

    msg = UnifiedMessage.to_group("me", "EMS", "net in 5")
    ok = asyncio.run(router.send(msg))

    assert ok is True
    assert t.sent[0].group == "EMS"


def test_inbound_dedup(store):
    t = FakeTransport()
    asyncio.run(t.start())
    # The router registers an inbound callback on the transport in __init__.
    _make_router(store, [t])

    msg = UnifiedMessage(sender="N0CALL", content="hi", address_type=AddressType.DIRECT)

    async def drive():
        await t.receive(msg)
        await t.receive(msg)  # same msg_id -> duplicate

    asyncio.run(drive())
    # Only stored once.
    assert len(store.read_thread("N0CALL")) == 1


def test_subscription_filter_drops_unsubscribed_group(store):
    t = FakeTransport()
    asyncio.run(t.start())
    # Subscribed to nothing; router registers the inbound filter callback.
    _make_router(store, [t], subs=set())

    msg = UnifiedMessage.to_group("N0CALL", "EMS", "should be dropped")

    asyncio.run(t.receive(msg))
    assert store.read_thread("@EMS") == []


def test_numeric_channel_bypasses_subscription_gate(store):
    """MeshCore channel tags (@0/@2) are device channels, not opt-in groups.

    Regression: inbound channel replies were dropped by the named-group
    subscription gate, so they never reached the store (missing from the channel
    view) while still leaking into the Watch feed. Numeric channel indices must
    bypass that gate and be persisted under their @<index> thread.
    """
    t = FakeTransport()
    asyncio.run(t.start())
    # Subscribed to nothing, show_unsubscribed off (the strict default).
    _make_router(store, [t], subs=set())

    msg = UnifiedMessage.to_group("peerkey", "2", "reply on the ops channel")

    asyncio.run(t.receive(msg))
    stored = store.read_thread("@2")
    assert [m.content for m in stored] == ["reply on the ops channel"]


