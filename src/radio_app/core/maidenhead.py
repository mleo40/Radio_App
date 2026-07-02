"""Maidenhead grid square ↔ latitude/longitude conversions.

Supports 4-character (field+square, e.g. "FN31") and 6-character
(field+square+subsquare, e.g. "FN31pr") grid locators.
"""
from __future__ import annotations

import math
import re

_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([a-x]{2})?$", re.IGNORECASE)
_EARTH_RADIUS_KM = 6371.0088


def grid_to_latlon(grid: str) -> tuple[float, float]:
    """Return the (latitude, longitude) centre-point of a Maidenhead grid.

    Accepts 4-char (field+square) or 6-char (field+square+subsquare) locators.
    Returns the centre of the smallest specified tile.

    Raises ValueError for invalid input.
    """
    grid = grid.strip().upper()
    if not _GRID_RE.match(grid):
        raise ValueError(f"Invalid Maidenhead grid locator: {grid!r}")

    # Field (2 letters): A-R, each 20° lon × 10° lat
    lon = (ord(grid[0]) - ord('A')) * 20.0 - 180.0
    lat = (ord(grid[1]) - ord('A')) * 10.0 - 90.0

    # Square (2 digits): 0-9, each 2° lon × 1° lat
    lon += int(grid[2]) * 2.0
    lat += int(grid[3]) * 1.0

    if len(grid) >= 6:
        # Subsquare (2 letters): a-x, each 5′ lon × 2.5′ lat
        lon += (ord(grid[4]) - ord('A')) * (2.0 / 24.0)
        lat += (ord(grid[5]) - ord('A')) * (1.0 / 24.0)
        # Centre of subsquare tile
        lon += 1.0 / 24.0
        lat += 0.5 / 24.0
    else:
        # Centre of square tile
        lon += 1.0
        lat += 0.5

    return lat, lon


def latlon_to_grid(lat: float, lon: float, precision: int = 6) -> str:
    """Return the Maidenhead grid locator for a lat/lon point.

    ``precision`` must be 4 or 6 (default 6).
    """
    if precision not in (4, 6):
        raise ValueError("precision must be 4 or 6")

    lon += 180.0
    lat += 90.0

    field_lon = int(lon / 20)
    field_lat = int(lat / 10)
    lon -= field_lon * 20.0
    lat -= field_lat * 10.0

    sq_lon = int(lon / 2)
    sq_lat = int(lat)
    lon -= sq_lon * 2.0
    lat -= sq_lat * 1.0

    grid = (
        chr(ord('A') + field_lon)
        + chr(ord('A') + field_lat)
        + str(sq_lon)
        + str(sq_lat)
    )

    if precision == 6:
        sub_lon = int(lon * 12)
        sub_lat = int(lat * 24)
        sub_lon = min(sub_lon, 23)
        sub_lat = min(sub_lat, 23)
        grid += chr(ord('a') + sub_lon) + chr(ord('a') + sub_lat)

    return grid


def bearing_distance(grid1: str, grid2: str) -> tuple[float, float]:
    """Great-circle distance (km) and initial bearing (degrees, 0-360) from
    ``grid1`` to ``grid2``, both Maidenhead locators.

    Bearing is measured clockwise from true north (0 = N, 90 = E). Raises
    ValueError if either locator is invalid (via :func:`grid_to_latlon`).
    """
    lat1, lon1 = grid_to_latlon(grid1)
    lat2, lon2 = grid_to_latlon(grid2)

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    # Haversine distance.
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    distance_km = 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))

    # Initial bearing.
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        dlambda
    )
    bearing_deg = (math.degrees(math.atan2(y, x)) + 360) % 360

    return distance_km, bearing_deg


_COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def compass_point(bearing_deg: float) -> str:
    """16-point compass label for a bearing in degrees (e.g. 23 -> "NNE")."""
    idx = int((bearing_deg % 360) / 22.5 + 0.5) % 16
    return _COMPASS_POINTS[idx]


_MAP_WIDTH = 41
_MAP_HEIGHT = 19
_MAP_RADIUS_COLS = _MAP_WIDTH // 2 - 1
_MAP_RADIUS_ROWS = _MAP_HEIGHT // 2 - 1
_MAP_MARKER_CHARS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_MAP_YOU_CHAR = "◉"  # ◉


def render_compass_map(
    my_grid: str, entries: list[tuple[str, str, float, float]]
) -> str:
    """ASCII compass plot centred on ``my_grid``, radar-style.

    ``entries`` is ``(label, grid, distance_km, bearing_deg)`` tuples, ideally
    pre-sorted by distance (nearest first) so marker letters/numbers assign in
    a predictable order. Distances are scaled so the farthest entry sits at
    the edge of the plot; rows/columns are scaled independently to
    approximate a circle despite terminal characters being taller than wide.
    Text-only output — no terminal graphics dependency, works in the TUI's
    RichLog and plain CLI stdout alike.
    """
    center_r, center_c = _MAP_HEIGHT // 2, _MAP_WIDTH // 2
    canvas = [[" "] * _MAP_WIDTH for _ in range(_MAP_HEIGHT)]
    canvas[center_r][center_c] = _MAP_YOU_CHAR

    max_dist = max((e[2] for e in entries), default=0.0) or 1.0
    legend: list[str] = []
    for i, (label, grid, dist, brg) in enumerate(entries):
        marker = _MAP_MARKER_CHARS[i % len(_MAP_MARKER_CHARS)]
        rad = math.radians(brg)
        frac = dist / max_dist
        dc_f = frac * _MAP_RADIUS_COLS * math.sin(rad)
        dr_f = -frac * _MAP_RADIUS_ROWS * math.cos(rad)
        dc, dr = round(dc_f), round(dr_f)
        # A station much closer than the farthest one (including one in your
        # own grid square, dist == 0) can round to the exact centre and
        # silently overwrite the "You" marker. Nudge it 1 cell out instead so
        # You always stays visible and distinct; direction is arbitrary but
        # consistent (east) when co-located, since bearing is meaningless.
        if dc == 0 and dr == 0:
            if dist == 0 or abs(dc_f) >= abs(dr_f):
                dc = 1 if (dist == 0 or math.sin(rad) >= 0) else -1
            else:
                dr = 1 if -math.cos(rad) >= 0 else -1
        r = max(0, min(_MAP_HEIGHT - 1, center_r + dr))
        c = max(0, min(_MAP_WIDTH - 1, center_c + dc))
        canvas[r][c] = marker
        legend.append(
            f"  {marker}  {label:<16} {grid:<8} {dist:7.0f} km  "
            f"{brg:5.1f}° {compass_point(brg)}"
        )

    lines = ["N".center(_MAP_WIDTH)]
    for r, row in enumerate(canvas):
        left = "W" if r == center_r else " "
        right = "E" if r == center_r else ""
        lines.append(left + "".join(row) + right)
    lines.append("S".center(_MAP_WIDTH))
    lines.append("")
    lines.append(f"You ({_MAP_YOU_CHAR}): {my_grid}")
    if legend:
        lines.extend(legend)
    else:
        lines.append("(no other stations with a known grid square)")
    return "\n".join(lines)
