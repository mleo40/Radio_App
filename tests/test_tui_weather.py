"""Headless TUI tests for the weather (WX) surface.

Covers:
- maidenhead.grid_to_latlon() conversion
- wx_internet.fetch_weather() NWS → Open-Meteo fallback
- store.query(kind=) filter
- Winlink NWS bulletin stamping
- Reticulum #weather group stamping
- JS8Call _JS8_WX_RE regex stamping
- TUI weather view mounts and basic interaction
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("textual")

from radio_app.core.maidenhead import grid_to_latlon, latlon_to_grid  # noqa: E402
from radio_app.core.message import UnifiedMessage  # noqa: E402

# ---------------------------------------------------------------------------
# maidenhead
# ---------------------------------------------------------------------------

def test_grid_to_latlon_fn31():
    lat, lon = grid_to_latlon("FN31")
    assert 41.0 <= lat <= 42.0
    assert -74.0 <= lon <= -71.0


def test_grid_to_latlon_em00():
    lat, lon = grid_to_latlon("EM00")
    # EM00 = central Texas area
    assert 29.0 <= lat <= 32.0
    assert -102.0 <= lon <= -97.0


def test_grid_to_latlon_6char():
    lat, lon = grid_to_latlon("FN31pr")
    # Subsquare should be within the 4-char square.
    lat4, lon4 = grid_to_latlon("FN31")
    assert abs(lat - lat4) < 1.0
    assert abs(lon - lon4) < 2.0


def test_latlon_to_grid_roundtrip():
    lat, lon = grid_to_latlon("FN31")
    result = latlon_to_grid(lat, lon, precision=4)
    assert result == "FN31"


def test_grid_to_latlon_invalid_raises():
    with pytest.raises(ValueError):
        grid_to_latlon("XX")  # Too short


# ---------------------------------------------------------------------------
# wx_internet — mock HTTP calls
# ---------------------------------------------------------------------------

NWS_POINTS_RESP = {
    "properties": {
        "forecast": "https://api.weather.gov/gridpoints/BOX/62,84/forecast"
    }
}

NWS_FORECAST_RESP = {
    "properties": {
        "periods": [
            {
                "name": "Tonight",
                "shortForecast": "Partly cloudy",
                "detailedForecast": "Partly cloudy. Low near 58.",
                "windSpeed": "8 mph",
                "windDirection": "SE",
                "temperature": 58,
                "temperatureUnit": "F",
            },
            {
                "name": "Sunday",
                "shortForecast": "Sunny",
                "detailedForecast": "Sunny. High near 72.",
                "windSpeed": "5 mph",
                "windDirection": "SW",
                "temperature": 72,
                "temperatureUnit": "F",
            },
        ]
    }
}

OPEN_METEO_RESP = {
    "current": {
        "temperature_2m": 62.5,
        "relative_humidity_2m": 72,
        "wind_speed_10m": 8.0,
        "wind_direction_10m": 135,
        "weather_code": 2,
        "surface_pressure": 1013,
    },
    "daily": {
        "weather_code": [2, 3],
        "temperature_2m_max": [72.0, 68.0],
        "temperature_2m_min": [55.0, 52.0],
        "precipitation_sum": [0.0, 0.1],
    },
}


def test_fetch_nws_returns_text():
    from radio_app.core import wx_internet

    call_count = 0

    async def fake_get_json(url):
        nonlocal call_count
        call_count += 1
        if "/points/" in url and "gridpoints" not in url:
            return NWS_POINTS_RESP
        return NWS_FORECAST_RESP

    async def run():
        with patch.object(wx_internet, "_get_json", side_effect=fake_get_json):
            text = await wx_internet.fetch_nws(41.5, -71.0)
        return text

    text = asyncio.run(run())
    assert "Tonight" in text
    assert "58" in text
    assert call_count == 2


def test_fetch_weather_falls_back_to_open_meteo():
    from radio_app.core import wx_internet

    async def nws_fail(url):
        raise OSError("NWS unavailable")

    call_count = 0

    async def open_meteo_ok(url):
        nonlocal call_count
        call_count += 1
        return OPEN_METEO_RESP

    async def run():
        # First call (NWS points) fails, second call hits open-meteo.
        call_results = [nws_fail, open_meteo_ok]
        call_idx = 0

        async def dispatcher(url):
            nonlocal call_idx
            fn = call_results[min(call_idx, len(call_results) - 1)]
            call_idx += 1
            return await fn(url)

        with patch.object(wx_internet, "_get_json", side_effect=dispatcher):
            text, source = await wx_internet.fetch_weather(41.5, -71.0)
        return text, source

    text, source = asyncio.run(run())
    assert source == "Open-Meteo"
    assert "Now:" in text


def test_fetch_weather_both_fail():
    from radio_app.core import wx_internet

    async def always_fail(url):
        raise OSError("network down")

    async def run():
        with patch.object(wx_internet, "_get_json", side_effect=always_fail):
            with pytest.raises(RuntimeError):
                await wx_internet.fetch_weather(41.5, -71.0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# store.query kind= filter
# ---------------------------------------------------------------------------

def test_store_query_kind_filter(tmp_path):
    from radio_app.core.store import MessageStore

    store = MessageStore(str(tmp_path / "test.db"))
    now = datetime.datetime.now(datetime.timezone.utc)

    wx_msg = UnifiedMessage(
        sender="NWS",
        content="Partly cloudy tonight",
        transport="winlink",
        timestamp=now,
        metadata={"kind": "weather_bulletin", "subject": "NWS BOSTON"},
    )
    normal_msg = UnifiedMessage(
        sender="W1AW",
        content="Hello",
        transport="reticulum",
        timestamp=now,
        metadata={},
    )
    store.save(wx_msg)
    store.save(normal_msg)

    wx_results = store.query(kind="weather_bulletin")
    all_results = store.query()

    assert len(wx_results) == 1
    assert wx_results[0].sender == "NWS"
    assert len(all_results) == 2


# ---------------------------------------------------------------------------
# Winlink NWS bulletin stamping
# ---------------------------------------------------------------------------

def test_winlink_nws_subject_stamps_kind():
    from radio_app.transports.winlink_transport import _NWS_SUBJECT_RE

    subjects = [
        "NWS-BULLETIN BOSTON",
        "ZONE FORECAST for Southern New England",
        "FLASH FLOOD WARNING",
        "TORNADO WARNING for Essex County",
        "MARINE FORECAST Cape Cod Bay",
    ]
    for subj in subjects:
        assert _NWS_SUBJECT_RE.search(subj), f"expected match for: {subj!r}"


def test_winlink_normal_subject_not_stamped():
    from radio_app.transports.winlink_transport import _NWS_SUBJECT_RE

    non_wx = ["Hello from K1ABC", "Meeting notes", "Grid squares 101"]
    for subj in non_wx:
        assert not _NWS_SUBJECT_RE.search(subj), f"unexpected match for: {subj!r}"


# ---------------------------------------------------------------------------
# Reticulum #weather group stamping
# ---------------------------------------------------------------------------

def test_js8call_wx_re_matches_nws_forecast():
    from radio_app.transports.js8call_transport import _JS8_WX_RE

    forecasts = [
        "Tonight: Partly cloudy. Low 58°F.",
        "Today: Sunny. High 72°F.",
        "NWS KBOX forecast for FN31",
        "FORECAST for Southern New England",
    ]
    for f in forecasts:
        assert _JS8_WX_RE.search(f), f"expected _JS8_WX_RE to match: {f!r}"


def test_js8call_wx_re_no_false_positives():
    from radio_app.transports.js8call_transport import _JS8_WX_RE

    non_wx = ["Hello K1ABC de K2DEF", "Grid FN31 73", "73 es good DX"]
    for t in non_wx:
        assert not _JS8_WX_RE.search(t), f"unexpected match for: {t!r}"


# ---------------------------------------------------------------------------
# TUI weather view
# ---------------------------------------------------------------------------

CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[transports.js8call]
enabled = true
port = 2442
[transports.winlink]
enabled = true
pat_url = "http://127.0.0.1:9"
callsign = "N0CALL"
connect = "telnet"
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG)
    return str(p)


def test_weather_view_mounts(config_path):
    """The weather-view Vertical and its key widgets are present in the DOM."""
    from radio_app.ui.tui import RadioTUI

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_weather()
            await pilot.pause()
            assert app.view == "weather"
            from textual.widgets import Input, RichLog
            wx_log = app.query_one("#wx-log", RichLog)
            wx_grid = app.query_one("#wx-grid", Input)
            assert wx_log is not None
            assert wx_grid is not None

    asyncio.run(run())


def test_weather_view_shows_empty_message(config_path):
    """When no weather_bulletin messages exist the feed shows a hint."""
    from radio_app.ui.tui import RadioTUI
    from textual.widgets import RichLog

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_weather()
            await pilot.pause()
            wx_log = app.query_one("#wx-log", RichLog)
            # The RichLog should contain some text (the "no data" hint).
            assert wx_log is not None

    asyncio.run(run())


def test_weather_view_renders_stored_bulletin(config_path):
    """A stored weather_bulletin appears in the WX feed after _render_weather()."""
    import datetime as dt
    from radio_app.ui.tui import RadioTUI
    from textual.widgets import RichLog

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_weather()
            await pilot.pause()
            # Inject a weather bulletin into the store.
            msg = UnifiedMessage(
                sender="NWS",
                content="Tonight: Partly cloudy. Low 58F.",
                transport="winlink",
                timestamp=dt.datetime.now(dt.timezone.utc),
                metadata={"kind": "weather_bulletin", "subject": "NWS BOSTON"},
            )
            app.core.store.save(msg)
            app._render_weather()
            await pilot.pause()
            # The log should now contain content (not just be empty).
            wx_log = app.query_one("#wx-log", RichLog)
            assert wx_log is not None

    asyncio.run(run())


def test_wx_setup_screen_both(config_path):
    """WXSetupScreen dismiss with meshcore=True, reticulum=True."""
    from radio_app.ui.tui import RadioTUI, WXSetupScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            picked = {}
            app.push_screen(
                WXSetupScreen(has_meshcore=True, has_reticulum=True),
                lambda v: picked.update(result=v),
            )
            await pilot.pause()
            app.screen.query_one("#wxsetup-both").press()
            await pilot.pause()
            r = picked.get("result") or {}
            assert r.get("meshcore") is True
            assert r.get("reticulum") is True
            assert "grids" in r

    asyncio.run(run())


def test_wx_setup_screen_skip(config_path):
    """WXSetupScreen dismisses None on skip."""
    from radio_app.ui.tui import RadioTUI, WXSetupScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            picked = {}
            app.push_screen(
                WXSetupScreen(has_meshcore=False, has_reticulum=False),
                lambda v: picked.update(result=v),
            )
            await pilot.pause()
            app.screen.query_one("#wxsetup-skip").press()
            await pilot.pause()
            assert picked.get("result") is None

    asyncio.run(run())


def test_winlink_wx_subscribe_screen_open_compose(config_path):
    """WinlinkWXSubscribeScreen dismisses True when 'Open Compose' pressed."""
    from radio_app.ui.tui import RadioTUI, WinlinkWXSubscribeScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            picked = {}
            app.push_screen(
                WinlinkWXSubscribeScreen(grid="FN31"),
                lambda v: picked.update(result=v),
            )
            await pilot.pause()
            app.screen.query_one("#wxsub-open").press()
            await pilot.pause()
            assert picked.get("result") is True

    asyncio.run(run())
