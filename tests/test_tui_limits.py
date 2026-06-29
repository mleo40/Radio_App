"""Headless TUI tests for the per-mode composer size limit.

These verify that the composer enforces each transport's documented
``max_message_size`` (bytes): a hard block for protocols with a real cap
(MeshCore), an advisory-only warning for JS8Call (no published cap), and the
live ``n/limit`` status-bar counter. Needs the ``tui`` extra (skips otherwise);
no network is required.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from radio_app.ui.tui import RadioTUI  # noqa: E402

CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[transports.js8call]
enabled = true
port = 2442
"""


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG)
    return str(p)


def test_refresh_compose_limit_from_active_transport(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            return app._compose_limit

    assert asyncio.run(run()) == 80  # JS8Call's documented frame cap


def test_check_compose_limit_hard_blocks_meshcore(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Simulate being in MeshCore mode with its 134-byte cap.
            app.active_transport = "meshcore"
            app._compose_limit = 134
            ok_short = app._check_compose_limit("x" * 134)
            ok_long = app._check_compose_limit("x" * 135)
            return ok_short, ok_long

    ok_short, ok_long = asyncio.run(run())
    assert ok_short is True
    assert ok_long is False  # one byte over -> blocked


def test_check_compose_limit_warns_but_allows_js8(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.active_transport = "js8call"
            app._compose_limit = 80
            # Over the advisory cap: allowed (auto-framed), not blocked.
            return app._check_compose_limit("y" * 200)

    assert asyncio.run(run()) is True


def test_check_compose_limit_counts_utf8_bytes(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.active_transport = "meshcore"
            app._compose_limit = 4
            # "café" = 5 UTF-8 bytes (é is 2) even though it's 4 characters.
            return app._check_compose_limit("café")

    assert asyncio.run(run()) is False


def test_compose_counter_markup_states(config_path):
    from textual.widgets import Input

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view = "active"
            app.active_transport = "meshcore"
            app._compose_limit = 134
            composer = app.query_one("#composer", Input)
            composer.value = "hello"          # under limit
            under = app._compose_counter_markup()
            composer.value = "z" * 135         # over a hard limit
            over = app._compose_counter_markup()
            composer.value = "/attach /a/very/long/path"  # command, exempt
            cmd = app._compose_counter_markup()
            return under, over, cmd

    under, over, cmd = asyncio.run(run())
    assert "5/134" in under and "dim" in under
    assert "135/134" in over and "red" in over
    assert cmd == ""


def test_compose_counter_soft_limit_is_yellow(config_path):
    from textual.widgets import Input

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view = "active"
            app.active_transport = "js8call"
            app._compose_limit = 80
            app.query_one("#composer", Input).value = "q" * 100
            return app._compose_counter_markup()

    assert "yellow" in asyncio.run(run())

