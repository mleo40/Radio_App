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

import json
import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum


class TimeSourceKind(str, Enum):
    SYSTEM = "system"        # local clock only, no external sync
    NTP = "ntp"              # internet NTP (pool.ntp.org or configured host)
    LOCAL_NTP = "local_ntp"  # chrony or ntpd daemon running on the host
    GPS = "gps"              # gpsd daemon running on the host


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


class TimeConsensus:
    """Query time sources in priority order and return the best available reading.

    Priority: GPS (gpsd) → local NTP daemon (chronyc/ntpq) → internet NTP → system.

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
    ) -> None:
        self.gpsd_host = gpsd_host
        self.gpsd_port = gpsd_port
        self.ntp_host = ntp_host
        self.timeout = timeout
        self.skip_gps = skip_gps
        self.skip_local_ntp = skip_local_ntp
        self.skip_internet_ntp = skip_internet_ntp

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

        # 2. Local NTP daemon (chronyc first, then ntpq)
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

        # 3. Internet NTP
        if not self.skip_internet_ntp and self.ntp_host:
            offset = query_ntp(self.ntp_host, timeout=self.timeout)
            if offset is not None:
                return TimeReading(
                    utc=datetime.now(UTC),
                    source=TimeSourceKind.NTP,
                    offset_ms=offset,
                    error=None,
                )

        # 4. System clock fallback
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
