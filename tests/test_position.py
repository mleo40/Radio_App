"""Position module: lat_lon_to_grid, Position, GPSReader."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from radio_app.core.position import GPSReader, Position, lat_lon_to_grid, position_from_config


# -- lat_lon_to_grid -----------------------------------------------------------


def test_grid_boston():
    # Boston MA: 42.36°N, -71.06°W → FN42
    assert lat_lon_to_grid(42.36, -71.06, precision=4) == "FN42"
    assert lat_lon_to_grid(42.36, -71.06, precision=6).startswith("FN42")


def test_grid_london():
    # London: 51.51°N, -0.13°W → IO91
    assert lat_lon_to_grid(51.51, -0.13, precision=4) == "IO91"


def test_grid_poles():
    # North Pole: lat90=180 → field S, sq 0; lon=0 → field J. Result: JS00.
    assert lat_lon_to_grid(90.0, 0.0, precision=4) == "JS00"
    # South Pole: lat90=0 → field A, sq 0; lon=0 → field J. Result: JA00.
    assert lat_lon_to_grid(-90.0, 0.0, precision=4) == "JA00"


def test_grid_antimeridian():
    # lon=-180 → lon180=0 → field A, sq 0; lat=0 → lat90=90 → field J, sq 0. Result: AJ00.
    assert lat_lon_to_grid(0.0, -180.0, precision=4) == "AJ00"
    # lon=179.9 → lon180=359.9 → field R (chr 65+17), sq 9; lat=0 → J, sq 0. Result: RJ90.
    assert lat_lon_to_grid(0.0, 179.9, precision=4) == "RJ90"


def test_grid_invalid_raises():
    with pytest.raises(ValueError):
        lat_lon_to_grid(91.0, 0.0)
    with pytest.raises(ValueError):
        lat_lon_to_grid(0.0, 181.0)


def test_grid_six_char_length():
    g = lat_lon_to_grid(42.36, -71.06, precision=6)
    assert len(g) == 6


# -- Position ------------------------------------------------------------------


def test_position_auto_grid():
    pos = Position(lat=42.3601, lon=-71.0589)
    assert len(pos.grid) == 4
    assert pos.grid == "FN42"


def test_position_str():
    pos = Position(lat=42.36, lon=-71.06, source="config")
    s = str(pos)
    assert "42" in s and "-71" in s and "FN42" in s


def test_position_source_default():
    pos = Position(lat=0.0, lon=0.0)
    assert pos.source == "manual"


# -- position_from_config ------------------------------------------------------


def test_position_from_config_with_latlon(tmp_path):
    from radio_app.config import Config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[position]\nlat = 42.36\nlon = -71.06\n")
    cfg = Config.load(cfg_file)
    pos = position_from_config(cfg)
    assert pos is not None
    assert abs(pos.lat - 42.36) < 0.001
    assert pos.source == "config"
    assert pos.grid == "FN42"


def test_position_from_config_empty(tmp_path):
    from radio_app.config import Config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("")
    cfg = Config.load(cfg_file)
    assert position_from_config(cfg) is None


# -- GPSReader -----------------------------------------------------------------


def _make_gpsd_tpv(lat: float = 42.36, lon: float = -71.06) -> bytes:
    """Build a minimal gpsd TPV JSON response."""
    version = json.dumps({"class": "VERSION", "release": "3.23"}) + "\n"
    devices = json.dumps({"class": "DEVICES", "devices": []}) + "\n"
    watch = json.dumps({"class": "WATCH"}) + "\n"
    tpv = json.dumps({
        "class": "TPV", "mode": 3, "lat": lat, "lon": lon, "alt": 10.5,
    }) + "\n"
    return (version + devices + watch + tpv).encode()


def _mock_socket(data: bytes):
    """Build a mock socket that returns ``data`` on recv."""
    sock = MagicMock()
    sock.__enter__ = lambda s: s
    sock.__exit__ = MagicMock(return_value=False)
    sock.recv.side_effect = [data, b""]
    return sock


def test_gps_reader_fix(monkeypatch):
    data = _make_gpsd_tpv(lat=42.36, lon=-71.06)
    with patch("socket.create_connection", return_value=_mock_socket(data)):
        pos = GPSReader().read()
    assert pos is not None
    assert abs(pos.lat - 42.36) < 0.001
    assert pos.source == "gps"
    assert pos.alt_m == pytest.approx(10.5)
    assert pos.grid == "FN42"


def test_gps_reader_no_fix(monkeypatch):
    # mode=1 means no fix
    data = (json.dumps({"class": "TPV", "mode": 1}) + "\n").encode()
    with patch("socket.create_connection", return_value=_mock_socket(data)):
        pos = GPSReader().read()
    assert pos is None


def test_gps_reader_connection_refused(monkeypatch):
    with patch("socket.create_connection", side_effect=OSError("refused")):
        pos = GPSReader().read()
    assert pos is None
