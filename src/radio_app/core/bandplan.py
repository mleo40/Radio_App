"""Offline band-plan and EmComm frequency reference.

Covers JS8Call calling frequencies, common EmComm simplex/net frequencies,
and select Winlink P2P/RMS spot frequencies. Intentionally minimal — a
field-usable reference, not a comprehensive database.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrequencyEntry:
    band: str              # "40m", "20m", etc.
    freq_khz: float        # center / operating frequency in kHz
    mode: str              # "JS8", "WINLINK", "SSB", "FM", "CW"
    region: str            # "US", "EU", "INTL"
    transport: str | None  # "js8call", "winlink", or None
    notes: str = ""


# JS8Call standard calling frequencies (js8call.groups.io consensus 2024)
_JS8_CALLING = [
    FrequencyEntry("160m",  1_842.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("80m",   3_578.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("60m",   5_357.0, "JS8", "US",   "js8call", "CH3 center"),
    FrequencyEntry("40m",   7_078.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("30m",  10_130.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("20m",  14_078.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("17m",  18_104.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("15m",  21_078.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("12m",  24_922.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("10m",  28_078.0, "JS8", "INTL", "js8call", "calling"),
    FrequencyEntry("6m",   50_318.0, "JS8", "US",   "js8call", "calling"),
]

# Common US EmComm / ARES / RACES frequencies
_EMCOMM_US = [
    FrequencyEntry("80m",    3_985.0, "SSB", "US", None, "ARES national calling"),
    FrequencyEntry("80m",    3_992.5, "SSB", "US", None, "SKYWARN"),
    FrequencyEntry("40m",    7_290.0, "SSB", "US", None, "ARES national calling"),
    FrequencyEntry("40m",    7_240.0, "SSB", "US", None, "RACES/ARES alternate"),
    FrequencyEntry("20m",   14_300.0, "SSB", "US", None, "IARU emergency"),
    FrequencyEntry("20m",   14_325.0, "SSB", "US", None, "SATERN/Salvation Army"),
    FrequencyEntry("60m",    4_724.0, "SSB", "US", None, "SHARES primary"),
    FrequencyEntry("60m",    6_804.0, "SSB", "US", None, "SHARES alternate"),
    FrequencyEntry("2m",   146_520.0, "FM",  "US", None, "national FM simplex calling"),
    FrequencyEntry("2m",   146_550.0, "FM",  "US", None, "ARES/RACES alternate"),
    FrequencyEntry("70cm", 446_000.0, "FM",  "US", None, "70cm simplex calling"),
]

# Select Winlink P2P/RMS spot frequencies (regional starting guide only)
_WINLINK = [
    FrequencyEntry("80m",   3_596.0, "WINLINK", "US",   "winlink", "P2P/RMS"),
    FrequencyEntry("40m",   7_101.0, "WINLINK", "US",   "winlink", "P2P/RMS"),
    FrequencyEntry("40m",   7_103.5, "WINLINK", "INTL", "winlink", "P2P/RMS"),
    FrequencyEntry("30m",  10_147.6, "WINLINK", "INTL", "winlink", "P2P/RMS"),
    FrequencyEntry("20m",  14_103.0, "WINLINK", "US",   "winlink", "P2P/RMS"),
    FrequencyEntry("20m",  14_109.0, "WINLINK", "INTL", "winlink", "P2P/RMS"),
    FrequencyEntry("17m",  18_104.6, "WINLINK", "INTL", "winlink", "P2P/RMS"),
    FrequencyEntry("15m",  21_096.0, "WINLINK", "INTL", "winlink", "P2P/RMS"),
]

ALL_ENTRIES: list[FrequencyEntry] = _JS8_CALLING + _EMCOMM_US + _WINLINK

_BAND_ORDER = [
    "160m", "80m", "60m", "40m", "30m", "20m", "17m",
    "15m", "12m", "10m", "6m", "2m", "70cm",
]


def lookup(
    *,
    band: str | None = None,
    mode: str | None = None,
    region: str | None = None,
    transport: str | None = None,
) -> list[FrequencyEntry]:
    """Filter the band-plan table. All filters are AND-combined; None = no filter.

    ``region`` matches "INTL" entries for any region value (INTL applies
    everywhere). ``band`` and ``mode`` are case-insensitive.
    """
    results = list(ALL_ENTRIES)
    if band:
        band_lc = band.lower()
        results = [e for e in results if e.band.lower() == band_lc]
    if mode:
        mode_uc = mode.upper()
        results = [e for e in results if e.mode.upper() == mode_uc]
    if region:
        r = region.upper()
        results = [e for e in results if e.region == r or e.region == "INTL"]
    if transport:
        results = [e for e in results if e.transport == transport]
    order = {b: i for i, b in enumerate(_BAND_ORDER)}
    results.sort(key=lambda e: (order.get(e.band, 99), e.freq_khz))
    return results


_BAND_EDGES: tuple[tuple[str, int, int], ...] = (
    ("160m", 1_800_000,  2_000_000),
    ("80m",  3_500_000,  4_000_000),
    ("60m",  5_330_000,  5_410_000),
    ("40m",  7_000_000,  7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m",  50_000_000, 54_000_000),
)


def band_for_freq(hz: int | None) -> str | None:
    """Return the amateur band name (e.g. ``"20m"``) for a frequency in Hz."""
    if not hz:
        return None
    for name, lo, hi in _BAND_EDGES:
        if lo <= hz <= hi:
            return name
    return None


def format_mhz(freq_khz: float) -> str:
    """Format a kHz value as a MHz string, e.g. 7078.0 → '7.078 MHz'."""
    mhz = freq_khz / 1000.0
    s = f"{mhz:.4f} MHz"
    # Strip trailing zeros after decimal point, but keep at least one decimal place.
    parts = s.split(" ")
    num = parts[0].rstrip("0").rstrip(".")
    if "." not in num:
        num += ".0"
    return f"{num} MHz"
