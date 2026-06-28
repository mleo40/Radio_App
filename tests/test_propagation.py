"""Tests for core/propagation.py — HamQSL solar conditions."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from radio_app.core.propagation import (
    BAND_GROUP,
    fetch_sync,
)

# Minimal valid HamQSL XML (mirrors real structure)
_GOOD_XML = b"""<?xml version="1.0" encoding="UTF-8" ?>
<solar>
  <solardata>
    <solarflux>188</solarflux>
    <sunspots>119</sunspots>
    <aindex>7</aindex>
    <kindex>2</kindex>
    <geomagfield>QUIET</geomagfield>
    <calculatedconditions>
      <band name="80m-40m" time="day">Poor</band>
      <band name="80m-40m" time="night">Good</band>
      <band name="30m-20m" time="day">Good</band>
      <band name="30m-20m" time="night">Good</band>
      <band name="17m-15m" time="day">Fair</band>
      <band name="17m-15m" time="night">Poor</band>
      <band name="12m-10m" time="day">Good</band>
      <band name="12m-10m" time="night">Poor</band>
    </calculatedconditions>
  </solardata>
</solar>"""


def _make_response(xml: bytes):
    """Fake urllib response context manager."""
    class _Resp:
        def read(self):
            return xml
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    return _Resp()


def test_parse_xml_fields():
    with patch("urllib.request.urlopen", return_value=_make_response(_GOOD_XML)):
        d = fetch_sync()
    assert d.sfi == 188
    assert d.ssn == 119
    assert d.a_index == 7
    assert d.k_index == 2
    assert d.geo_field == "QUIET"


def test_parse_xml_conditions():
    with patch("urllib.request.urlopen", return_value=_make_response(_GOOD_XML)):
        d = fetch_sync()
    assert d.conditions["80m-40m"]["day"] == "Poor"
    assert d.conditions["80m-40m"]["night"] == "Good"
    assert d.conditions["30m-20m"]["day"] == "Good"
    assert d.conditions["12m-10m"]["night"] == "Poor"


def test_condition_for_band():
    with patch("urllib.request.urlopen", return_value=_make_response(_GOOD_XML)):
        d = fetch_sync()
    assert d.condition_for("20m") == {"day": "Good", "night": "Good"}
    assert d.condition_for("80m") == {"day": "Poor", "night": "Good"}
    assert d.condition_for("17m") == {"day": "Fair", "night": "Poor"}
    assert d.condition_for("10m") == {"day": "Good", "night": "Poor"}


def test_condition_for_unknown_band():
    with patch("urllib.request.urlopen", return_value=_make_response(_GOOD_XML)):
        d = fetch_sync()
    assert d.condition_for("2m") == {}
    assert d.condition_for("70cm") == {}
    assert d.condition_for("160m") == {}


def test_band_group_mapping():
    assert BAND_GROUP["80m"] == "80m-40m"
    assert BAND_GROUP["40m"] == "80m-40m"
    assert BAND_GROUP["20m"] == "30m-20m"
    assert BAND_GROUP["10m"] == "12m-10m"


def test_parse_partial_xml_no_crash():
    """Missing bands or elements should not raise."""
    xml = b"""<solar><solardata>
        <solarflux>150</solarflux>
        <sunspots>80</sunspots>
        <aindex>3</aindex>
        <kindex>1</kindex>
        <calculatedconditions>
          <band name="30m-20m" time="day">Good</band>
        </calculatedconditions>
    </solardata></solar>"""
    with patch("urllib.request.urlopen", return_value=_make_response(xml)):
        d = fetch_sync()
    assert d.sfi == 150
    assert d.condition_for("20m") == {"day": "Good"}
    assert d.condition_for("80m") == {}


def test_fetch_raises_on_missing_solardata():
    xml = b"<solar><junk/></solar>"
    with patch("urllib.request.urlopen", return_value=_make_response(xml)):
        with pytest.raises(ValueError, match="solardata"):
            fetch_sync()
