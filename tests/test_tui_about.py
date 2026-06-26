"""Headless TUI tests for the hidden "about" easter egg.

Verify both reveal paths open the AboutScreen modal: the undocumented Ctrl+G
chord and the ``xyzzy`` composer magic word. Needs the ``tui`` extra (skips
otherwise); no network required.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from radio_app.transports.base import TRANSPORT_REGISTRY  # noqa: E402
from radio_app.ui.about import ABOUT_MD  # noqa: E402
from radio_app.ui.tui import AboutScreen, RadioTUI  # noqa: E402

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
    TRANSPORT_REGISTRY.pop("mercury", None)
    return str(p)


def test_about_text_is_substantial():
    # The packaged about text is the runtime source of truth; make sure it's
    # present and mentions the app so a broken constant is caught early.
    assert "Radio_App" in ABOUT_MD
    assert len(ABOUT_MD) > 1000


def test_ctrl_g_opens_about_screen(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+g")
            await pilot.pause()
            return isinstance(app.screen, AboutScreen)

    assert asyncio.run(run()) is True


def test_about_binding_is_hidden_from_footer(config_path):
    # The easter egg must stay undocumented: the binding exists but is not shown.
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            about = [b for b in app.BINDINGS if getattr(b, "action", "") == "about"]
            return about

    bindings = asyncio.run(run())
    assert bindings, "about binding should exist"
    assert all(getattr(b, "show", True) is False for b in bindings)


def test_xyzzy_magic_word_opens_about(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            composer = app.query_one("#composer")
            composer.value = "xyzzy"
            await composer.action_submit()
            await pilot.pause()
            return isinstance(app.screen, AboutScreen)

    assert asyncio.run(run()) is True


def test_about_screen_closes_on_escape(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+g")
            await pilot.pause()
            opened = isinstance(app.screen, AboutScreen)
            await pilot.press("escape")
            await pilot.pause()
            closed = not isinstance(app.screen, AboutScreen)
            return opened and closed

    assert asyncio.run(run()) is True

