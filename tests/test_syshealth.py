"""Tests for host system-health metrics (best-effort, stdlib-backed)."""

from __future__ import annotations

from radio_app.core import syshealth


def test_format_bytes_units():
    assert syshealth.format_bytes(None) == "?"
    assert syshealth.format_bytes(0) == "0 B"
    assert syshealth.format_bytes(512) == "512 B"
    assert syshealth.format_bytes(1536) == "1.5 KB"
    assert syshealth.format_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert syshealth.format_bytes(3 * 1024**3) == "3.0 GB"


def test_collect_returns_disk_info(tmp_path):
    # Disk usage comes from the standard library, so it is always available.
    h = syshealth.collect(str(tmp_path / "conversations.db"))
    assert h.disk_total is not None and h.disk_total > 0
    assert h.disk_free is not None and h.disk_free >= 0
    assert h.disk_used_percent is not None
    assert 0 <= h.disk_used_percent <= 100


def test_collect_handles_nonexistent_path(tmp_path):
    # The DB file may not exist yet — disk should resolve via a parent dir.
    h = syshealth.collect(str(tmp_path / "deep" / "missing" / "db.sqlite"))
    assert h.disk_total is not None


def test_collect_cpu_count_present():
    h = syshealth.collect()
    # os.cpu_count() is essentially always available.
    assert h.cpu_count is None or h.cpu_count >= 1


def test_format_duration():
    assert syshealth.format_duration(None) == "?"
    assert syshealth.format_duration(-1) == "?"
    assert syshealth.format_duration(0) == "0m"
    assert syshealth.format_duration(90) == "1m"
    assert syshealth.format_duration(3 * 3600 + 12 * 60) == "3h12m"


def test_collect_reports_battery_fields():
    # Battery is hardware-dependent, so we only assert the fields exist and are
    # internally consistent (no battery -> percent is None).
    h = syshealth.collect()
    assert isinstance(h.has_battery, bool)
    if not h.has_battery:
        assert h.battery_percent is None
    else:
        assert h.battery_percent is None or 0 <= h.battery_percent <= 100


def test_battery_no_battery_when_psutil_returns_none(monkeypatch):
    # When psutil reports no battery and there's no sysfs battery, has_battery
    # must be False and percent None.
    if not syshealth._HAVE_PSUTIL:
        return
    monkeypatch.setattr(syshealth, "_HAVE_LINUX_PSUPPLY", False)
    monkeypatch.setattr(syshealth.psutil, "sensors_battery", lambda: None)
    pct, plugged, secs, has_bat = syshealth._battery()
    assert has_bat is False and pct is None and secs is None


