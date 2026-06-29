"""Tests for the WSJT-X transport.

Pure-function tests (no sockets):
  - Binary decode parsing (Decode and Status datagrams)
  - FT8 addressing logic (CQ, direct, overheard)
  - decode_to_message mapping
  - build_freetext_datagram round-trip

Transport-class tests (fake UDP):
  - start/stop lifecycle
  - inbound datagram → UnifiedMessage via callback
  - outbound send → datagram bytes
  - callsign learned from Status
"""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import MagicMock

from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.transports.base import ReachabilityStatus
from radio_app.transports.wsjt_x_transport import (
    _MSG_DECODE,
    _MSG_FREETEXT,
    _MSG_STATUS,
    MAGIC,
    MAX_CONTENT,
    SCHEMA,
    WsjtXTransport,
    _DecodeEvent,
    build_freetext_datagram,
    decode_to_message,
    parse_decode,
    parse_status,
)

# ── Helpers: build test datagrams ─────────────────────────────────────────────


def _qstring(s: str | None) -> bytes:
    if s is None:
        return struct.pack(">I", 0xFFFFFFFF)
    encoded = s.encode("utf-16-be")
    return struct.pack(">I", len(encoded)) + encoded


def _qbytearray(b: bytes | None) -> bytes:
    if b is None:
        return struct.pack(">I", 0xFFFFFFFF)
    return struct.pack(">I", len(b)) + b


def _header(msg_type: int, instance_id: str = "WSJT-X") -> bytes:
    return (
        struct.pack(">I", MAGIC)
        + struct.pack(">I", SCHEMA)
        + struct.pack(">I", msg_type)
        + _qbytearray(instance_id.encode())
    )


def _decode_datagram(
    message: str,
    snr: int = -5,
    mode: str = "FT8",
    off_air: bool = False,
    instance_id: str = "WSJT-X",
) -> bytes:
    return (
        _header(_MSG_DECODE, instance_id)
        + struct.pack(">?", True)       # new_decode
        + struct.pack(">I", 0)          # time_ms
        + struct.pack(">i", snr)        # snr
        + struct.pack(">d", 0.5)        # delta_time
        + struct.pack(">I", 1234)       # delta_freq
        + _qstring(mode)
        + _qstring(message)
        + struct.pack(">?", False)      # low_confidence
        + struct.pack(">?", off_air)
    )


def _status_datagram(
    de_call: str = "W1AW",
    dx_call: str = "",
    freq_hz: int = 14_074_000,
    transmitting: bool = False,
    decoding: bool = True,
    instance_id: str = "WSJT-X",
) -> bytes:
    return (
        _header(_MSG_STATUS, instance_id)
        + struct.pack(">Q", freq_hz)   # freq
        + _qstring("FT8")              # tx_mode
        + _qstring(dx_call)            # dx_call
        + _qstring("-10")              # report
        + _qstring("FT8")              # tx_mode2
        + struct.pack(">?", False)     # tx_enabled
        + struct.pack(">?", transmitting)
        + struct.pack(">?", decoding)
        + struct.pack(">I", 1500)      # rx_df
        + struct.pack(">I", 1500)      # tx_df
        + _qstring(de_call)            # de_call
        + _qstring("FN42")             # de_grid
        + _qstring("EN52")             # dx_grid
        + struct.pack(">?", False)     # tx_watchdog
        + _qstring("")                 # sub_mode
        + struct.pack(">?", False)     # fast_mode
    )


# ── parse_decode ──────────────────────────────────────────────────────────────


def test_parse_decode_basic():
    data = _decode_datagram("CQ W1AW FN42", snr=-10, mode="FT8")
    ev = parse_decode(data)
    assert ev is not None
    assert ev.message == "CQ W1AW FN42"
    assert ev.snr == -10
    assert ev.mode == "FT8"
    assert ev.off_air is False
    assert ev.instance_id == "WSJT-X"


def test_parse_decode_off_air():
    data = _decode_datagram("KD9ABC W1AW +03", off_air=True)
    ev = parse_decode(data)
    assert ev is not None
    assert ev.off_air is True


def test_parse_decode_wrong_type_returns_none():
    data = _status_datagram()
    assert parse_decode(data) is None


def test_parse_decode_truncated_returns_none():
    assert parse_decode(b"\xad\xbc\xcb\xda") is None


def test_parse_decode_bad_magic_returns_none():
    data = _decode_datagram("CQ W1AW FN42")
    bad = b"\x00\x00\x00\x00" + data[4:]
    assert parse_decode(bad) is None


# ── parse_status ──────────────────────────────────────────────────────────────


def test_parse_status_basic():
    data = _status_datagram(de_call="KD9ABC", dx_call="W1AW", freq_hz=14_074_000)
    s = parse_status(data)
    assert s is not None
    assert s.de_call == "KD9ABC"
    assert s.dx_call == "W1AW"
    assert s.freq_hz == 14_074_000
    assert s.tx_mode == "FT8"
    assert s.decoding is True
    assert s.transmitting is False


