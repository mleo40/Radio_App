"""Tests for the radio interlock — one HF radio, one transmitter.

Covers the pure ``RadioInterlock`` token plus the ``uses_shared_radio``
capability that decides which transports contend for the radio.
"""

from __future__ import annotations

from radio_app.core.radio_interlock import InterlockDecision, RadioInterlock
from radio_app.transports.js8call_transport import JS8CallTransport
from radio_app.transports.winlink_transport import WinlinkTransport


# -- pure interlock token -----------------------------------------------------

def test_noncontender_is_never_gated():
    il = RadioInterlock({"js8call", "winlink"})
    dec = il.acquire("reticulum")
    assert dec.granted is True
    assert dec.blocked_by is None
    assert il.holder is None  # a non-radio transport never takes the radio


def test_acquire_grants_when_free_and_is_idempotent():
    il = RadioInterlock({"js8call", "winlink"})
    assert il.acquire("js8call") == InterlockDecision(granted=True)
    assert il.holder == "js8call"
    # Re-acquiring by the same holder still succeeds.
    assert il.acquire("js8call").granted is True


def test_acquire_denied_when_another_holds_it():
    il = RadioInterlock({"js8call", "winlink"})
    il.acquire("js8call")
    dec = il.acquire("winlink")
    assert dec.granted is False
    assert dec.blocked_by == "js8call"
    assert il.holder == "js8call"  # holder unchanged on denial


def test_release_frees_the_radio_for_the_other():
    il = RadioInterlock({"js8call", "winlink"})
    il.acquire("js8call")
    il.release("js8call")
    assert il.holder is None
    assert il.acquire("winlink").granted is True


def test_release_by_non_holder_is_noop():
    il = RadioInterlock({"js8call", "winlink"})
    il.acquire("js8call")
    il.release("winlink")  # winlink doesn't hold it
    assert il.holder == "js8call"


def test_blocked_by_reports_the_conflict():
    il = RadioInterlock({"js8call", "winlink"})
    il.acquire("js8call")
    assert il.blocked_by("winlink") == "js8call"
    assert il.blocked_by("js8call") is None     # holder isn't blocked by itself
    assert il.blocked_by("reticulum") is None   # non-contender never blocked


def test_transfer_forces_handoff_and_returns_previous():
    il = RadioInterlock({"js8call", "winlink"})
    il.acquire("js8call")
    prev = il.transfer("winlink")
    assert prev == "js8call"
    assert il.holder == "winlink"


def test_transfer_to_noncontender_frees_the_radio():
    il = RadioInterlock({"js8call", "winlink"})
    il.acquire("js8call")
    prev = il.transfer("reticulum")  # moving to a non-radio mode
    assert prev == "js8call"
    assert il.holder is None


# -- capability wiring --------------------------------------------------------

def test_js8call_uses_shared_radio():
    assert JS8CallTransport({}).capabilities().uses_shared_radio is True


def test_winlink_telnet_does_not_use_radio():
    t = WinlinkTransport({"connect": "telnet"})
    assert t.capabilities().uses_shared_radio is False


def test_winlink_rf_method_uses_radio():
    t = WinlinkTransport({"connect": "varahf"})
    assert t.capabilities().uses_shared_radio is True


def test_winlink_auto_with_rf_fallback_uses_radio():
    t = WinlinkTransport(
        {"connect": "auto", "connect_order": ["telnet", "varahf"]}
    )
    # Auto contends for the radio because an RF path can be used.
    assert t.capabilities().uses_shared_radio is True


def test_winlink_auto_telnet_only_does_not_use_radio():
    t = WinlinkTransport({"connect": "auto", "connect_order": ["telnet"]})
    assert t.capabilities().uses_shared_radio is False

