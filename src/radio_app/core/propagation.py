"""Live HF propagation conditions from HamQSL solar XML feed.

Provides ``fetch_sync`` — a plain blocking HTTP fetch that returns a
``PropagationData`` snapshot or raises on any error.  The TUI drives this
via a ``@work(thread=True)`` thread worker; the CLI calls it directly.
"""

from __future__ import annotations

import logging
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"

# HamQSL reports conditions for band ranges; map individual amateur bands to them.
BAND_GROUP: dict[str, str] = {
    "80m": "80m-40m", "40m": "80m-40m",
    "30m": "30m-20m", "20m": "30m-20m",
    "17m": "17m-15m", "15m": "17m-15m",
    "12m": "12m-10m", "10m": "12m-10m",
}


@dataclass
class PropagationData:
    sfi: int                              # solar flux index
    ssn: int                              # sunspot number
    a_index: int                          # geomagnetic A-index
    k_index: int                          # geomagnetic K-index
    geo_field: str                        # geomagnetic field description (e.g. "QUIET")
    # band-range → {"day": "Good"|"Fair"|"Poor", "night": ...}
    conditions: dict[str, dict[str, str]] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def condition_for(self, band: str) -> dict[str, str]:
        """Return day/night condition dict for a single band name, or {}."""
        group = BAND_GROUP.get(band.lower())
        return self.conditions.get(group, {}) if group else {}


def _int(text: str | None) -> int:
    try:
        return int((text or "0").strip())
    except ValueError:
        return 0


def fetch_sync(url: str = HAMQSL_URL, timeout: float = 8.0) -> PropagationData:
    """Fetch and parse the HamQSL solar XML.  Raises on any error."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    item = root.find("solardata")
    if item is None:
        raise ValueError("No <solardata> element in HamQSL response")
    conds: dict[str, dict[str, str]] = {}
    for band_el in item.findall(".//calculatedconditions/band"):
        name = band_el.get("name", "")
        time = band_el.get("time", "")
        val  = (band_el.text or "").strip()
        if name and time and val:
            conds.setdefault(name, {})[time] = val
    return PropagationData(
        sfi=_int(item.findtext("solarflux")),
        ssn=_int(item.findtext("sunspots")),
        a_index=_int(item.findtext("aindex")),
        k_index=_int(item.findtext("kindex")),
        geo_field=(item.findtext("geomagfield") or "").strip(),
        conditions=conds,
    )
