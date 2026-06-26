"""Tests for the ``radioapp winlink`` CLI command (no Pat required)."""

from __future__ import annotations

from radio_app.cli import main


def _write_cfg(tmp_path, enabled=True):
    p = tmp_path / "config.toml"
    block = "enabled = true" if enabled else "enabled = false"
    p.write_text(
        "[transports.winlink]\n"
        f"{block}\n"
        'pat_url = "http://127.0.0.1:9"\n'
        'callsign = "N0CALL"\n'
        'connect = "auto"\n'
        "[logging]\n"
        'file = ""\n'
    )
    return str(p)


def test_winlink_status_reports_unreachable(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    rc = main(["--config", cfg, "winlink", "status"])
    out = capsys.readouterr().out
    assert rc == 1  # Pat not answering on port 9
    assert "pat reachable    : no" in out
    assert "N0CALL" in out
    assert "auto: telnet" in out
    # Credentials guidance is always shown.
    assert "never stores it" in out


def test_winlink_status_when_disabled(tmp_path, capsys):
    cfg = _write_cfg(tmp_path, enabled=False)
    rc = main(["--config", cfg, "winlink", "status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "not enabled" in out


def test_winlink_defaults_to_status(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    rc = main(["--config", cfg, "winlink"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "pat url" in out

