"""Tests for the Winlink step of the ``radioapp setup`` wizard.

No network and no Pat are required: the prompt helpers (``_ask`` / ``_ask_bool``)
and the Pat reachability probe (``_probe_pat``) are monkeypatched so the wizard
runs non-interactively. We assert the wizard writes a usable ``[transports.winlink]``
block with sensible auto-fallback defaults and defaults the Winlink callsign to
the operator's callsign.
"""

from __future__ import annotations

import argparse

from radio_app import cli
from radio_app.config import Config


def _run_setup(tmp_path, monkeypatch, *, enable_winlink: bool, pat_up: bool):
    """Drive ``_cmd_setup`` with scripted answers; return the saved Config."""

    def fake_ask(prompt: str, default: str = "") -> str:
        if prompt.startswith("Callsign"):
            return "N0CALL"
        if prompt.startswith("Grid square"):
            return ""  # optional
        if prompt.startswith("Pat HTTP API URL"):
            return "http://127.0.0.1:8080"
        # Everything else (incl. "Winlink callsign") = press Enter, keep default.
        return default

    def fake_ask_bool(prompt: str, default: bool) -> bool:
        if prompt.startswith("Enable Winlink"):
            return enable_winlink
        if prompt.startswith("Enable"):
            return False  # keep Reticulum/JS8Call/MeshCore off
        return default

    monkeypatch.setattr(cli, "_ask", fake_ask)
    monkeypatch.setattr(cli, "_ask_bool", fake_ask_bool)
    monkeypatch.setattr(cli, "_probe_pat", lambda url: pat_up)

    path = tmp_path / "config.toml"
    rc = cli._cmd_setup(argparse.Namespace(config=str(path)))
    assert rc == 0
    return Config.load(str(path))


def test_setup_enables_winlink_with_defaults(tmp_path, monkeypatch):
    cfg = _run_setup(tmp_path, monkeypatch, enable_winlink=True, pat_up=False)
    wl = cfg.transports.get("winlink", {})
    assert wl.get("enabled") is True
    assert wl.get("pat_url") == "http://127.0.0.1:8080"
    # Winlink callsign defaulted to the operator callsign.
    assert wl.get("callsign") == "N0CALL"
    # Out-of-the-box auto-fallback defaults are written.
    assert wl.get("connect") == "auto"
    assert wl.get("connect_order") == ["telnet", "varahf", "ardop"]
    assert wl.get("poll_interval") == 60
    assert wl.get("auto_connect") is False


def test_setup_winlink_disabled_writes_disabled_block(tmp_path, monkeypatch):
    cfg = _run_setup(tmp_path, monkeypatch, enable_winlink=False, pat_up=False)
    wl = cfg.transports.get("winlink", {})
    assert wl.get("enabled") is False
    # No defaults injected when the operator declines.
    assert "connect_order" not in wl


def test_setup_winlink_probe_up_does_not_change_outcome(tmp_path, monkeypatch):
    cfg = _run_setup(tmp_path, monkeypatch, enable_winlink=True, pat_up=True)
    assert cfg.transports["winlink"]["enabled"] is True

