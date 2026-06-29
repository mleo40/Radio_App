"""Internet weather fetch helpers (no API key required).

Two providers, tried in order:
  1. NWS  (api.weather.gov) — US only, authoritative, rich forecast text.
  2. Open-Meteo (api.open-meteo.com) — global, current conditions + daily summary.

All I/O is async and uses only the stdlib (urllib / asyncio); no third-party HTTP
library is required.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds per HTTP request
_USER_AGENT = "Radio_App/1.0 (github.com/mleo40/Radio_App)"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

async def _get_json(url: str) -> Any:
    """Async wrapper around a blocking urllib GET that returns parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    def _blocking() -> Any:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    return await asyncio.get_event_loop().run_in_executor(None, _blocking)


# ---------------------------------------------------------------------------
# NWS (US only)
# ---------------------------------------------------------------------------

async def fetch_nws(lat: float, lon: float) -> str:
    """Return a plain-text NWS forecast for the given coordinates.

    Makes two HTTP calls: /points → forecast URL → periods.
    Raises on network error or non-US coordinates (NWS only covers CONUS/territories).
    """
    points = await _get_json(
        f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
    )
    forecast_url: str = points["properties"]["forecast"]
    forecast = await _get_json(forecast_url)
    periods: list[dict] = forecast["properties"]["periods"][:4]
    lines = []
    for p in periods:
        name = p.get("name", "")
        detail = p.get("detailedForecast") or p.get("shortForecast", "")
        wind = p.get("windSpeed", "")
        direction = p.get("windDirection", "")
        temp = p.get("temperature")
        unit = "°F" if p.get("temperatureUnit") == "F" else "°C"
        temp_str = f"{temp}{unit}" if temp is not None else ""
        parts = [x for x in [temp_str, f"{wind} {direction}".strip()] if x]
        summary = f"{detail}  [{', '.join(parts)}]" if parts else detail
        lines.append(f"{name}: {summary}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Open-Meteo (global fallback)
# ---------------------------------------------------------------------------

_WMO_CODES: dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
}


async def fetch_open_meteo(lat: float, lon: float) -> str:
    """Return plain-text current conditions + 24-hour summary from Open-Meteo."""
    params = (
        f"latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,"
        "wind_direction_10m,weather_code,surface_pressure"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_sum,wind_speed_10m_max"
        "&wind_speed_unit=mph&temperature_unit=fahrenheit"
        "&timezone=auto&forecast_days=2"
    )
    data = await _get_json(f"https://api.open-meteo.com/v1/forecast?{params}")

    cur = data.get("current", {})
    temp = cur.get("temperature_2m")
    humid = cur.get("relative_humidity_2m")
    wind_spd = cur.get("wind_speed_10m")
    wind_dir = cur.get("wind_direction_10m")
    code = cur.get("weather_code", 0)
    pressure = cur.get("surface_pressure")

    condition = _WMO_CODES.get(code, f"WMO {code}")
    dir_str = _bearing_to_compass(wind_dir) if wind_dir is not None else ""
    now_parts = [condition]
    if temp is not None:
        now_parts.append(f"{temp:.0f}°F")
    if wind_spd is not None:
        now_parts.append(f"wind {wind_spd:.0f} mph {dir_str}".strip())
    if humid is not None:
        now_parts.append(f"humidity {humid:.0f}%")
    if pressure is not None:
        now_parts.append(f"{pressure:.0f} mb")
    current_line = "Now: " + "  ".join(now_parts)

    daily = data.get("daily", {})
    codes = daily.get("weather_code", [])
    hi = daily.get("temperature_2m_max", [])
    lo = daily.get("temperature_2m_min", [])
    precip = daily.get("precipitation_sum", [])
    forecast_lines = []
    day_names = ["Today", "Tomorrow"]
    for i, name in enumerate(day_names):
        if i >= len(codes):
            break
        cond = _WMO_CODES.get(codes[i], f"WMO {codes[i]}")
        hi_s = f"H {hi[i]:.0f}°F" if i < len(hi) and hi[i] is not None else ""
        lo_s = f"L {lo[i]:.0f}°F" if i < len(lo) and lo[i] is not None else ""
        pr_s = f"precip {precip[i]:.2f}\"" if i < len(precip) and precip[i] else ""
        parts = [x for x in [cond, hi_s, lo_s, pr_s] if x]
        forecast_lines.append(f"{name}: {'  '.join(parts)}")

    return "\n".join([current_line] + forecast_lines)


def _bearing_to_compass(degrees: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(degrees / 22.5) % 16
    return dirs[idx]


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

async def fetch_weather(lat: float, lon: float) -> tuple[str, str]:
    """Fetch weather from NWS (US) or Open-Meteo (global fallback).

    Returns ``(text, source_label)`` where source_label is ``"NWS"`` or
    ``"Open-Meteo"``.  Raises RuntimeError if both providers fail.
    """
    try:
        text = await fetch_nws(lat, lon)
        return text, "NWS"
    except Exception as exc:  # noqa: BLE001
        log.debug("NWS fetch failed (%s), trying Open-Meteo", exc)

    try:
        text = await fetch_open_meteo(lat, lon)
        return text, "Open-Meteo"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Both weather providers failed: {exc}") from exc
