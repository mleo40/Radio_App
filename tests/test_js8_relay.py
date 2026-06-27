"""Tests for JS8Call store-and-forward inbox and directed-command sending.

The inbox parsing is pure; the send paths are exercised by injecting a fake
StreamWriter so we can assert the exact JS8Call API JSON without a socket.
"""

from __future__ import annotations

import asyncio
import json

from radio_app.transports.js8call_transport import (
    JS8_DIRECTED_COMMANDS,
    JS8CallTransport,
)


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass


def _running() -> tuple[JS8CallTransport, _FakeWriter]:
    t = JS8CallTransport({})
    t._running = True
    w = _FakeWriter()
    t._writer = w  # type: ignore[assignment]
    return t, w


def _last(w: _FakeWriter) -> dict:
    return json.loads(w.sent[-1].decode())


# -- inbox parsing (pure) -----------------------------------------------------

def test_update_inbox_normalises_nested_and_flat():
    t = JS8CallTransport({})
    t._update_inbox({"MESSAGES": [
        {"params": {"FROM": "w1aw", "TO": "n0call",
                    "TEXT": "hi", "UTC": 111, "_ID": 5}},
        {"FROM": "k2xyz", "TO": "n0call", "MESSAGE": "yo"},
    ]})
    msgs = t.inbox_messages()
    assert msgs[0] == {
        "id": "5", "from": "W1AW", "to": "N0CALL", "text": "hi", "utc": 111
    }
    assert msgs[1]["from"] == "K2XYZ" and msgs[1]["text"] == "yo"


def test_update_inbox_ignores_malformed():
    t = JS8CallTransport({})
    t._update_inbox({"MESSAGES": "nope"})
    t._update_inbox({})
    assert t.inbox_messages() == []


def test_inbox_messages_event_is_cached():
    t = JS8CallTransport({})
    asyncio.run(t._handle_event({
        "type": "INBOX.MESSAGES",
        "params": {"MESSAGES": [{"FROM": "a", "TO": "b", "TEXT": "c"}]},
    }))
    assert t.inbox_messages()[0]["text"] == "c"


# -- directed commands --------------------------------------------------------

def test_send_directed_command_validates_and_transmits():
    t, w = _running()
    assert asyncio.run(t.send_directed_command("w1aw", "snr?")) is True
    assert _last(w) == {"type": "TX.SEND_MESSAGE", "value": "W1AW SNR?"}


def test_send_directed_command_to_group():
    t, w = _running()
    assert asyncio.run(t.send_directed_command("@EMS", "HEARING?")) is True
    assert _last(w)["value"] == "@EMS HEARING?"


def test_send_directed_command_rejects_unknown():
    t, w = _running()
    assert asyncio.run(t.send_directed_command("w1aw", "bogus")) is False
    assert w.sent == []  # nothing transmitted


def test_directed_command_set_includes_common_queries():
    for cmd in ("SNR?", "GRID?", "INFO?", "HEARING?", "73"):
        assert cmd in JS8_DIRECTED_COMMANDS


# -- store-and-forward relay --------------------------------------------------

def test_store_relay_message_builds_inbox_store():
    t, w = _running()
    assert asyncio.run(t.store_relay_message("w1aw", "meet at noon")) is True
    payload = _last(w)
    assert payload["type"] == "INBOX.STORE_MESSAGE"
    assert payload["params"] == {"CALLSIGN": "W1AW", "TEXT": "meet at noon"}


def test_store_relay_message_rejects_empty():
    t, w = _running()
    assert asyncio.run(t.store_relay_message("", "x")) is False
    assert asyncio.run(t.store_relay_message("w1aw", "   ")) is False
    assert w.sent == []


def test_request_inbox_sends_get_messages():
    t, w = _running()
    assert asyncio.run(t.request_inbox()) is True
    assert _last(w)["type"] == "INBOX.GET_MESSAGES"


def test_methods_return_false_when_not_running():
    t = JS8CallTransport({})  # never started -> no writer
    assert asyncio.run(t.request_inbox()) is False
    assert asyncio.run(t.send_directed_command("w1aw", "SNR?")) is False
    assert asyncio.run(t.store_relay_message("w1aw", "hi")) is False

