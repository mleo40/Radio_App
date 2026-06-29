"""Tests for the JS8Call -> SMS (APRS SMSGTE gateway) feature.

The formatter is pure; the transport send path is exercised with a fake
StreamWriter so we can assert the exact JS8Call API JSON without a socket.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from radio_app.transports.js8call_transport import (
    JS8CallTransport,
    format_js8_sms,
    normalize_phone,
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


# -- phone normalisation (pure) ----------------------------------------------

def test_normalize_phone_strips_formatting():
    assert normalize_phone("(202) 555-0133") == "2025550133"


def test_normalize_phone_keeps_leading_plus():
    assert normalize_phone("+1 202-555-0133") == "+12025550133"


def test_normalize_phone_rejects_empty():
    with pytest.raises(ValueError):
        normalize_phone("no-digits-here!")


# -- SMS formatting (pure) ----------------------------------------------------

def test_format_js8_sms_builds_aprs_gateway_value():
    value = format_js8_sms("2025550133", "on my way")
    # @APRSIS CMD :<addressee padded to 9>:@<phone> <text>
    assert value == "@APRSIS CMD :SMSGTE   :@2025550133 on my way"


def test_format_js8_sms_addressee_is_padded_to_nine():
    value = format_js8_sms("12025550133", "hi")
    addressee = value.split(":", 2)[1]
    assert addressee == "SMSGTE   "  # 6 + 3 spaces = 9 chars
    assert len(addressee) == 9


def test_format_js8_sms_collapses_whitespace():
    value = format_js8_sms("2025550133", "  hello\n  there  ")
    assert value.endswith(":@2025550133 hello there")


def test_format_js8_sms_rejects_empty_text():
    with pytest.raises(ValueError):
        format_js8_sms("2025550133", "   ")


def test_format_js8_sms_rejects_empty_phone():
    with pytest.raises(ValueError):
        format_js8_sms("", "hello")


# -- transport send path ------------------------------------------------------

def test_send_sms_transmits_via_send_message():
    t, w = _running()
    assert asyncio.run(t.send_sms("(202) 555-0133", "running late")) is True
    payload = _last(w)
    assert payload["type"] == "TX.SEND_MESSAGE"
    assert payload["value"] == "@APRSIS CMD :SMSGTE   :@2025550133 running late"


def test_send_sms_rejects_bad_input():
    t, w = _running()
    assert asyncio.run(t.send_sms("", "hi")) is False
    assert asyncio.run(t.send_sms("2025550133", "   ")) is False
    assert w.sent == []  # nothing transmitted


def test_send_sms_false_when_not_running():
    t = JS8CallTransport({})  # never started -> no writer
    assert asyncio.run(t.send_sms("2025550133", "hi")) is False

