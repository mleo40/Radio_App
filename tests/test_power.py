"""Battery/power reading from Linux sysfs."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from radio_app.core.power import BatteryReading, read_battery


def _make_sysfs(
    tmp_path: Path,
    *,
    percent: int = 72,
    status: str = "Discharging",
    energy_now: int = 36_000_000,
    energy_full: int = 50_000_000,
    power_now: int = 12_000_000,
) -> Path:
    bat = tmp_path / "sys" / "class" / "power_supply" / "BAT0"
    bat.mkdir(parents=True)
    (bat / "type").write_text("Battery\n")
    (bat / "capacity").write_text(f"{percent}\n")
    (bat / "status").write_text(f"{status}\n")
    (bat / "energy_now").write_text(f"{energy_now}\n")
    (bat / "energy_full").write_text(f"{energy_full}\n")
    (bat / "power_now").write_text(f"{power_now}\n")
    return tmp_path / "sys" / "class" / "power_supply"


def test_read_battery_discharging(tmp_path):
    sysfs = _make_sysfs(
        tmp_path, percent=55, status="Discharging",
        energy_now=27_500_000, power_now=11_000_000,
    )
    with patch("radio_app.core.power._SYSFS_ROOT", sysfs):
        r = read_battery()
    assert r.present is True
    assert r.percent == pytest.approx(55.0)
    assert r.charging is False
    # 27.5 Wh / 11 W = 2.5 h = 150 min
    assert r.time_remaining_min == 150
    assert r.source == "sysfs"


def test_read_battery_charging(tmp_path):
    sysfs = _make_sysfs(tmp_path, percent=80, status="Charging", power_now=0)
    with patch("radio_app.core.power._SYSFS_ROOT", sysfs):
        r = read_battery()
    assert r.charging is True
    assert r.time_remaining_min is None


def test_read_battery_full(tmp_path):
    sysfs = _make_sysfs(tmp_path, percent=100, status="Full", power_now=0)
    with patch("radio_app.core.power._SYSFS_ROOT", sysfs):
        r = read_battery()
    assert r.charging is True


def test_read_battery_no_sysfs(tmp_path):
    absent = tmp_path / "nonexistent"
    with patch("radio_app.core.power._SYSFS_ROOT", absent):
        r = read_battery()
    assert r.present is False
    assert r.source == "none"
    assert r.percent is None


def test_read_battery_no_battery_dir(tmp_path):
    # Only an AC adapter, no battery entry with type=Battery
    ac = tmp_path / "sys" / "class" / "power_supply" / "AC0"
    ac.mkdir(parents=True)
    (ac / "type").write_text("Mains\n")
    sysfs = tmp_path / "sys" / "class" / "power_supply"
    with patch("radio_app.core.power._SYSFS_ROOT", sysfs):
        r = read_battery()
    assert r.present is False


def test_read_battery_fallback_charge_now(tmp_path):
    # No energy_now/power_now — use charge_now/current_now instead
    bat = tmp_path / "sys" / "class" / "power_supply" / "BAT0"
    bat.mkdir(parents=True)
    (bat / "type").write_text("Battery\n")
    (bat / "capacity").write_text("60\n")
    (bat / "status").write_text("Discharging\n")
    (bat / "charge_now").write_text("3_000_000\n".replace("_", ""))
    (bat / "current_now").write_text("1_500_000\n".replace("_", ""))
    sysfs = tmp_path / "sys" / "class" / "power_supply"
    with patch("radio_app.core.power._SYSFS_ROOT", sysfs):
        r = read_battery()
    assert r.present is True
    # 3 Ah / 1.5 A = 2 h = 120 min
    assert r.time_remaining_min == 120
