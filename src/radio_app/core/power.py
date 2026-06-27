"""Host battery / power-supply awareness via Linux sysfs.

Reads power-supply state directly from ``/sys/class/power_supply/`` without
requiring psutil. Fails gracefully (``present=False``) on macOS, Windows,
or any system without a battery — always safe to call.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_SYSFS_ROOT = Path("/sys/class/power_supply")


@dataclass
class BatteryReading:
    present: bool          # False when no battery found
    percent: float | None  # 0-100
    charging: bool         # True = on AC / charging / full
    time_remaining_min: int | None  # estimated minutes left (discharging only)
    source: str            # "sysfs" | "none"


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _find_battery() -> Path | None:
    """Return the first sysfs battery directory, or None."""
    if not _SYSFS_ROOT.exists():
        return None
    for p in sorted(_SYSFS_ROOT.iterdir()):
        t = _read(p / "type")
        if t and t.lower() == "battery":
            return p
    return None


def read_battery() -> BatteryReading:
    """Return the current host battery state.

    Always returns a valid ``BatteryReading``; ``present=False`` when no
    battery is detectable.
    """
    bat = _find_battery()
    if bat is None:
        return BatteryReading(present=False, percent=None, charging=False,
                              time_remaining_min=None, source="none")

    cap_raw = _read(bat / "capacity")
    percent = float(cap_raw) if cap_raw is not None else None

    status = (_read(bat / "status") or "").lower()
    charging = status in ("charging", "full", "not charging")

    # Estimate time remaining from energy_now / power_now (µWh / µW → hours).
    time_min: int | None = None
    if not charging:
        en = _read(bat / "energy_now")
        pw = _read(bat / "power_now")
        if en and pw and float(pw) > 0:
            time_min = int(float(en) / float(pw) * 60)
        else:
            # Fallback: charge_now / current_now (µAh / µA → hours).
            cn = _read(bat / "charge_now")
            ci = _read(bat / "current_now")
            if cn and ci and float(ci) > 0:
                time_min = int(float(cn) / float(ci) * 60)

    return BatteryReading(
        present=True,
        percent=percent,
        charging=charging,
        time_remaining_min=time_min,
        source="sysfs",
    )
