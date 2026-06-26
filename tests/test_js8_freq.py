"""Tests for JS8Call band-plan helpers and dial-frequency tracking (no sockets).

``band_for_freq`` / ``dial_for_band`` are pure; the RIG.FREQ caching is exercised
through the transport's synchronous ``_update_freq`` (the read loop just feeds it).
"""

from __future__ import annotations

from radio_app.transports.js8call_transport import (
    JS8_BAND_DIAL_HZ,
    JS8CallTransport,
    band_for_freq,
    dial_for_band,
)


def test_band_for_freq_known_bands():
    assert band_for_freq(14_078_000) == "20m"
    assert band_for_freq(7_078_000) == "40m"
    assert band_for_freq(10_130_000) == "30m"
    assert band_for_freq(50_318_000) == "6m"


def test_band_for_freq_out_of_band_and_none():
    assert band_for_freq(12_000_000) is None  # between 30m and 20m
    assert band_for_freq(None) is None
    assert band_for_freq(0) is None


def test_dial_for_band_normalises_name():
    assert dial_for_band("20m") == JS8_BAND_DIAL_HZ["20m"]
    assert dial_for_band("20") == JS8_BAND_DIAL_HZ["20m"]  # 'm' appended
    assert dial_for_band(" 40M ") == JS8_BAND_DIAL_HZ["40m"]
    assert dial_for_band("nonsense") is None
    assert dial_for_band("") is None


def test_every_default_dial_maps_back_to_its_band():
    for band, hz in JS8_BAND_DIAL_HZ.items():
        assert band_for_freq(hz) == band


def test_update_freq_caches_dial_and_band():
    t = JS8CallTransport({})
    assert t.dial_freq is None
    assert t.current_band() is None
    t._update_freq({"DIAL": 14_078_000, "OFFSET": 1500})
    assert t.dial_freq == 14_078_000
    assert t.current_band() == "20m"


def test_update_freq_ignores_bad_values():
    t = JS8CallTransport({})
    t._update_freq({"DIAL": 7_078_000})
    t._update_freq({"DIAL": "not-a-number"})  # ignored, keeps previous
    assert t.dial_freq == 7_078_000


def test_radio_status_snapshot_without_cat():
    t = JS8CallTransport({})
    snap = t.radio_status_snapshot()
    assert snap["cat"] is False
    assert snap["dial"] is None
    assert snap["freq"] is None
    assert snap["band"] is None
    assert snap["speed"] == ""
    assert snap["selected"] == ""


def test_station_status_event_populates_snapshot():
    t = JS8CallTransport({})
    # Mimic the params JS8Call sends in a STATION.STATUS event.
    t._update_freq({"DIAL": 14_078_000, "OFFSET": 1200})
    t._update_status({"SPEED": 0, "SELECTED": "ke7xyz"})
    snap = t.radio_status_snapshot()
    assert snap["cat"] is True
    assert snap["dial"] == 14_078_000
    assert snap["offset"] == 1200
    assert snap["freq"] == 14_079_200
    assert snap["band"] == "20m"
    assert snap["speed"] == "normal"          # 0 -> normal
    assert snap["selected"] == "KE7XYZ"       # upper-cased


def test_update_status_maps_speed_names_and_passthrough():
    t = JS8CallTransport({})
    t._update_status({"SPEED": 2})
    assert t.radio_status_snapshot()["speed"] == "turbo"
    t._update_status({"SPEED": "fancy"})      # unknown -> passthrough
    assert t.radio_status_snapshot()["speed"] == "fancy"



