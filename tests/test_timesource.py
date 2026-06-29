"""Time-source module: NTP query and TimeReading."""
from __future__ import annotations

import struct
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from radio_app.core.timesource import (
    TimeReading,
    TimeSourceKind,
    current_time,
    query_ntp,
)


def _make_ntp_reply(offset_s: float = 0.0) -> bytes:
    """Build a minimal 48-byte NTP reply with a controlled transmit timestamp."""
    import time
    _NTP_DELTA = 2_208_988_800
    tx = time.time() + offset_s + _NTP_DELTA  # fake NTP transmit time
    tx_sec = int(tx)
    tx_frac = int((tx - tx_sec) * 2**32)
    pkt = bytearray(48)
    pkt[0] = 0x1C  # server reply
    struct.pack_into("!II", pkt, 40, tx_sec, tx_frac)
    return bytes(pkt)


def test_query_ntp_success(monkeypatch):
    reply = _make_ntp_reply(offset_s=0.0)
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recvfrom.return_value = (reply, ("1.2.3.4", 123))

    with patch("socket.socket", return_value=mock_sock):
        offset = query_ntp("pool.ntp.org", timeout=1.0)

    assert offset is not None
    assert isinstance(offset, float)
    assert abs(offset) < 200  # within 200 ms (mocked so near 0)


def test_query_ntp_timeout(monkeypatch):
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recvfrom.side_effect = TimeoutError()

    with patch("socket.socket", return_value=mock_sock):
        result = query_ntp("pool.ntp.org", timeout=0.01)

    assert result is None


def test_query_ntp_short_reply(monkeypatch):
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recvfrom.return_value = (b"\x1c" * 10, ("1.2.3.4", 123))

    with patch("socket.socket", return_value=mock_sock):
        result = query_ntp("pool.ntp.org")

    assert result is None


def test_current_time_with_ntp(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.query_ntp",
        lambda host, timeout=2.0: 42.5,
    )
    reading = current_time(ntp_host="pool.ntp.org")
    assert reading.source is TimeSourceKind.NTP
    assert reading.offset_ms == 42.5
    assert reading.error is None
    assert reading.utc.tzinfo is not None


def test_current_time_ntp_unavailable(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.query_ntp",
        lambda host, timeout=2.0: None,
    )
    reading = current_time(ntp_host="pool.ntp.org")
    assert reading.source is TimeSourceKind.SYSTEM
    assert reading.offset_ms is None
    assert "unreachable" in (reading.error or "")


def test_current_time_no_ntp():
    reading = current_time(ntp_host=None)
    assert reading.source is TimeSourceKind.SYSTEM
    assert reading.offset_ms is None
    assert reading.error is None


def test_time_reading_dataclass():
    now = datetime.now(UTC)
    r = TimeReading(utc=now, source=TimeSourceKind.NTP, offset_ms=-12.3, error=None)
    assert r.utc is now
    assert r.offset_ms == -12.3


# --- TimeConsensus -----------------------------------------------------------

import subprocess as _subprocess

from radio_app.core.timesource import (
    TimeConsensus,
    query_chronyc,
    query_gpsd_time,
    query_ntpd,
)


def test_time_consensus_uses_gps_first(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: -5.0)
    tc = TimeConsensus()
    r = tc.best_reading()
    assert r.source is TimeSourceKind.GPS
    assert r.offset_ms == pytest.approx(-5.0)