def test_parse_status_wrong_type_returns_none():
    data = _decode_datagram("CQ W1AW FN42")
    assert parse_status(data) is None


# ── FT8 addressing (via decode_to_message) ────────────────────────────────────


def _event(message: str, snr: int = -5, off_air: bool = False) -> _DecodeEvent:
    return _DecodeEvent(
        instance_id="WSJT-X",
        new_decode=True,
        time_ms=0,
        snr=snr,
        delta_time=0.5,
        delta_freq=1234,
        mode="FT8",
        message=message,
        low_confidence=False,
        off_air=off_air,
    )


def test_cq_is_broadcast():
    msg = decode_to_message(_event("CQ W1AW FN42"), my_callsign="KD9ABC")
    assert msg is not None
    assert msg.address_type is AddressType.BROADCAST
    assert msg.sender == "W1AW"
    assert msg.recipient is None
    assert msg.metadata["overheard"] is False


def test_cq_dx_mode():
    msg = decode_to_message(_event("CQ DX W1AW FN42"), my_callsign="KD9ABC")
    assert msg is not None
    assert msg.address_type is AddressType.BROADCAST
    assert msg.sender == "W1AW"


def test_direct_to_my_callsign():
    msg = decode_to_message(_event("KD9ABC W1AW -07"), my_callsign="W1AW")
    assert msg is not None
    assert msg.address_type is AddressType.DIRECT
    assert msg.sender == "KD9ABC"
    assert msg.recipient == "W1AW"
    assert msg.metadata["snr"] == -5
    assert msg.metadata["overheard"] is False


def test_directed_to_other_station_is_overheard():
    msg = decode_to_message(_event("KD9ABC W1AW -07"), my_callsign="N0CALL")
    assert msg is not None
    assert msg.address_type is AddressType.BROADCAST
    assert msg.metadata["overheard"] is True
    assert msg.metadata["to"] == "W1AW"
    assert msg.recipient is None  # not stored as our 1:1 thread


def test_no_callsign_treats_directed_as_overheard():
    # Without knowing my callsign, directed frames can't be addressed to us.
    msg = decode_to_message(_event("KD9ABC W1AW -07"), my_callsign="")
    assert msg is not None
    assert msg.address_type is AddressType.BROADCAST
    assert msg.metadata["overheard"] is True


def test_off_air_returns_none():
    assert decode_to_message(_event("CQ W1AW FN42", off_air=True)) is None


def test_empty_message_returns_none():
    assert decode_to_message(_event("")) is None


def test_whitespace_only_returns_none():
    assert decode_to_message(_event("   ")) is None


def test_snr_stored_in_metadata():
    msg = decode_to_message(_event("CQ W1AW FN42", snr=-12))
    assert msg is not None
    assert msg.metadata["snr"] == -12


def test_mode_stored_in_metadata():
    ev = _DecodeEvent("X", True, 0, 0, 0.0, 1000, "FT4", "CQ W1AW FN42", False, False)
    msg = decode_to_message(ev)
    assert msg is not None
    assert msg.metadata["mode"] == "FT4"


# ── build_freetext_datagram ───────────────────────────────────────────────────


def test_freetext_datagram_magic_and_type():
    dgram = build_freetext_datagram("WSJT-X", "HELLO WORLD")
    magic, schema, msg_type = struct.unpack_from(">III", dgram)
    assert magic == MAGIC
    assert schema == SCHEMA
    assert msg_type == _MSG_FREETEXT


def test_freetext_datagram_contains_text():
    dgram = build_freetext_datagram("WSJT-X", "TEST 73")
    # Text is encoded as UTF-16-BE following the instance id
    assert "TEST 73".encode("utf-16-be") in dgram


def test_freetext_datagram_send_flag_true():
    dgram = build_freetext_datagram("WSJT-X", "HI", send=True)
    assert dgram[-1] == 1  # True as big-endian bool


def test_freetext_datagram_send_flag_false():
    dgram = build_freetext_datagram("WSJT-X", "HI", send=False)
    assert dgram[-1] == 0


# ── WsjtXTransport (fake UDP) ─────────────────────────────────────────────────


def _fake_transport() -> WsjtXTransport:
    t = WsjtXTransport({"callsign": "N0CALL"})
    t._udp_transport = MagicMock()
    t._running = True
    return t


def _capture(t: WsjtXTransport) -> list[UnifiedMessage]:
    received: list[UnifiedMessage] = []

    async def _cb(msg: UnifiedMessage) -> None:
        received.append(msg)

    t.on_receive(_cb)
    return received


def test_inbound_cq_emits_broadcast():
    t = _fake_transport()
    received = _capture(t)
    data = _decode_datagram("CQ W1AW FN42")
    asyncio.run(_run_datagram(t, data))
    assert len(received) == 1
    assert received[0].address_type is AddressType.BROADCAST
    assert received[0].sender == "W1AW"
    assert received[0].transport == "wsjt_x"


