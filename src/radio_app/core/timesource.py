"""Time-source query: multi-source clock offset with priority chain.

Tries time sources in order: GPS (gpsd) → local NTP daemon (chronyc/ntpq)
→ internet NTP → system clock. Accurate time matters for HF modes (JS8Call
uses sub-second framing) and for message timestamps across a multi-hop mesh
where clocks can drift.

Radio_App never starts or manages gpsd or chrony — it polls whatever daemons
the host already runs. Each source degrades gracefully (returns None) when
unavailable.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import re
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .._compat import UTC

log = logging.getLogger(__name__)


class TimeSourceKind(str, Enum):
    SYSTEM = "system"        # local clock only, no external sync
    NTP = "ntp"              # internet NTP (pool.ntp.org or configured host)
    LOCAL_NTP = "local_ntp"  # chrony or ntpd daemon running on the host
    GPS = "gps"              # gpsd daemon running on the host
    WSJTX = "wsjtx_ft8"     # WSJT-X / JS8Call DT field via UDP port 2237


@dataclass
class TimeReading:
    """Current UTC time and an optional clock-offset measurement."""

    utc: datetime
    source: TimeSourceKind
    offset_ms: float | None   # clock offset vs reference in ms; None if unavailable
    error: str | None         # non-None when sync failed / unavailable


_NTP_EPOCH_DELTA = 2_208_988_800  # seconds from NTP epoch (1900) to Unix epoch (1970)


def query_ntp(
    host: str = "pool.ntp.org",
    timeout: float = 2.0,
) -> float | None:
    """Return the local clock offset vs an NTP server in milliseconds.

    Sends a single 48-byte NTP client packet (mode 3, version 3) and reads
    the server's transmit timestamp to compute the offset. Returns None on
    any failure (network unreachable, timeout, malformed reply).

    Positive offset = local clock is ahead of NTP.
    """
    pkt = bytearray(48)
    pkt[0] = 0x1B  # LI=0, VN=3, Mode=3 (client)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t0 = time.monotonic()
            s.sendto(bytes(pkt), (host, 123))
            data, _ = s.recvfrom(1024)
            t1 = time.monotonic()
        if len(data) < 48:
            return None
        # Transmit timestamp occupies bytes 40-47: (seconds, fraction) since NTP epoch.
        tx_sec, tx_frac = struct.unpack("!II", data[40:48])
        ntp_unix = tx_sec - _NTP_EPOCH_DELTA + tx_frac / 2**32
        # De-skew by half the round-trip time to get the midpoint.
        local_unix = time.time() - (t1 - t0) / 2
        return (local_unix - ntp_unix) * 1000.0
    except Exception:  # noqa: BLE001 - any failure = no sync info
        return None


def query_gpsd_time(
    host: str = "127.0.0.1",
    port: int = 2947,
    timeout: float = 3.0,
) -> float | None:
    """Return local clock offset vs GPS time in milliseconds, or None.

    Connects to an already-running gpsd instance and reads the first TPV
    report with mode >= 2 (2D or 3D fix). The TPV 'time' field is GPS-
    disciplined UTC (±100 ns from atomic clock). Never starts or manages
    gpsd — just polls it.

    Positive = local clock ahead of GPS.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
            buf = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = max(0.05, deadline - time.monotonic())
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(4096)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                for line in buf.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if obj.get("class") == "TPV" and obj.get("mode", 0) >= 2:
                        gps_time_str = obj.get("time")
                        if gps_time_str:
                            gps_dt = datetime.fromisoformat(
                                gps_time_str.rstrip("Z")
                            ).replace(tzinfo=UTC)
                            local_unix = time.time()
                            gps_unix = gps_dt.timestamp()
                            return (local_unix - gps_unix) * 1000.0
    except (OSError, TimeoutError):
        pass
    return None


