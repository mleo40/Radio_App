"""Tests for Maidenhead grid conversions, bearing/distance, and the ASCII map.

core/maidenhead.py had no test coverage at all before this file -- covers the
pre-existing grid_to_latlon/latlon_to_grid round-trip too, not just the new
bearing_distance/compass_point/render_compass_map additions.
"""
from __future__ import annotations

import pytest

from radio_app.core.maidenhead import (
    _MAP_HEIGHT,
    bearing_distance,
    compass_point,
    grid_to_latlon,
    latlon_to_grid,
    render_compass_map,
)

# ---------------------------------------------------------------------------
# grid_to_latlon / latlon_to_grid (pre-existing, previously untested)
# ---------------------------------------------------------------------------

def test_grid_to_latlon_4char():
    lat, lon = grid_to_latlon("FN31")
    assert lat == pytest.approx(41.5, abs=0.5)
    assert lon == pytest.approx(-72.0, abs=1.0)


def test_grid_to_latlon_6char_more_precise_than_4char():
    lat4, lon4 = grid_to_latlon("FN31")
    lat6, lon6 = grid_to_latlon("FN31pr")
    # Same square, but the 6-char centre shouldn't be identical to the 4-char
    # (coarser) centre unless by coincidence.
    assert (lat4, lon4) != (lat6, lon6)


def test_grid_to_latlon_rejects_invalid():
    with pytest.raises(ValueError):
        grid_to_latlon("not-a-grid")


def test_grid_to_latlon_case_insensitive():
    assert grid_to_latlon("fn31") == grid_to_latlon("FN31")


def test_latlon_to_grid_round_trips_4char():
    lat, lon = grid_to_latlon("FN31")
    assert latlon_to_grid(lat, lon, precision=4) == "FN31"


def test_latlon_to_grid_round_trips_6char():
    lat, lon = grid_to_latlon("FN31pr")
    assert latlon_to_grid(lat, lon, precision=6) == "FN31pr"


def test_latlon_to_grid_rejects_bad_precision():
    with pytest.raises(ValueError):
        latlon_to_grid(41.5, -72.0, precision=5)


# ---------------------------------------------------------------------------
# bearing_distance
# ---------------------------------------------------------------------------

def test_bearing_distance_same_grid_is_zero():
    dist, brg = bearing_distance("FN31", "FN31")
    assert dist == pytest.approx(0.0, abs=0.01)


def test_bearing_distance_is_positive_and_bounded():
    dist, brg = bearing_distance("FN31pr", "FN42")
    assert dist > 0
    assert 0.0 <= brg < 360.0


def test_bearing_distance_roughly_matches_known_geography():
    # FN31 (~Hartford CT) to FN42 (~Boston area): real distance is on the
    # order of 150km, roughly ENE. Loose bounds -- this is a sanity check on
    # the formula, not a precision test (grid centres aren't exact cities).
    dist, brg = bearing_distance("FN31pr", "FN42")
    assert 100 < dist < 250
    assert 30 < brg < 90  # somewhere in the NE quadrant


def test_bearing_distance_is_antisymmetric_in_direction():
    # Bearing from A->B and B->A should point roughly opposite ways (mod
    # small great-circle convergence effects over short distances).
    _, brg_ab = bearing_distance("FN31", "FN42")
    _, brg_ba = bearing_distance("FN42", "FN31")
    diff_from_opposite = abs((brg_ab - brg_ba) % 360 - 180)
    assert diff_from_opposite < 5  # nearly exactly opposite for a short hop


def test_bearing_distance_rejects_invalid_grid():
    with pytest.raises(ValueError):
        bearing_distance("FN31", "nope")


# ---------------------------------------------------------------------------
# compass_point
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "deg,expected",
    [
        (0, "N"),
        (45, "NE"),
        (90, "E"),
        (135, "SE"),
        (180, "S"),
        (225, "SW"),
        (270, "W"),
        (315, "NW"),
        (359, "N"),
        (360, "N"),
        (-1 % 360, "N"),
    ],
)
def test_compass_point(deg, expected):
    assert compass_point(deg) == expected


# ---------------------------------------------------------------------------
# render_compass_map
# ---------------------------------------------------------------------------

def test_render_compass_map_shows_you_marker_with_no_entries():
    text = render_compass_map("FN31", [])
    assert "You (" in text
    assert "FN31" in text
    assert "no other stations" in text.lower()


def test_render_compass_map_includes_all_labels_in_legend():
    entries = [
        ("Alice", "FN42", 150.0, 60.0),
        ("Bob", "EM73", 1200.0, 210.0),
    ]
    text = render_compass_map("FN31", entries)
    assert "Alice" in text
    assert "Bob" in text
    assert "FN42" in text
    assert "EM73" in text


def test_render_compass_map_has_compass_edges():
    text = render_compass_map("FN31", [("Alice", "FN42", 150.0, 60.0)])
    lines = text.splitlines()
    assert lines[0].strip() == "N"
    assert any(line.strip() == "S" for line in lines)
    assert any(line.startswith("W") for line in lines)
    assert any(line.rstrip().endswith("E") for line in lines)


def test_render_compass_map_colocated_station_does_not_hide_you_marker():
    # Regression: a favorite in your own exact grid square (dist == 0, a
    # meaningless bearing of 0) also has to not land on the You marker.
    entries = [("SameGrid", "FN31", 0.0, 0.0)]
    text = render_compass_map("FN31", entries)
    center_line = text.splitlines()[1 + _MAP_HEIGHT // 2]
    assert "◉" in center_line
    assert "1" in center_line


def test_render_compass_map_nearby_station_does_not_hide_you_marker():
    # Regression: a station much closer than the farthest one can round to
    # the exact centre cell and silently overwrite the "You" marker.
    entries = [
        ("Nearby", "FN31", 35.0, 223.7),   # rounds to (0,0) offset at this scale
        ("FarAway", "EM73", 1415.0, 233.7),
    ]
    text = render_compass_map("FN31pr", entries)
    assert "◉" in text
    center_line = text.splitlines()[1 + _MAP_HEIGHT // 2]
    assert "◉" in center_line


def test_render_compass_map_handles_many_entries_without_crashing():
    entries = [
        (f"Station{i}", "FN31", float(i + 1) * 10, float(i * 17 % 360))
        for i in range(40)  # more than the marker alphabet length
    ]
    text = render_compass_map("FN31", entries)
    assert "Station0" in text
    assert "Station39" in text
