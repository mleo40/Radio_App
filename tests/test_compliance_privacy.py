"""Tests for station identity, privacy stripping, and the HF encryption guard."""

from __future__ import annotations

import asyncio

import pytest

from radio_app.core.compliance import ComplianceGuard
from radio_app.core.filters import FilterAction, FilterEngine, FilterRule
from radio_app.core.groups import GroupRegistry
from radio_app.core.message import UnifiedMessage
from radio_app.core.privacy import apply_outbound_privacy
from radio_app.core.router import Router
from radio_app.core.station import Station
from radio_app.core.store import MessageStore
from radio_app.transports.base import Transport, TransportCapabilities


class HFTransport(Transport):
    name = "hf"

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=80,
            supports_addressing=True,
            supports_groups=True,
            supports_encryption=False,
            carries_operator_identity=True,
            prohibits_encryption=True,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: UnifiedMessage) -> bool:
        self.sent = msg
        return True


class AnonTransport(Transport):
    name = "anon"

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=100000,
            supports_addressing=True,
            supports_groups=True,
            supports_encryption=True,
            carries_operator_identity=False,
        )

    def local_identity(self):
        return "anon:deadbeef"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: UnifiedMessage) -> bool:
        self.sent = msg
        return True


# -- station validation ------------------------------------------------------


def test_station_validation():
    assert Station("N0CALL", "FN31pr").validate() == []
    assert "callsign is required" in Station("").validate()
    assert Station("N0CALL", "ZZ99").validate()  # bad grid -> non-empty problems


# -- privacy -----------------------------------------------------------------


def test_privacy_attaches_callsign_on_hf():
    msg = UnifiedMessage.direct("me", "K1ABC", "hi")
    msg.metadata = {"grid": "FN31", "callsign": "leak"}
    out = apply_outbound_privacy(msg, HFTransport(), Station("N0CALL", "FN31"))
    assert out.sender == "N0CALL"


def test_privacy_strips_pii_on_anonymous_transport():
    msg = UnifiedMessage.direct("N0CALL", "K1ABC", "hi")
    msg.metadata = {"grid": "FN31", "callsign": "N0CALL", "qth": "town"}
    out = apply_outbound_privacy(msg, AnonTransport(), Station("N0CALL", "FN31"))
    assert out.sender == "anon:deadbeef"
    assert "grid" not in out.metadata
    assert "callsign" not in out.metadata
    assert "qth" not in out.metadata
    # Original is untouched (per-transport copy).
    assert msg.metadata["callsign"] == "N0CALL"


# -- compliance guard --------------------------------------------------------


def test_guard_blocks_encrypted_over_hf_by_default():
    guard = ComplianceGuard(allow_encrypted_on_hf=False)
    msg = UnifiedMessage.direct("N0CALL", "K1ABC", "secret")
    msg.encrypt = True
    decision = guard.check_outbound(msg, HFTransport())
    assert decision.allowed is False


def test_guard_requires_confirmation_when_enabled():
    confirmed = ComplianceGuard(allow_encrypted_on_hf=True, confirm=lambda _w: True)
    denied = ComplianceGuard(allow_encrypted_on_hf=True, confirm=lambda _w: False)
    msg = UnifiedMessage.direct("N0CALL", "K1ABC", "secret")
    msg.encrypt = True
    assert confirmed.check_outbound(msg, HFTransport()).allowed is True
    assert denied.check_outbound(msg, HFTransport()).allowed is False


def test_plaintext_over_hf_is_allowed():
    guard = ComplianceGuard(allow_encrypted_on_hf=False)
    msg = UnifiedMessage.direct("N0CALL", "K1ABC", "plain")
    assert guard.check_outbound(msg, HFTransport()).allowed is True


# -- router integration ------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = MessageStore(tmp_path / "t.db")
    yield s
    s.close()


def _router(store, transports, station, guard):
    groups = GroupRegistry(subscriptions=set(), show_unsubscribed=True)
    filters = FilterEngine([FilterRule(action=FilterAction.SHOW)], groups)
    return Router(
        transports, store, groups, filters, station=station, compliance=guard
    )


def test_router_refuses_encrypted_hf_send(store):
    hf = HFTransport()
    asyncio.run(hf.start())
    guard = ComplianceGuard(allow_encrypted_on_hf=False)
    router = _router(store, [hf], Station("N0CALL"), guard)

    msg = UnifiedMessage.direct("N0CALL", "K1ABC", "secret")
    msg.encrypt = True
    ok = asyncio.run(router.send(msg, force_transport="hf"))
    assert ok is False
    assert not hasattr(hf, "sent")

