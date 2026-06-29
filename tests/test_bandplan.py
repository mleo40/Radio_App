"""Band-plan lookup and formatting."""
from __future__ import annotations

import pytest

from radio_app.core.bandplan import FrequencyEntry, band_for_freq, format_mhz, lookup


def test_lookup_all_returns_entries():
    assert len(lookup()) > 10


def test_lookup_band_40m():
    results = lookup(band="40m")
    assert all(e.band == "40m" for e in results)
    assert len(results) >= 2


def test_lookup_band_case_insensitive():
    assert lookup(band="40M") == lookup(band="40m")


def test_lookup_mode_js8():
    results = lookup(mode="js8")
    assert all(e.mode == "JS8" for e in results)
    assert len(results) >= 8


def test_lookup_mode_case_insensitive():
    assert lookup(mode="JS8") == lookup(mode="js8")


def test_lookup_region_us_includes_intl():
    results = lookup(region="US")
    regions = {e.region for e in results}
    assert "US" in regions
    assert "INTL" in regions


def test_lookup_transport_js8call():
    results = lookup(transport="js8call")
    assert all(e.transport == "js8call" for e in results)


def test_lookup_combined_filters():
    results = lookup(band="40m", mode="js8")
    assert all(e.band == "40m" and e.mode == "JS8" for e in results)


def test_lookup_no_match():
    assert lookup(band="40m", mode="FM") == []


def test_format_mhz_40m():
    assert format_mhz(7_078.0) == "7.078 MHz"


def test_format_mhz_20m():
    assert format_mhz(14_078.0) == "14.078 MHz"


def test_format_mhz_2m():
    # 146520 kHz = 146.52 MHz
    result = format_mhz(146_520.0)
    assert "146.52" in result
    assert "MHz" in result


def test_entries_sorted_by_band_order():
    results = lookup()
    bands = [e.band for e in results]
    if "160m" in bands and "20m" in bands:
        assert bands.index("160m") < bands.index("20m")
    if "40m" in bands and "20m" in bands:
        assert bands.index("40m") < bands.index("20m")


def test_cli_bands(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[storage]\ndatabase = ":memory:"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    from radio_app.cli import main
    assert main(["bands", "--band", "40m"]) == 0
    out = capsys.readouterr().out
    assert "40m" in out
    assert "7.078" in out


def test_cli_bands_no_match(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[storage]\ndatabase = ":memory:"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    from radio_app.cli import main
    assert main(["bands", "--band", "40m", "--mode", "FM"]) == 0
    assert "No entries" in capsys.readouterr().out


def test_cli_bands_all(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[storage]\ndatabase = ":memory:"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    from radio_app.cli import main
    assert main(["bands"]) == 0
    out = capsys.readouterr().out
    assert "JS8" in out
    assert "WINLINK" in out


def test_cli_bands_transport_filter(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[storage]\ndatabase = ":memory:"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    from radio_app.cli import main
    assert main(["bands", "--transport", "winlink"]) == 0
    out = capsys.readouterr().out
    assert "WINLINK" in out


# -- band_for_freq ------------------------------------------------------------


def test_band_for_freq_20m():
    assert band_for_freq(14_078_000) == "20m"


def test_band_for_freq_40m():
    assert band_for_freq(7_078_000) == "40m"


def test_band_for_freq_at_lower_edge():
    assert band_for_freq(14_000_000) == "20m"


def test_band_for_freq_at_upper_edge():
    assert band_for_freq(14_350_000) == "20m"


def test_band_for_freq_between_bands_returns_none():
    assert band_for_freq(15_000_000) is None


def test_band_for_freq_none_returns_none():
    assert band_for_freq(None) is None


def test_band_for_freq_zero_returns_none():
    assert band_for_freq(0) is None


def test_band_for_freq_6m():
    assert band_for_freq(50_318_000) == "6m"