def test_time_consensus_falls_back_to_local_ntp_chronyc(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_chronyc", lambda **kw: 12.5)
    tc = TimeConsensus()
    r = tc.best_reading()
    assert r.source is TimeSourceKind.LOCAL_NTP
    assert r.offset_ms == pytest.approx(12.5)


def test_time_consensus_falls_back_to_local_ntp_ntpd(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_chronyc", lambda **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntpd", lambda **kw: 8.3)
    tc = TimeConsensus()
    r = tc.best_reading()
    assert r.source is TimeSourceKind.LOCAL_NTP
    assert r.offset_ms == pytest.approx(8.3)


def test_time_consensus_falls_back_to_internet_ntp(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_chronyc", lambda **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntpd", lambda **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntp", lambda *a, **kw: 33.1)
    tc = TimeConsensus()
    r = tc.best_reading()
    assert r.source is TimeSourceKind.NTP
    assert r.offset_ms == pytest.approx(33.1)


def test_time_consensus_system_fallback(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_chronyc", lambda **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntpd", lambda **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntp", lambda *a, **kw: None)
    tc = TimeConsensus()
    r = tc.best_reading()
    assert r.source is TimeSourceKind.SYSTEM
    assert r.offset_ms is None
    assert r.error is not None


def test_time_consensus_skip_gps_and_local(monkeypatch):
    called = []
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time",
                        lambda *a, **kw: called.append("gps") or None)
    monkeypatch.setattr("radio_app.core.timesource.query_chronyc",
                        lambda **kw: called.append("chrony") or None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntpd",
                        lambda **kw: called.append("ntpd") or None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntp",
                        lambda *a, **kw: called.append("ntp") or 0.0)
    tc = TimeConsensus(skip_gps=True, skip_local_ntp=True)
    tc.best_reading()
    assert "gps" not in called
    assert "chrony" not in called
    assert "ntpd" not in called
    assert "ntp" in called


def test_time_consensus_utc_populated():
    tc = TimeConsensus(skip_gps=True, skip_local_ntp=True, skip_internet_ntp=True)
    r = tc.best_reading()
    assert r.utc.tzinfo is not None


# --- query_chronyc -----------------------------------------------------------

def _fake_subproc(stdout: str):
    return type("R", (), {"stdout": stdout, "returncode": 0})()


def test_query_chronyc_parses_slow(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.subprocess.run",
        lambda *a, **kw: _fake_subproc(
            "System time     : 0.000500000 seconds slow of NTP time\n"
        ),
    )
    result = query_chronyc()
    assert result == pytest.approx(-0.5)  # 500 µs slow = -0.5 ms


def test_query_chronyc_parses_fast(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.subprocess.run",
        lambda *a, **kw: _fake_subproc(
            "System time     : 0.001000000 seconds fast of NTP time\n"
        ),
    )
    result = query_chronyc()
    assert result == pytest.approx(1.0)  # 1 ms fast = +1.0 ms


def test_query_chronyc_not_found(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert query_chronyc() is None


def test_query_chronyc_no_system_time_line(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.subprocess.run",
        lambda *a, **kw: _fake_subproc("Reference ID: something\n"),
    )
    assert query_chronyc() is None


# --- query_ntpd --------------------------------------------------------------

def test_query_ntpd_parses_offset(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.subprocess.run",
        lambda *a, **kw: _fake_subproc(
            "associd=0 status=0615,\noffset=-12.345, frequency=-22.1\n"
        ),
    )
    result = query_ntpd()
    assert result == pytest.approx(-12.345)


def test_query_ntpd_not_found(monkeypatch):
    monkeypatch.setattr(
        "radio_app.core.timesource.subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert query_ntpd() is None


# --- query_gpsd_time ---------------------------------------------------------

def test_query_gpsd_time_returns_none_on_refused():
    with patch("socket.create_connection", side_effect=OSError("refused")):
        result = query_gpsd_time()
    assert result is None


def test_query_gpsd_time_no_fix():
    import json
    data = (json.dumps({"class": "TPV", "mode": 1}) + "\n").encode()
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recv.side_effect = [data, b""]
    with patch("socket.create_connection", return_value=mock_sock):
        result = query_gpsd_time()
    assert result is None


def test_query_gpsd_time_with_fix():
    import json
    from datetime import UTC, datetime, timedelta
    # Use a timestamp very close to now so the offset is small and verifiable
    gps_dt = datetime.now(UTC) - timedelta(seconds=0.050)  # 50 ms in the past
    now_iso = gps_dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    tpv = json.dumps({
        "class": "TPV", "mode": 3,
        "time": now_iso, "lat": 42.0, "lon": -71.0,
    }) + "\n"
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recv.side_effect = [tpv.encode(), b""]
    with patch("socket.create_connection", return_value=mock_sock):
        result = query_gpsd_time()
    assert isinstance(result, float)
    # Local clock is ahead of a timestamp 50ms in the past → positive offset ~50ms
    assert result > 0


# --- WSJTXDTMonitor / _parse_wsjtx_decode ------------------------------------

from radio_app.core.timesource import WSJTXDTMonitor, _parse_wsjtx_decode


def _make_wsjtx_decode(dt: float, uid: str = "WSJT-X") -> bytes:
    """Build a minimal WSJT-X schema-2 Decode packet with the given DT."""
    magic = 0xADBCCBDA
    schema = 2
    msg_id = 2
    header = struct.pack(">III", magic, schema, msg_id)
    uid_bytes = uid.encode("ascii")
    uid_field = struct.pack(">I", len(uid_bytes)) + uid_bytes
    # New bool (1) + Time uint32 (4) + SNR int32 (4) + DT double (8)
    body = struct.pack(">BIid", 0, 0, 0, dt)
    return header + uid_field + body


def test_parse_wsjtx_decode_basic():
    pkt = _make_wsjtx_decode(0.25)
    result = _parse_wsjtx_decode(pkt)
    assert result == pytest.approx(0.25)


def test_parse_wsjtx_decode_negative_dt():
    pkt = _make_wsjtx_decode(-0.8)
    result = _parse_wsjtx_decode(pkt)
    assert result == pytest.approx(-0.8)


def test_parse_wsjtx_decode_js8call_uid():
    pkt = _make_wsjtx_decode(0.1, uid="JS8Call")
    result = _parse_wsjtx_decode(pkt)
    assert result == pytest.approx(0.1)


def test_parse_wsjtx_decode_wrong_magic():
    pkt = bytearray(_make_wsjtx_decode(0.1))
    pkt[0] = 0xFF  # corrupt magic
    assert _parse_wsjtx_decode(bytes(pkt)) is None


def test_parse_wsjtx_decode_wrong_schema():
    pkt = bytearray(_make_wsjtx_decode(0.1))
    # schema is bytes 4-7; set to 3
    struct.pack_into(">I", pkt, 4, 3)
    assert _parse_wsjtx_decode(bytes(pkt)) is None


def test_parse_wsjtx_decode_wrong_msg_id():
    pkt = bytearray(_make_wsjtx_decode(0.1))
    struct.pack_into(">I", pkt, 8, 5)  # not a Decode message
    assert _parse_wsjtx_decode(bytes(pkt)) is None


def test_parse_wsjtx_decode_unknown_uid():
    pkt = _make_wsjtx_decode(0.1, uid="JTDX")
    assert _parse_wsjtx_decode(pkt) is None


def test_parse_wsjtx_decode_too_short():
    assert _parse_wsjtx_decode(b"\x00" * 10) is None


# --- WSJTXDTMonitor sample accumulation --------------------------------------


def test_wsjtx_monitor_no_offset_before_min_samples():
    mon = WSJTXDTMonitor(min_samples=4, max_samples=10)
    # Feed 3 packets (below min_samples=4)
    for _ in range(3):
        mon._on_packet(_make_wsjtx_decode(0.1))
    assert mon.get_offset_ms() is None
    assert mon.sample_count == 3


def test_wsjtx_monitor_returns_offset_at_min_samples():
    mon = WSJTXDTMonitor(min_samples=4, max_samples=10)
    for _ in range(4):
        mon._on_packet(_make_wsjtx_decode(0.2))
    offset = mon.get_offset_ms()
    assert offset is not None
    # DT=0.2 → offset = -0.2 * 1000 = -200 ms
    assert offset == pytest.approx(-200.0, abs=1.0)


def test_wsjtx_monitor_outlier_filtering():
    mon = WSJTXDTMonitor(min_samples=4, max_samples=10)
    # 9 samples near 0.1 and one extreme outlier
    for _ in range(9):
        mon._on_packet(_make_wsjtx_decode(0.1))
    mon._on_packet(_make_wsjtx_decode(99.0))  # outlier
    offset = mon.get_offset_ms()
    assert offset is not None
    # Should be close to -100 ms, not pulled toward the outlier
    assert abs(offset - (-100.0)) < 20.0


def test_wsjtx_monitor_rolling_window():
    mon = WSJTXDTMonitor(min_samples=4, max_samples=5)
    # Fill with 0.1 samples
    for _ in range(5):
        mon._on_packet(_make_wsjtx_decode(0.1))
    # Now push 5 samples of 0.5 (rolling deque evicts old ones)
    for _ in range(5):
        mon._on_packet(_make_wsjtx_decode(0.5))
    offset = mon.get_offset_ms()
    assert offset is not None
    assert abs(offset - (-500.0)) < 20.0


def test_wsjtx_monitor_ignores_invalid_packets():
    mon = WSJTXDTMonitor(min_samples=4, max_samples=10)
    mon._on_packet(b"\x00" * 20)  # garbage
    mon._on_packet(_make_wsjtx_decode(0.1, uid="JTDX"))  # unknown uid
    assert mon.sample_count == 0
    assert mon.get_offset_ms() is None


# --- TimeConsensus with wsjtx_monitor ----------------------------------------


def test_time_consensus_uses_wsjtx_before_local_ntp(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntp", lambda *a, **kw: None)
    mon = WSJTXDTMonitor(min_samples=1, max_samples=10)
    mon._on_packet(_make_wsjtx_decode(0.3))
    tc = TimeConsensus(wsjtx_monitor=mon)
    r = tc.best_reading()
    assert r.source is TimeSourceKind.WSJTX
    assert r.offset_ms == pytest.approx(-300.0, abs=1.0)


def test_time_consensus_skips_wsjtx_when_no_samples(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntp", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_chronyc", lambda **kw: 5.0)
    mon = WSJTXDTMonitor(min_samples=4, max_samples=10)  # no packets fed
    tc = TimeConsensus(wsjtx_monitor=mon)
    r = tc.best_reading()
    assert r.source is TimeSourceKind.LOCAL_NTP


def test_time_consensus_wsjtx_beats_local_ntp(monkeypatch):
    monkeypatch.setattr("radio_app.core.timesource.query_gpsd_time", lambda *a, **kw: None)
    monkeypatch.setattr("radio_app.core.timesource.query_ntp", lambda *a, **kw: None)
    called = []
    monkeypatch.setattr(
        "radio_app.core.timesource.query_chronyc",
        lambda **kw: called.append("chrony") or 5.0,
    )
    mon = WSJTXDTMonitor(min_samples=1, max_samples=10)
    mon._on_packet(_make_wsjtx_decode(0.05))
    tc = TimeConsensus(wsjtx_monitor=mon)
    r = tc.best_reading()
    assert r.source is TimeSourceKind.WSJTX
    assert "chrony" not in called  # never reached
