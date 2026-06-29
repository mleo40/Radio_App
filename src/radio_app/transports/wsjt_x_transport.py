"""WSJT-X transport — surface FT8/FT4 decodes from WSJT-X as messages.

WSJT-X broadcasts UDP datagrams on port 2237 using a QDataStream binary
protocol (big-endian Qt serialisation). We receive Decode events (inbound
FT8/FT4 frames) and Status events (rig state). Outbound messages are sent
as FreeText datagrams which WSJT-X queues for the next 15-second TX window.

Key constraints
- FT8 free-text field: 13 characters maximum (content is truncated)
- 15-second TX period; Radio_App cannot control *when* WSJT-X transmits
- Multiple WSJT-X instances on a LAN share the same port; we track the
  instance ID from each datagram header to route replies back correctly
- Amateur regulations prohibit encrypted content on HF (uses_shared_radio=True)

Protocol reference: wsjtx/Network/MessageServer.cpp in the WSJT-X source tree.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass

from ..core.message import AddressType, UnifiedMessage
from .base import ReachabilityStatus, Transport, TransportCapabilities

log = logging.getLogger(__name__)

# ── QDataStream wire constants ────────────────────────────────────────────────

MAGIC: int = 0xADBCCBDA
SCHEMA: int = 2

# Outbound message types (Radio_App → WSJT-X)
_MSG_REPLY: int = 4
_MSG_FREETEXT: int = 9

# Inbound message types (WSJT-X → Radio_App)
_MSG_HEARTBEAT: int = 0
_MSG_STATUS: int = 1
_MSG_DECODE: int = 2
_MSG_CLEAR: int = 3
_MSG_QSOLOGGED: int = 5
_MSG_CLOSE: int = 6

MAX_CONTENT: int = 13  # FT8 free-text character limit


# ── Low-level binary reader ───────────────────────────────────────────────────


class _Reader:
    """Big-endian QDataStream deserialiser."""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def uint32(self) -> int:
        (val,) = struct.unpack_from(">I", self._data, self._pos)
        self._pos += 4
        return val

    def int32(self) -> int:
        (val,) = struct.unpack_from(">i", self._data, self._pos)
        self._pos += 4
        return val

    def uint64(self) -> int:
        (val,) = struct.unpack_from(">Q", self._data, self._pos)
        self._pos += 8
        return val

    def double(self) -> float:
        (val,) = struct.unpack_from(">d", self._data, self._pos)
        self._pos += 8
        return val

    def bool_(self) -> bool:
        (val,) = struct.unpack_from(">?", self._data, self._pos)
        self._pos += 1
        return val

    def uint8(self) -> int:
        (val,) = struct.unpack_from(">B", self._data, self._pos)
        self._pos += 1
        return val

    def qstring(self) -> str | None:
        length = self.uint32()
        if length == 0xFFFFFFFF:
            return None
        raw = self._data[self._pos : self._pos + length]
        self._pos += length
        return raw.decode("utf-16-be")

    def qbytearray(self) -> bytes | None:
        length = self.uint32()
        if length == 0xFFFFFFFF:
            return None
        raw = self._data[self._pos : self._pos + length]
        self._pos += length
        return raw

    def remaining(self) -> int:
        return len(self._data) - self._pos


# ── Parsed event dataclasses ──────────────────────────────────────────────────


@dataclass
class _DecodeEvent:
    instance_id: str
    new_decode: bool
    time_ms: int        # ms since midnight UTC
    snr: int            # dB
    delta_time: float   # seconds
    delta_freq: int     # Hz audio offset
    mode: str           # "FT8", "FT4", etc.
    message: str        # decoded text, e.g. "KD9ABC W1AW EN52"
    low_confidence: bool
    off_air: bool       # True means replayed from log, not live


@dataclass
class StatusEvent:
    """Parsed WSJT-X Status datagram (rig / session state)."""

    instance_id: str
    freq_hz: int
    tx_mode: str
    dx_call: str
    de_call: str
    de_grid: str
    dx_grid: str
    transmitting: bool
    decoding: bool
    config_name: str = ""
    tx_message: str = ""


# ── Header / body parsers (pure functions, easy to unit-test) ─────────────────


def _parse_header(r: _Reader) -> tuple[int, str] | None:
    """Read magic+schema+type+id. Returns (msg_type, instance_id) or None."""
    try:
        magic = r.uint32()
        if magic != MAGIC:
            return None
        _schema = r.uint32()
        msg_type = r.uint32()
        raw_id = r.qbytearray()
        instance_id = raw_id.decode("utf-8", errors="replace") if raw_id else ""
        return msg_type, instance_id
    except (struct.error, IndexError):
        return None


def parse_decode(data: bytes) -> _DecodeEvent | None:
    """Parse a full Decode (type 2) datagram. Returns None on bad data."""
    r = _Reader(data)
    header = _parse_header(r)
    if header is None or header[0] != _MSG_DECODE:
        return None
    instance_id = header[1]
    try:
        new_decode = r.bool_()
        time_ms = r.uint32()
        snr = r.int32()
        delta_time = r.double()
        delta_freq = r.uint32()
        mode = r.qstring() or ""
        message = r.qstring() or ""
        low_confidence = r.bool_()
        off_air = r.bool_()
    except (struct.error, IndexError):
        return None
    return _DecodeEvent(
        instance_id=instance_id,
        new_decode=new_decode,
        time_ms=time_ms,
        snr=snr,
        delta_time=delta_time,
        delta_freq=delta_freq,
        mode=mode,
        message=message,
        low_confidence=low_confidence,
        off_air=off_air,
    )


def parse_status(data: bytes) -> StatusEvent | None:
    """Parse a full Status (type 1) datagram. Returns None on bad data."""
    r = _Reader(data)
    header = _parse_header(r)
    if header is None or header[0] != _MSG_STATUS:
        return None
    instance_id = header[1]
    try:
        freq_hz = r.uint64()
        tx_mode = r.qstring() or ""
        dx_call = r.qstring() or ""
        _report = r.qstring()
        _tx_mode2 = r.qstring()
        _tx_enabled = r.bool_()
        transmitting = r.bool_()
        decoding = r.bool_()
        _rx_df = r.uint32()
        _tx_df = r.uint32()
        de_call = r.qstring() or ""
        de_grid = r.qstring() or ""
        dx_grid = r.qstring() or ""
        _tx_watchdog = r.bool_()
        _sub_mode = r.qstring()
        _fast_mode = r.bool_()
    except (struct.error, IndexError):
        return None
    config_name = ""
    tx_message = ""
    try:
        # Schema >= 3 extras (all optional)
        if r.remaining() >= 1:
            _special_op = r.uint8()
        if r.remaining() >= 8:
            _freq_tol = r.uint32()
            _tr_period = r.uint32()
        if r.remaining() >= 4:
            config_name = r.qstring() or ""
        if r.remaining() >= 4:
            tx_message = r.qstring() or ""
    except (struct.error, IndexError):
        pass
    return StatusEvent(
        instance_id=instance_id,
        freq_hz=freq_hz,
        tx_mode=tx_mode,
        dx_call=dx_call,
        de_call=de_call,
        de_grid=de_grid,
        dx_grid=dx_grid,
        transmitting=transmitting,
        decoding=decoding,
        config_name=config_name,
        tx_message=tx_message,
    )


# ── FT8 addressing logic ──────────────────────────────────────────────────────


def _ft8_addressing(
    message: str, my_callsign: str
) -> tuple[AddressType, str, str | None]:
    """Derive (address_type, sender, recipient) from a decoded FT8 text line.

    Standard FT8 formats:
      CQ [MODE] SENDER GRID   → broadcast
      SENDER RECIPIENT REPORT  → direct (if recipient == my_callsign) else overheard
    """
    parts = message.strip().split()
    if not parts:
        return AddressType.BROADCAST, "", None

    if parts[0] == "CQ":
        # CQ [DX/MODE] SENDER GRID — find sender as last non-grid token before grid
        # Typical: ["CQ", "W1AW", "FN42"] or ["CQ", "DX", "W1AW", "FN42"]
        sender = parts[-2] if len(parts) >= 3 else (parts[1] if len(parts) >= 2 else "")
        return AddressType.BROADCAST, sender, None

    if len(parts) >= 2:
        sender = parts[0]
        recipient = parts[1]
        my = my_callsign.strip().upper()
        if my and recipient.upper() == my:
            return AddressType.DIRECT, sender, recipient
        # Directed to another station — overheard
        return AddressType.BROADCAST, sender, recipient  # recipient stored in metadata

    return AddressType.BROADCAST, parts[0], None


def decode_to_message(
    event: _DecodeEvent, my_callsign: str = ""
) -> UnifiedMessage | None:
    """Convert a _DecodeEvent to a UnifiedMessage. Pure — no I/O, safe to test.

    Returns None for empty or off-air (replayed) decodes.
    """
    if not event.message.strip() or event.off_air:
        return None

    addr_type, sender, recipient = _ft8_addressing(event.message, my_callsign)

    overheard = (
        addr_type is AddressType.BROADCAST
        and recipient is not None  # directed to someone else
    )

    # For overheard direct traffic, clear recipient so it's not stored as a
    # false 1:1 thread — put the actual target in metadata instead.
    msg_recipient = recipient if addr_type is AddressType.DIRECT else None

    metadata: dict = {
        "snr": event.snr,
        "delta_time": event.delta_time,
        "delta_freq": event.delta_freq,
        "mode": event.mode,
        "low_confidence": event.low_confidence,
        "overheard": overheard,
        "kind": "decode",
    }
    if overheard and recipient:
        metadata["to"] = recipient

    return UnifiedMessage(
        sender=sender or (event.instance_id or "unknown"),
        content=event.message,
        address_type=addr_type,
        recipient=msg_recipient,
        metadata=metadata,
    )


# ── Outbound datagram builder ─────────────────────────────────────────────────


def build_freetext_datagram(instance_id: str, text: str, send: bool = True) -> bytes:
    """Build a WSJT-X FreeText (type 9) datagram."""
    buf = bytearray()
    buf += struct.pack(">I", MAGIC)
    buf += struct.pack(">I", SCHEMA)
    buf += struct.pack(">I", _MSG_FREETEXT)
    id_bytes = instance_id.encode("utf-8")
    buf += struct.pack(">I", len(id_bytes))
    buf += id_bytes
    text_bytes = text.encode("utf-16-be")
    buf += struct.pack(">I", len(text_bytes))
    buf += text_bytes
    buf += struct.pack(">?", send)
    return bytes(buf)


# ── asyncio DatagramProtocol ──────────────────────────────────────────────────


class _UDPProtocol(asyncio.DatagramProtocol):
    """Forwards raw datagrams to a synchronous callback."""

    def __init__(self, on_datagram) -> None:
        self._on_datagram = on_datagram
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.warning("[wsjt_x] UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            log.warning("[wsjt_x] UDP connection lost: %s", exc)


# ── Transport class ───────────────────────────────────────────────────────────


class WsjtXTransport(Transport):
    """WSJT-X UDP transport: receive FT8/FT4 decodes, send free-text messages."""

    name = "wsjt_x"
    display_name = "WSJT-X"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        cfg = config or {}
        self._host: str = cfg.get("host", "0.0.0.0")
        self._port: int = int(cfg.get("port", 2237))
        self._wsjtx_host: str = cfg.get("wsjtx_host", "127.0.0.1")
        self._my_callsign: str = cfg.get("callsign", "").strip().upper()
        self._instance_id: str = ""
        self._status: StatusEvent | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._last_rx: float = 0.0  # monotonic time of last datagram from WSJT-X

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _UDPProtocol(self._on_datagram),
                local_addr=(self._host, self._port),
                reuse_port=True,
            )
            self._udp_transport = transport
        except OSError as exc:
            log.warning(
                "[wsjt_x] UDP bind failed on %s:%s — %s. "
                "Is another process using port %s?",
                self._host,
                self._port,
                exc,
                self._port,
            )
            self._running = False
            return
        self._running = True
        log.info("[wsjt_x] Listening on UDP %s:%s.", self._host, self._port)

    async def stop(self) -> None:
        self._running = False
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        log.info("[wsjt_x] Stopped.")

    # -- receive side ----------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple) -> None:
        """Called synchronously by the UDP protocol on each received datagram."""
        if not data or len(data) < 12:
            return
        r = _Reader(data)
        header = _parse_header(r)
        if header is None:
            return
        msg_type, instance_id = header
        self._last_rx = time.monotonic()  # heard from WSJT-X
        if instance_id:
            self._instance_id = instance_id

        if msg_type == _MSG_DECODE:
            event = parse_decode(data)
            if event is not None and not event.off_air:
                msg = decode_to_message(event, self._my_callsign)
                if msg is not None:
                    asyncio.ensure_future(self._emit(msg))

        elif msg_type == _MSG_STATUS:
            status = parse_status(data)
            if status is not None:
                self._status = status
                if not self._my_callsign and status.de_call:
                    self._my_callsign = status.de_call.strip().upper()
                    log.debug(
                        "[wsjt_x] Learned callsign from Status: %s",
                        self._my_callsign,
                    )

        elif msg_type == _MSG_CLOSE:
            log.info("[wsjt_x] WSJT-X instance '%s' closed.", instance_id)

    # -- send side -------------------------------------------------------------

    async def send(self, msg: UnifiedMessage) -> bool:
        """Queue a free-text transmission in WSJT-X (fires on next TX window)."""
        if not self._running or self._udp_transport is None:
            return False
        text = msg.content[:MAX_CONTENT].strip()
        if not text:
            return False
        dgram = build_freetext_datagram(self._instance_id, text, send=True)
        try:
            self._udp_transport.sendto(dgram, (self._wsjtx_host, self._port))
        except OSError as exc:
            log.warning("[wsjt_x] sendto failed: %s", exc)
            return False
        log.debug("[wsjt_x] Queued free-text: %r", text)
        return True

    # -- introspection ---------------------------------------------------------

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=MAX_CONTENT,
            supports_broadcast=True,
            supports_addressing=False,
            supports_groups=False,
            supports_encryption=False,
            supports_delivery_confirmation=False,
            supports_chunking=False,
            supports_attachments=False,
            supports_position=False,
            is_realtime=False,
            typical_latency_s=15.0,
            needs_internet=False,
            address_scheme="callsign",
            carries_operator_identity=True,
            prohibits_encryption=True,
            uses_shared_radio=True,
        )

    async def check_reachable(self) -> ReachabilityStatus:
        if not self._running or self._udp_transport is None:
            return ReachabilityStatus.DOWN
        # Reachable only if we've received a valid datagram from WSJT-X recently.
        # A bound socket is not enough — it just means we're listening.
        if self._last_rx > 0 and (time.monotonic() - self._last_rx) < 60.0:
            return ReachabilityStatus.OK
        return ReachabilityStatus.DOWN

    def local_identity(self) -> str | None:
        return self._my_callsign or None

    def health_detail(self) -> dict:
        """Extra info shown in the Health panel (not part of the base interface)."""
        s = self._status
        if s is None:
            return {"status": "no status received from WSJT-X"}
        return {
            "callsign": s.de_call,
            "freq_hz": s.freq_hz,
            "mode": s.tx_mode,
            "dx_call": s.dx_call or "—",
            "de_grid": s.de_grid,
            "dx_grid": s.dx_grid or "—",
            "transmitting": s.transmitting,
            "decoding": s.decoding,
            "config": s.config_name,
        }