def query_chronyc(timeout: float = 3.0) -> float | None:
    """Return local clock offset via chronyc tracking, in milliseconds, or None.

    Runs ``chronyc tracking`` as a subprocess — chrony must already be
    running; this function never starts it. Parses the 'System time' line
    which chrony updates from its reference (GPS, PPS, NTP, etc.).

    Positive = local clock ahead of reference.
    """
    try:
        result = subprocess.run(
            ["chronyc", "tracking"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        m = re.search(
            r"System time\s*:\s*([\d.]+)\s+seconds\s+(fast|slow)", result.stdout
        )
        if m:
            offset_s = float(m.group(1))
            sign = 1.0 if m.group(2) == "fast" else -1.0
            return sign * offset_s * 1000.0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def query_ntpd(timeout: float = 3.0) -> float | None:
    """Return local clock offset via ntpq, in milliseconds, or None.

    Runs ``ntpq -c rv`` as a subprocess — ntpd must already be running;
    this function never starts it. Falls back after chronyc fails.
    ntpq reports offset in milliseconds already.

    Positive = local clock ahead of reference.
    """
    try:
        result = subprocess.run(
            ["ntpq", "-c", "rv"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        m = re.search(r"\boffset=([-\d.]+)", result.stdout)
        if m:
            return float(m.group(1))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def _parse_wsjtx_decode(data: bytes) -> float | None:
    """Extract the DT field from a WSJT-X schema-2 Decode (msg_id=2) UDP packet.

    Returns DT in seconds, or None if the packet is not a valid Decode message.
    Byte layout mirrors jtxsync/source/main.c exactly.

    After the 12-byte header (magic + schema + msg_id):
      uid_len (uint32) + uid bytes
      New bool (1 byte)
      Time uint32 / 4 bytes (ms since midnight, skipped)
      SNR int32 / 4 bytes (skipped)
      Delta time double / 8 bytes  ← what we want
    """
    if len(data) < 36:
        return None
    magic, schema, msg_id = struct.unpack_from(">III", data, 0)
    if magic != 0xADBCCBDA or schema != 2 or msg_id != 2:
        return None

    count = 12
    if count + 4 > len(data):
        return None
    uid_len = struct.unpack_from(">I", data, count)[0]
    count += 4

    if uid_len > 32 or count + uid_len > len(data):
        return None
    uid = data[count: count + uid_len].decode("ascii", errors="ignore")
    count += uid_len

    if uid not in ("WSJT-X", "JS8Call"):
        return None

    # New bool (1) + Time uint32 (4) + SNR int32 (4) = 9 bytes to skip
    count += 9
    if count + 8 > len(data):
        return None
    return struct.unpack_from(">d", data, count)[0]


class _WSJTXProtocol(asyncio.DatagramProtocol):
    """asyncio datagram handler that forwards raw packets to a callback."""

    def __init__(self, on_packet):
        self._on_packet = on_packet

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._on_packet(data)

    def error_received(self, exc: Exception) -> None:
        log.debug("WSJTXDTMonitor socket error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


class WSJTXDTMonitor:
    """Background asyncio UDP listener that accumulates DT samples from WSJT-X
    or JS8Call and exposes a filtered clock-offset estimate.

    Binds to UDP port 2237 (configurable) with SO_REUSEPORT so it can share
    the port alongside a running WSJT-X or JS8Call instance. Degrades
    gracefully — ``get_offset_ms()`` returns None until ``min_samples`` have
    been collected, and the whole monitor silently no-ops if the port is
    unavailable.

    ``get_offset_ms()`` is thread-safe and may be called from any thread.
    """

    def __init__(
        self,
        port: int = 2237,
        max_samples: int = 10,
        min_samples: int = 4,
    ) -> None:
        self._port = port
        self._max_samples = max(min_samples, max_samples)
        self._min_samples = min_samples
        self._samples: collections.deque[float] = collections.deque(
            maxlen=self._max_samples
        )
        self._lock = threading.Lock()
        self._cached_offset_ms: float | None = None
        self._transport: asyncio.BaseTransport | None = None
        self._task: asyncio.Task | None = None

    # -- public API -----------------------------------------------------------

    def get_offset_ms(self) -> float | None:
        """Return the current filtered clock offset in milliseconds, or None.

        Positive = local clock is ahead of the FT8/JS8 reference.
        Returns None until ``min_samples`` Decode messages have been received.
        Thread-safe.
        """
        with self._lock:
            return self._cached_offset_ms

    @property
    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _WSJTXProtocol(self._on_packet),
                local_addr=("0.0.0.0", self._port),
                reuse_port=True,
            )
            log.debug("WSJTXDTMonitor listening on UDP :%d", self._port)
        except OSError as exc:
            log.debug("WSJTXDTMonitor could not bind UDP :%d — %s", self._port, exc)

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # -- internals ------------------------------------------------------------

    def _on_packet(self, data: bytes) -> None:
        dt = _parse_wsjtx_decode(data)
        if dt is None:
            return
        with self._lock:
            self._samples.append(dt)
            self._cached_offset_ms = self._compute_offset()

    def _compute_offset(self) -> float | None:
        """Compute filtered mean DT and convert to a clock offset in ms.

        Mirrors jtxsync's algorithm: filter outliers beyond ±1 std dev,
        then take the mean of what remains. Returns None if not enough
        samples yet. Caller must hold self._lock.
        """
        samples = list(self._samples)
        if len(samples) < self._min_samples:
            return None
        mean = sum(samples) / len(samples)
        if len(samples) > 1:
            variance = sum((s - mean) ** 2 for s in samples) / (len(samples) - 1)
            stdev = math.sqrt(variance)
        else:
            stdev = 0.0
        filtered = [s for s in samples if mean - stdev <= s <= mean + stdev]
        if not filtered:
            return None
        filtered_mean = sum(filtered) / len(filtered)
        # Negate: positive DT means signals arrived early → our clock is behind
        # → offset (local − reference) is negative.
        return -filtered_mean * 1000.0


class TimeConsensus:
    """Query time sources in priority order and return the best available reading.

    Priority: GPS (gpsd) → WSJT-X/JS8Call DT → local NTP daemon → internet NTP → system.

    Radio_App never starts or manages any of these daemons — it polls
    whatever is already running on the host. Each source is tried in order;
    the first that returns an offset wins.
    """

    def __init__(
        self,
        *,
        gpsd_host: str = "127.0.0.1",
        gpsd_port: int = 2947,
        ntp_host: str = "pool.ntp.org",
        timeout: float = 2.0,
        skip_gps: bool = False,
        skip_local_ntp: bool = False,
        skip_internet_ntp: bool = False,
        wsjtx_monitor: WSJTXDTMonitor | None = None,
    ) -> None:
        self.gpsd_host = gpsd_host
        self.gpsd_port = gpsd_port
        self.ntp_host = ntp_host
        self.timeout = timeout
        self.skip_gps = skip_gps
        self.skip_local_ntp = skip_local_ntp
        self.skip_internet_ntp = skip_internet_ntp
        self.wsjtx_monitor = wsjtx_monitor

    def best_reading(self) -> TimeReading:
        """Return the best available time reading.

        Tries sources in order and returns immediately on first success.
        Always returns a valid TimeReading — falls back to system clock if
        all external sources fail.
        """
        # 1. GPS via gpsd
        if not self.skip_gps:
            offset = query_gpsd_time(
                self.gpsd_host, self.gpsd_port, timeout=self.timeout
            )
            if offset is not None:
                return TimeReading(
                    utc=datetime.now(UTC),
                    source=TimeSourceKind.GPS,
                    offset_ms=offset,
                    error=None,
                )

        # 2. WSJT-X / JS8Call DT (passive background monitor — no I/O here)
        if self.wsjtx_monitor is not None:
            offset = self.wsjtx_monitor.get_offset_ms()
            if offset is not None:
                return TimeReading(
                    utc=datetime.now(UTC),
                    source=TimeSourceKind.WSJTX,
                    offset_ms=offset,
                    error=None,
                )

        # 3. Local NTP daemon (chronyc first, then ntpq)
        if not self.skip_local_ntp:
            offset = query_chronyc(timeout=self.timeout)
            if offset is None:
                offset = query_ntpd(timeout=self.timeout)
            if offset is not None:
                return TimeReading(
                    utc=datetime.now(UTC),
                    source=TimeSourceKind.LOCAL_NTP,
                    offset_ms=offset,
                    error=None,
                )

        # 4. Internet NTP
        if not self.skip_internet_ntp and self.ntp_host:
            offset = query_ntp(self.ntp_host, timeout=self.timeout)
            if offset is not None:
                return TimeReading(
                    utc=datetime.now(UTC),
                    source=TimeSourceKind.NTP,
                    offset_ms=offset,
                    error=None,
                )

        # 5. System clock fallback
        all_skipped = self.skip_gps and self.skip_local_ntp and self.skip_internet_ntp
        error = None if all_skipped else "all time sources unavailable"
        return TimeReading(
            utc=datetime.now(UTC),
            source=TimeSourceKind.SYSTEM,
            offset_ms=None,
            error=error,
        )


def current_time(
    *,
    ntp_host: str | None = "pool.ntp.org",
    timeout: float = 2.0,
) -> TimeReading:
    """Return the current UTC time with an optional NTP offset measurement."""
    utc = datetime.now(UTC)
    if ntp_host:
        offset = query_ntp(ntp_host, timeout)
        if offset is not None:
            return TimeReading(utc, TimeSourceKind.NTP, offset, None)
        return TimeReading(utc, TimeSourceKind.SYSTEM, None, "NTP unreachable")
    return TimeReading(utc, TimeSourceKind.SYSTEM, None, None)
