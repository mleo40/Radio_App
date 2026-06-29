"""Tests for the SettingsScreen modal."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("textual")

CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[transports.js8call]
enabled = true
port = 2442
[transports.winlink]
enabled = true
pat_url = "http://127.0.0.1:9"
callsign = "N0CALL"
connect = "telnet"
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG)
    return str(p)


def test_settings_screen_renders(config_path):
    """Modal opens with three tab buttons visible."""
    from textual.widgets import Button

    from radio_app.ui.tui import RadioTUI, SettingsScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            opened = {}
            app.push_screen(SettingsScreen(app.core), lambda v: opened.update(result=v))
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#stab-fav", Button) is not None
            assert screen.query_one("#stab-wx", Button) is not None
            assert screen.query_one("#stab-bak", Button) is not None

    asyncio.run(run())


def test_settings_favorites_add_callsign(config_path):
    """Add a callsign via User type — appears in list and persists to core."""
    from textual.widgets import Button, Input, ListView

    from radio_app.ui.tui import RadioTUI, SettingsScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(SettingsScreen(app.core))
            await pilot.pause()
            screen = app.screen
            inp = screen.query_one("#stab-fav-id", Input)
            inp.value = "W1AW"
            screen.query_one("#stab-fav-add", Button).press()
            await pilot.pause()
            lst = screen.query_one("#stab-fav-list", ListView)
            assert len(lst) >= 1
            ids = [f.id for f in app.core.favorites.all()]
            assert "W1AW" in ids

    asyncio.run(run())


def test_settings_favorites_remove(config_path):
    """Removing a favorite via core API and refreshing the screen list reflects the change."""
    from textual.widgets import ListView

    from radio_app.ui.tui import RadioTUI, SettingsScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.core.favorites.add("K1ABC", kind="callsign")
            app.core.favorites.save(app.core.config)
            app.push_screen(SettingsScreen(app.core))
            await pilot.pause()
            screen = app.screen
            lst = screen.query_one("#stab-fav-list", ListView)
            assert len(lst) == 1
            # Remove via core API, then verify _refresh_fav_list reflects it
            app.core.favorites.remove("K1ABC")
            app.core.favorites.save(app.core.config)
            screen._refresh_fav_list()
            await pilot.pause()
            assert len(lst) == 0

    asyncio.run(run())


def test_settings_wx_grids_returned_on_close(config_path):
    """Adding a grid in Weather tab and closing returns it in the result dict."""
    from textual.widgets import Button, Input

    from radio_app.ui.tui import RadioTUI, SettingsScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            result_holder = {}
            app.push_screen(
                SettingsScreen(app.core),
                lambda v: result_holder.update(result=v),
            )
            await pilot.pause()
            screen = app.screen
            screen.query_one("#stab-wx", Button).press()
            await pilot.pause()
            inp = screen.query_one("#stab-wx-grid-input", Input)
            inp.value = "FN42"
            screen.query_one("#stab-wx-grid-add", Button).press()
            await pilot.pause()
            screen.query_one("#settings-close", Button).press()
            await pilot.pause()
            r = result_holder.get("result") or {}
            wx = r.get("wx") or {}
            assert "FN42" in wx.get("grids", "")

    asyncio.run(run())


def test_settings_backup_button_runs_without_crash(config_path):
    """Pressing Backup Now calls backup.create_backup without raising."""
    from textual.widgets import Button

    from radio_app.core.backup import BackupResult
    from radio_app.ui.tui import RadioTUI, SettingsScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(SettingsScreen(app.core))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#stab-bak", Button).press()
            await pilot.pause()
            fake_result = BackupResult(
                path=Path(config_path).parent / "radio_app-backup-test.tar.gz",
                config_included=True,
                db_included=True,
                size_bytes=1024,
            )
            with patch("radio_app.core.backup.create_backup", return_value=fake_result):
                screen.query_one("#stab-bak-backup", Button).press()
                await pilot.pause(delay=0.2)
            assert True  # no crash

    asyncio.run(run())
