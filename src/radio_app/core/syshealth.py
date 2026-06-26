"""Host system health metrics for the Health board / ``db stats``.

Everything here is best-effort and degrades gracefully: each metric returns
``None`` when it can't be determined on the current platform. Free disk space,
load average and (on Linux) memory + temperature come from the standard library
with no extra dependency; if the optional :mod:`psutil` package is installed we
use it for richer, cross-platform CPU/memory/temperature readings.
"""

from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass

try:
    import psutil  # type: ignore

    _HAVE_PSUTIL = True
except ModuleNotFoundError:
    psutil = None  # type: ignore
    _HAVE_PSUTIL = False


@dataclass
class SystemHealth:
    cpu_percent: float | None = None
    load_avg: tuple[float, float, float] | None = None
    cpu_count: int | None = None
    mem_total: int | None = None
    mem_used: int | None = None
    mem_percent: float | None = None
    temp_c: float | None = None
    disk_total: int | None = None
    disk_free: int | None = None
    disk_used_percent: float | None = None
    #: Battery charge 0-100, or None when there is no battery (e.g. a desktop).
    battery_percent: float | None = None
    #: True on external/AC power, False on battery, None if undetermined.
    power_plugged: bool | None = None
    #: Estimated seconds of battery runtime left (None if unknown/unlimited).
    battery_secs_left: int | None = None
    #: True when a battery is present at all (a desktop reports False).
    has_battery: bool = False


def format_bytes(n: int | None) -> str:
    """Human-friendly size (``1.2 GB``). Returns ``?`` for ``None``."""
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _cpu() -> tuple[float | None, int | None]:
    count = os.cpu_count()
    if _HAVE_PSUTIL:
        try:
            return float(psutil.cpu_percent(interval=None)), count
        except Exception:  # noqa: BLE001
            return None, count
    return None, count


def _load_avg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()  # Unix only
    except (OSError, AttributeError):
        return None


def _memory() -> tuple[int | None, int | None, float | None]:
    """Return (total, used, percent_used)."""
    if _HAVE_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            return int(vm.total), int(vm.used), float(vm.percent)
        except Exception:  # noqa: BLE001
            pass
    # Linux fallback via /proc/meminfo.
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0]) * 1024  # kB -> B
        total = info.get("MemTotal")
        avail = info.get("MemAvailable")
        if total and avail is not None:
            used = total - avail
            pct = (used / total * 100) if total else None
            return total, used, pct
    except (OSError, ValueError, IndexError):
        pass
    return None, None, None


def _temperature() -> float | None:
    """Best-effort CPU/SoC temperature in degrees Celsius."""
    if _HAVE_PSUTIL:
        try:
            temps = psutil.sensors_temperatures() or {}
            # Prefer a CPU-ish sensor, else the first reading we find.
            preferred = ("coretemp", "k10temp", "cpu_thermal", "cpu-thermal", "soc")
            for key in preferred:
                if key in temps and temps[key]:
                    return float(temps[key][0].current)
            for entries in temps.values():
                if entries:
                    return float(entries[0].current)
        except Exception:  # noqa: BLE001
            pass
    # Linux sysfs thermal zones (millidegrees C).
    best: float | None = None
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            with open(os.path.join(zone, "temp")) as fh:
                milli = int(fh.read().strip())
            celsius = milli / 1000.0
            # Pick the hottest plausible zone (ignore obviously bogus values).
            if 0 < celsius < 150 and (best is None or celsius > best):
                best = celsius
        except (OSError, ValueError):
            continue
    return best


def _disk(path: str | None) -> tuple[int | None, int | None, float | None]:
    """Return (total, free, used_percent) for the filesystem holding ``path``."""
    target = path or os.getcwd()
    # Walk up to an existing directory (the DB file may not exist yet).
    probe = target
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe or "/")
        used_pct = (usage.used / usage.total * 100) if usage.total else None
        return usage.total, usage.free, used_pct
    except OSError:
        return None, None, None


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _battery() -> tuple[float | None, bool | None, int | None, bool]:
    """Return (percent, power_plugged, secs_left, has_battery), all best-effort."""
    if _HAVE_PSUTIL:
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                secs = bat.secsleft
                # psutil sentinels: UNLIMITED (-1, on AC) / UNKNOWN (-2).
                if secs is None or secs < 0:
                    secs_left = None
                else:
                    secs_left = int(secs)
                return (
                    float(bat.percent),
                    bool(bat.power_plugged),
                    secs_left,
                    True,
                )
        except Exception:  # noqa: BLE001
            pass
        else:
            # psutil present and definitively reported no battery.
            if not _HAVE_LINUX_PSUPPLY:
                return None, None, None, False
    # Linux sysfs fallback: /sys/class/power_supply/{BAT*,AC*}.
    percent: float | None = None
    has_bat = False
    for bat_dir in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        cap = _read_int(os.path.join(bat_dir, "capacity"))
        if cap is not None:
            has_bat = True
            percent = float(cap)
            break
    plugged: bool | None = None
    for ac_dir in sorted(glob.glob("/sys/class/power_supply/A*")):
        online = _read_int(os.path.join(ac_dir, "online"))
        if online is not None:
            plugged = bool(online)
            break
    if not has_bat:
        return None, plugged, None, False
    return percent, plugged, None, True


_HAVE_LINUX_PSUPPLY = bool(glob.glob("/sys/class/power_supply/BAT*"))


def format_duration(secs: int | None) -> str:
    """Human-friendly runtime like ``3h12m``. Returns ``?`` for ``None``."""
    if secs is None or secs < 0:
        return "?"
    hours, rem = divmod(int(secs), 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def collect(disk_path: str | None = None) -> SystemHealth:
    """Gather a one-shot snapshot of host system health."""
    cpu_percent, cpu_count = _cpu()
    mem_total, mem_used, mem_pct = _memory()
    disk_total, disk_free, disk_pct = _disk(disk_path)
    bat_pct, plugged, secs_left, has_bat = _battery()
    return SystemHealth(
        cpu_percent=cpu_percent,
        load_avg=_load_avg(),
        cpu_count=cpu_count,
        mem_total=mem_total,
        mem_used=mem_used,
        mem_percent=mem_pct,
        temp_c=_temperature(),
        disk_total=disk_total,
        disk_free=disk_free,
        disk_used_percent=disk_pct,
        battery_percent=bat_pct,
        power_plugged=plugged,
        battery_secs_left=secs_left,
        has_battery=has_bat,
    )