def test_inbound_direct_emits_direct():
    t = _fake_transport()
    received = _capture(t)
    data = _decode_datagram("KD9ABC N0CALL +02")
    asyncio.run(_run_datagram(t, data))
    assert len(received) == 1
    assert received[0].address_type is AddressType.DIRECT
    assert received[0].sender == "KD9ABC"
    assert received[0].recipient == "N0CALL"


def test_inbound_off_air_not_emitted():
    t = _fake_transport()
    received = _capture(t)
    data = _decode_datagram("CQ W1AW FN42", off_air=True)
    asyncio.run(_run_datagram(t, data))
    assert len(received) == 0


def test_status_updates_callsign():
    t = WsjtXTransport({})  # no callsign configured
    t._udp_transport = MagicMock()
    t._running = True
    assert t._my_callsign == ""
    data = _status_datagram(de_call="W1AW")
    t._on_datagram(data, ("127.0.0.1", 2237))
    assert t._my_callsign == "W1AW"


def test_status_does_not_override_configured_callsign():
    t = _fake_transport()  # callsign="N0CALL"
    data = _status_datagram(de_call="W1AW")
    t._on_datagram(data, ("127.0.0.1", 2237))
    assert t._my_callsign == "N0CALL"


def test_send_queues_freetext_datagram():
    t = _fake_transport()
    msg = UnifiedMessage.broadcast("N0CALL", "CQ TEST")
    result = asyncio.run(t.send(msg))
    assert result is True
    t._udp_transport.sendto.assert_called_once()
    dgram, addr = t._udp_transport.sendto.call_args[0]
    assert "CQ TEST".encode("utf-16-be") in dgram


def test_send_truncates_to_max_content():
    t = _fake_transport()
    long_text = "A" * 50
    msg = UnifiedMessage.broadcast("N0CALL", long_text)
    asyncio.run(t.send(msg))
    dgram, _ = t._udp_transport.sendto.call_args[0]
    truncated = ("A" * MAX_CONTENT).encode("utf-16-be")
    assert truncated in dgram


def test_send_returns_false_when_not_running():
    t = WsjtXTransport({})
    t._running = False
    msg = UnifiedMessage.broadcast("N0CALL", "HI")
    assert asyncio.run(t.send(msg)) is False


def test_send_empty_content_returns_false():
    t = _fake_transport()
    msg = UnifiedMessage.broadcast("N0CALL", "   ")
    assert asyncio.run(t.send(msg)) is False


def test_check_reachable_down_when_no_datagrams_received():
    # Socket is bound but WSJT-X hasn't sent anything — should be DOWN.
    t = _fake_transport()
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.DOWN


def test_check_reachable_ok_after_receiving_datagram():
    t = _fake_transport()
    data = _status_datagram()
    asyncio.run(_run_datagram(t, data))
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.OK


def test_check_reachable_down_after_timeout(monkeypatch):
    import time as _time
    t = _fake_transport()
    data = _status_datagram()
    asyncio.run(_run_datagram(t, data))
    # Simulate 61 seconds elapsing since last datagram
    monkeypatch.setattr(_time, "monotonic", lambda: t._last_rx + 61.0)
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.DOWN


def test_check_reachable_down_when_not_running():
    t = WsjtXTransport({})
    t._last_rx = 1.0  # has received data but transport not started
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.DOWN


def test_capabilities_prohibits_encryption():
    caps = WsjtXTransport({}).capabilities()
    assert caps.prohibits_encryption is True
    assert caps.uses_shared_radio is True
    assert caps.max_message_size == MAX_CONTENT
    assert caps.needs_internet is False


def test_instance_id_tracked_from_datagram():
    t = _fake_transport()
    data = _decode_datagram("CQ W1AW FN42", instance_id="MyWsjtX")
    asyncio.run(_run_datagram(t, data))
    assert t._instance_id == "MyWsjtX"


def test_health_detail_without_status():
    t = WsjtXTransport({})
    detail = t.health_detail()
    assert "status" in detail


def test_health_detail_with_status():
    t = _fake_transport()
    data = _status_datagram(de_call="W1AW", dx_call="KD9ABC", freq_hz=14_074_000)
    t._on_datagram(data, ("127.0.0.1", 2237))
    detail = t.health_detail()
    assert detail["callsign"] == "W1AW"
    assert detail["freq_hz"] == 14_074_000
    assert detail["mode"] == "FT8"


# ── helpers ───────────────────────────────────────────────────────────────────


async def _run_datagram(t: WsjtXTransport, data: bytes) -> None:
    """Deliver a datagram to the transport and let pending futures settle."""
    t._on_datagram(data, ("127.0.0.1", 2237))
    await asyncio.sleep(0)  # let ensure_future tasks run
