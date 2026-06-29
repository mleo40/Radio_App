"""Maidenhead grid square ↔ latitude/longitude conversions.

Supports 4-character (field+square, e.g. "FN31") and 6-character
(field+square+subsquare, e.g. "FN31pr") grid locators.
"""
from __future__ import annotations

import re

_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([a-x]{2})?$", re.IGNORECASE)


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
