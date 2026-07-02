"""Tests for:
  1. MeshCore firehose composer placeholder hint
  2. /groups TUI command (list, show, new, delete, add, rm, tag, untag)
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import Input, RichLog  # noqa: E402

from radio_app.ui.tui import RadioTUI  # noqa: E402

_BASE_CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[station]
callsign = "W1TEST"
[transports.meshcore]
enabled = true
connection = "tcp"
tcp_port = 5000
[transports.js8call]
enabled = true
port = 2442
[groups.EMS]
transports = ["js8call"]
members = ["js8call:KE0XYZ"]
tags = ["emcomm"]
[subscriptions]
groups = ["EMS"]
"""


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_BASE_CONFIG)
    return str(p)


def _log_text(app: RadioTUI) -> str:
    return "\n".join(s.text for s in app.query_one("#messages", RichLog).lines)


def _composer(app: RadioTUI) -> Input:
    return app.query_one("#composer", Input)


# ---------------------------------------------------------------------------
# 1. MeshCore firehose composer hint
# ---------------------------------------------------------------------------

def test_meshcore_hint_on_mode_switch(config_path):
    """Switching to MeshCore with no channel shows a guidance placeholder."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            ph = _composer(app).placeholder.lower()
            assert "channel" in ph or "click" in ph, \
                f"Expected channel guidance in placeholder, got: {ph!r}"

    asyncio.run(run())


def test_js8_mode_has_normal_placeholder(config_path):
    """Non-MeshCore modes use the normal 'Type a message' placeholder."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            ph = _composer(app).placeholder.lower()
            assert "type" in ph or "message" in ph or "help" in ph, \
                f"Expected normal placeholder, got: {ph!r}"

    asyncio.run(run())


def test_meshcore_hint_clears_after_channel_selected(config_path):
    """After selecting a MeshCore channel the placeholder returns to normal."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            # Simulate selecting channel 0.
            app.current_target = "@0"
            app._update_composer_placeholder()
            await pilot.pause()
            ph = _composer(app).placeholder.lower()
            assert "type" in ph or "message" in ph or "help" in ph, \
                f"Expected normal placeholder after channel selected, got: {ph!r}"

    asyncio.run(run())


def test_meshcore_hint_returns_on_deselect(config_path):
    """Pressing Escape (deselect_chat) in MeshCore restores the channel hint."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            # Select a channel then deselect.
            app.current_target = "@0"
            app._update_composer_placeholder()
            await pilot.pause()
            app.action_deselect_chat()
            await pilot.pause()
            ph = _composer(app).placeholder.lower()
            assert "channel" in ph or "click" in ph, \
                f"Expected channel hint after deselect, got: {ph!r}"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. /groups command
# ---------------------------------------------------------------------------

def test_groups_list_shows_configured_groups(config_path):
    """/groups lists all groups with subscription markers."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups")
            await pilot.pause()
            text = _log_text(app)
            assert "EMS" in text

    asyncio.run(run())


def test_groups_show_details(config_path):
    """/groups @EMS shows member and tag details."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups @EMS")
            await pilot.pause()
            text = _log_text(app)
            assert "EMS" in text
            assert "KE0XYZ" in text
            assert "emcomm" in text.lower()

    asyncio.run(run())


def test_groups_show_nonexistent(config_path):
    """/groups @NONE gives a 'no such group' message."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups @NONE")
            await pilot.pause()
            text = _log_text(app)
            assert "no such group" in text.lower()

    asyncio.run(run())


def test_groups_new_creates_group(config_path):
    """/groups new @NET creates a new group persisted to config."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups new @NET js8call")
            await pilot.pause()
            text = _log_text(app)
            assert "NET" in text
            assert "created" in text.lower()
            # Verify persisted.
            assert "NET" in app.core.config.data.get("groups", {})

    asyncio.run(run())


def test_groups_delete_removes_group(config_path):
    """/groups delete @EMS removes the group."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups delete @EMS")
            await pilot.pause()
            text = _log_text(app)
            assert "deleted" in text.lower()
            assert "EMS" not in app.core.config.data.get("groups", {})

    asyncio.run(run())


def test_groups_add_member(config_path):
    """/groups add @EMS js8call:W2NEW adds a member."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups add @EMS js8call:W2NEW")
            await pilot.pause()
            text = _log_text(app)
            assert "W2NEW" in text
            members = app.core.config.data.get("groups", {}).get("EMS", {}).get("members", [])
            assert any("W2NEW" in str(m) for m in members)

    asyncio.run(run())


def test_groups_rm_member(config_path):
    """/groups rm @EMS js8call:KE0XYZ removes the member."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups rm @EMS js8call:KE0XYZ")
            await pilot.pause()
            text = _log_text(app)
            assert "removed" in text.lower()
            members = app.core.config.data.get("groups", {}).get("EMS", {}).get("members", [])
            assert not any("KE0XYZ" in str(m) for m in members)

    asyncio.run(run())


def test_groups_tag_and_untag(config_path):
    """/groups tag adds a tag; /groups untag removes it."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            await app._handle_command("/groups tag @EMS @relief")
            await pilot.pause()
            text = _log_text(app)
            assert "relief" in text.lower()
            tags = app.core.config.data.get("groups", {}).get("EMS", {}).get("tags", [])
            assert "relief" in tags

            app.query_one("#messages", RichLog).clear()
            await app._handle_command("/groups untag @EMS @relief")
            await pilot.pause()
            text = _log_text(app)
            assert "removed" in text.lower()
            tags = app.core.config.data.get("groups", {}).get("EMS", {}).get("tags", [])
            assert "relief" not in tags

    asyncio.run(run())


def test_groups_empty_list(config_path, tmp_path):
    """/groups with no groups configured gives a helpful message."""
    empty_cfg = tmp_path / "empty.toml"
    empty_cfg.write_text(
        "[general]\ndisplay_name = \"T\"\n[logging]\nfile = \"\"\n"
        "[station]\ncallsign = \"W1T\"\n[transports.js8call]\nenabled = true\nport = 2442\n"
    )

    async def run():
        app = RadioTUI(str(empty_cfg))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/groups")
            await pilot.pause()
            text = _log_text(app)
            assert "no groups" in text.lower() or "create" in text.lower()

    asyncio.run(run())
