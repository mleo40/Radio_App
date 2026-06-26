"""Tests for JS8Call inbound event -> UnifiedMessage mapping (no sockets).

All addressing logic lives in the pure ``message_from_event`` helper, which is
what we exercise here (mirroring the socket-free ``_groups_in_line`` tests).
"""

from __future__ import annotations

from radio_app.core.message import AddressType
from radio_app.transports.js8call_transport import (
    _stable_msg_id,
    message_from_event,
)


def _directed(**params) -> dict:
    return {"type": "RX.DIRECTED", "params": params}


def test_direct_to_my_callsign():
    ev = _directed(FROM="KE7XYZ", TO="N0CALL", TEXT="hello there", SNR=-3)
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg is not None
    assert msg.address_type is AddressType.DIRECT
    assert msg.recipient == "N0CALL"
    assert msg.sender == "KE7XYZ"
    assert msg.content == "hello there"
    assert msg.metadata["snr"] == -3


def test_group_message_sets_group():
    ev = _directed(FROM="KE7XYZ", TO="@TTP", TEXT="net in 5")
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg.address_type is AddressType.GROUP
    assert msg.group == "TTP"
    assert msg.recipient is None


def test_allcall_is_broadcast():
    ev = _directed(FROM="KE7XYZ", TO="@ALLCALL", TEXT="cq cq")
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg.address_type is AddressType.BROADCAST
    assert msg.group is None and msg.recipient is None


def test_directed_to_other_station_is_overheard_not_direct():
    # Traffic from KE7XYZ to W1ABC (not us) must NOT land in our 1:1 thread with
    # KE7XYZ: it's overheard broadcast traffic, addressed to W1ABC.
    ev = _directed(FROM="KE7XYZ", TO="W1ABC", TEXT="ack")
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg.address_type is AddressType.BROADCAST
    assert msg.metadata.get("overheard") is True
    assert msg.metadata.get("to") == "W1ABC"
    # thread_key for a broadcast is @ALLCALL, never the sender's 1:1 thread.
    assert msg.thread_key != "KE7XYZ"


def test_bare_subscribed_group_treated_as_group():
    ev = _directed(FROM="KE7XYZ", TO="TTP", TEXT="hi")
    msg = message_from_event(ev, my_callsign="N0CALL", my_groups=("TTP",))
    assert msg.address_type is AddressType.GROUP
    assert msg.group == "TTP"


def test_target_folded_into_text_fallback():
    # Some builds omit TO and prefix the body with the target token.
    ev = {"type": "RX.DIRECTED", "params": {"FROM": "KE7XYZ", "TEXT": "@TTP rolling"}}
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg.address_type is AddressType.GROUP
    assert msg.group == "TTP"
    assert msg.content == "rolling"


def test_empty_text_returns_none():
    assert message_from_event(_directed(FROM="KE7XYZ", TO="N0CALL", TEXT="")) is None


def test_non_message_event_returns_none():
    assert message_from_event({"type": "RX.SPOT", "params": {"CALL": "KE7XYZ"}}) is None


def test_stable_id_uses_native_id_when_present():
    ev = _directed(FROM="KE7XYZ", TO="@TTP", TEXT="x", _ID=12345)
    a = message_from_event(ev)
    b = message_from_event(ev)
    assert a.msg_id == b.msg_id == "js8-12345"


def test_sentinel_id_minus_one_does_not_collapse_messages():
    # JS8Call stamps spontaneous RX events with _ID = -1 (a constant). Two
    # DIFFERENT replies must NOT share an id, or the router drops all but one.
    first = message_from_event(
        _directed(FROM="KE7XYZ", TO="N0CALL", TEXT="SNR -07", CMD="SNR", _ID=-1)
    )
    second = message_from_event(
        _directed(FROM="W1ABC", TO="N0CALL", TEXT="SNR -12", CMD="SNR", _ID=-1)
    )
    assert first.msg_id != second.msg_id
    assert first.msg_id != "js8--1"
    # The same frame seen twice still de-duplicates (same id).
    repeat = message_from_event(
        _directed(FROM="KE7XYZ", TO="N0CALL", TEXT="SNR -07", CMD="SNR", _ID=-1)
    )
    assert repeat.msg_id == first.msg_id


def test_zero_id_falls_back_to_hash():
    msg = message_from_event(
        _directed(FROM="KE7XYZ", TO="@TTP", TEXT="hi", _ID=0)
    )
    assert msg.msg_id != "js8-0"
    assert msg.msg_id.startswith("js8-")


def test_stable_id_is_deterministic_without_native_id():
    params = {"FROM": "KE7XYZ", "TO": "@TTP", "FREQ": 7078000}
    first = _stable_msg_id(params, "KE7XYZ", "@TTP", "same body")
    second = _stable_msg_id(params, "KE7XYZ", "@TTP", "same body")
    assert first == second
    other = _stable_msg_id(params, "KE7XYZ", "@TTP", "different body")
    assert first != other


# -- directed command parsing ------------------------------------------------


def test_snr_query_from_cmd_param():
    ev = _directed(FROM="KE7XYZ", TO="@TTP", TEXT="SNR?", CMD="SNR?")
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg.metadata["js8_command"] == "SNR?"
    assert msg.metadata["js8_query"] is True
    assert "snr_report" not in msg.metadata


def test_snr_response_parses_value():
    ev = _directed(FROM="KE7XYZ", TO="N0CALL", TEXT="N0CALL SNR -07", CMD="SNR")
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg.metadata["js8_command"] == "SNR"
    assert msg.metadata["js8_query"] is False
    assert msg.metadata["snr_report"] == -7


def test_command_detected_from_text_without_cmd_param():
    ev = _directed(FROM="KE7XYZ", TO="N0CALL", TEXT="GRID?")
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert msg.metadata["js8_command"] == "GRID?"
    assert msg.metadata["js8_query"] is True


def test_plain_chat_has_no_command():
    ev = _directed(FROM="KE7XYZ", TO="N0CALL", TEXT="see you at the net")
    msg = message_from_event(ev, my_callsign="N0CALL")
    assert "js8_command" not in msg.metadata


