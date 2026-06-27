"""GPS position and Maidenhead grid-square support.

Provides:
- ``Position`` dataclass (lat/lon/alt + computed Maidenhead locator)
- ``lat_lon_to_grid()`` — pure WGS-84 → Maidenhead conversion (no deps)
- ``GPSReader`` — reads a live fix from gpsd's JSON streaming API
- ``position_from_config()`` — read position from [position] config section
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


@dataclass
class Position:
    """A geographic position with an auto-computed Maidenhead grid locator."""

    lat: float
    lon: float
    alt_m: float | None = None
    source: str = "manual"   # "gps" | "config" | "manual"
    grid: str = field(init=False)

    def __post_init__(self) -> None:
        self.grid = lat_lon_to_grid(self.lat, self.lon)

    def __str__(self) -> str:
        return f"{self.lat:+.4f}°  {self.lon:+.4f}°  {self.grid}"


def lat_lon_to_grid(lat: float, lon: float, precision: int = 4) -> str:
    """Convert WGS-84 lat/lon to a Maidenhead grid square (4 or 6 chars).

    Precision 4 gives a roughly 100 × 50 km square; precision 6 adds a
    2-letter subsquare (~4 × 2 km). Raises ``ValueError`` for out-of-range
    coordinates.
    """
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError(f"lat={lat}, lon={lon} out of WGS-84 range")
    lon180 = lon + 180.0   # 0–360
    lat90 = lat + 90.0     # 0–180

    field_lon = chr(ord("A") + int(lon180 / 20))
    field_lat = chr(ord("A") + int(lat90 / 10))
    sq_lon = str(int((lon180 % 20) / 2))
    sq_lat = str(int(lat90 % 10))
    grid = field_lon + field_lat + sq_lon + sq_lat

    if precision >= 6:
        sub_lon = int((lon180 % 2) / 2 * 24)
        sub_lat = int((lat90 % 1) * 24)
        grid += chr(ord("a") + sub_lon) + chr(ord("a") + sub_lat)

    return grid


def position_from_config(cfg: "Config") -> "Position | None":
    """Read a manually-configured position from [position] in config.toml.

    Returns None when neither lat/lon nor a grid override is set.
    """
    sec = cfg.data.get("position", {})
    lat = sec.get("lat")
    lon = sec.get("lon")
    if lat is not None and lon is not None:
        return Position(lat=float(lat), lon=float(lon), source="config")
    return None


class GPSReader:
    """Read the current GPS fix from gpsd's JSON streaming API (port 2947).

    gpsd uses a simple newline-delimited JSON protocol. We send a WATCH
    request to start the TPV (time/position/velocity) stream, read until
    we get a TPV report with a 2D or 3D fix, then disconnect. Fails
    gracefully (returns None) when gpsd is unreachable or has no fix.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2947,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def read(self) -> Position | None:
        """Return the current GPS fix, or None if unavailable."""
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as sock:
                sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
                buf = b""
                deadline = time.monotonic() + self.timeout
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
                        except json.JSONDecodeError:
                            continue
                        if obj.get("class") == "TPV" and obj.get("mode", 0) >= 2:
                            lat = obj.get("lat")
                            lon = obj.get("lon")
                            if lat is not None and lon is not None:
                                return Position(
                                    lat=float(lat),
                                    lon=float(lon),
                                    alt_m=float(obj["alt"]) if "alt" in obj else None,
                                    source="gps",
                                )
        except (OSError, TimeoutError):
            pass
        return None
