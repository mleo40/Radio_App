"""Tests for the /map TUI command and `radioapp map` CLI command.

Core math/rendering (bearing_distance, render_compass_map) is covered in
tests/test_maidenhead.py; this covers the command wiring: position
resolution, favorite lookup, and error messages.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import RichLog  # noqa: E402

from radio_app.ui.tui import RadioTUI  # noqa: E402

_TUI_CONFIG = """\
[general]
display_name = "T"
[logging]
file = ""
[station]
callsign = "W1TEST"
[position]
lat = 41.7
lon = -72.7
[transports.js8call]
enabled = true
port = 2442
"""


@pytest.fixture()
def tui_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_TUI_CONFIG)
    return str(p)


def _log_text(app: RadioTUI) -> str:
    return "\n".join(s.text for s in app.query_one("#messages", RichLog).lines)


def test_map_no_position_configured(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[general]\n[logging]\nfile = ""\n[station]\ncallsign = "W1T"\n'
    )

    async def run():
        app = RadioTUI(str(cfg))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._handle_command("/map")
            await pilot.pause()
            assert "no position set" in _log_text(app).lower()

    asyncio.run(run())


def test_map_no_favorites_with_grid(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._handle_command("/map")
            await pilot.pause()
            assert "no favorites have a grid square" in _log_text(app).lower()

    asyncio.run(run())


def test_map_plots_favorites_with_grid(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.core.favorites.add("W1AW", meta={"gridsquare": "FN31"})
            app.core.favorites.add("KE0XYZ", meta={"gridsquare": "FN42"})

            await app._handle_command("/map")
            await pilot.pause()
            text = _log_text(app)
            assert "◉" in text
            assert "W1AW" in text
            assert "KE0XYZ" in text
            assert "FN31" in text
            assert "FN42" in text

    asyncio.run(run())


def test_map_single_grid_target(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._handle_command("/map EM73")
            await pilot.pause()
            text = _log_text(app).lower()
            assert "em73" in text
            assert "km" in text

    asyncio.run(run())


def test_map_single_favorite_target_by_id(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.core.favorites.add("W1AW", meta={"gridsquare": "FN31"})

            await app._handle_command("/map W1AW")
            await pilot.pause()
            text = _log_text(app).lower()
            assert "w1aw" in text
            assert "fn31" in text
            assert "km" in text

    asyncio.run(run())


def test_map_unknown_favorite_target(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._handle_command("/map NOSUCHCALL")
            await pilot.pause()
            text = _log_text(app).lower()
            assert "no known grid square" in text

    asyncio.run(run())


# ---------------------------------------------------------------------------
# CLI: radioapp map
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path, extra=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[general]\n[logging]\nfile = ""\n[station]\ncallsign = "W1T"\n'
        "[position]\nlat = 41.7\nlon = -72.7\n" + extra
    )
    return cfg


def test_cli_map_no_position(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\n[logging]\nfile = ""\n[station]\ncallsign = "W1T"\n')
    rc = cli_main(["--config", str(cfg), "map"])
    assert rc == 1
    assert "no position configured" in capsys.readouterr().err.lower()


def test_cli_map_lists_favorites(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(tmp_path)
    rc = cli_main(["--config", str(cfg), "favorites", "add", "W1AW", "--grid", "FN31"])
    assert rc == 0
    capsys.readouterr()

    rc = cli_main(["--config", str(cfg), "map"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "◉" in out
    assert "W1AW" in out
    assert "FN31" in out


def test_cli_map_direct_grid_target(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(tmp_path)
    rc = cli_main(["--config", str(cfg), "map", "EM73"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "em73" in out
    assert "km" in out
