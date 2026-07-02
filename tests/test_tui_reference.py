"""Headless TUI tests for the F9 field-reference screen.

Verify the screen opens with F9, closes on Escape/q, doesn't stack on a
second press, and its tabs actually switch content. Needs the ``tui`` extra
(skips otherwise); no network required.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import ContentSwitcher  # noqa: E402

from radio_app.ui.reference import (  # noqa: E402
    JS8CALL_MD,
    OTHER_MODES_MD,
    QMX_QDX_MD,
    TRUSDX_MD,
    WSJTX_MD,
)
from radio_app.ui.tui import RadioTUI, ReferenceScreen  # noqa: E402

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


@pytest.mark.parametrize(
    "content", [TRUSDX_MD, QMX_QDX_MD, JS8CALL_MD, WSJTX_MD, OTHER_MODES_MD]
)
def test_reference_content_is_substantial(content):
    # Packaged content is the runtime source of truth; catch an accidentally
    # emptied constant early.
    assert len(content) > 200


def test_f1_opens_reference_screen(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f9")
            await pilot.pause()
            return isinstance(app.screen, ReferenceScreen)

    assert asyncio.run(run()) is True


def test_f1_binding_is_visible_in_footer():
    # Unlike the About easter egg, this is a real feature -- it should show.
    ref = [b for b in RadioTUI.BINDINGS if getattr(b, "action", "") == "reference"]
    assert ref, "reference binding should exist"
    assert all(getattr(b, "show", False) is True for b in ref)


def test_reference_screen_does_not_stack_on_second_press(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f9")
            await pilot.pause()
            first = app.screen
            await pilot.press("f9")
            await pilot.pause()
            return first is app.screen

    assert asyncio.run(run()) is True


def test_reference_screen_closes_on_escape(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f9")
            await pilot.pause()
            opened = isinstance(app.screen, ReferenceScreen)
            await pilot.press("escape")
            await pilot.pause()
            closed = not isinstance(app.screen, ReferenceScreen)
            return opened and closed

    assert asyncio.run(run()) is True


def test_reference_screen_closes_on_q(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f9")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            return not isinstance(app.screen, ReferenceScreen)

    assert asyncio.run(run()) is True


def test_reference_tabs_switch_content(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f9")
            await pilot.pause()
            screen = app.screen
            switcher = screen.query_one("#ref-content", ContentSwitcher)
            initial = switcher.current

            await pilot.click("#ref-qmx")
            # Under heavy parallel test load, pilot.pause() alone doesn't
            # reliably give the button-pressed handler enough real wall-clock
            # time to settle -- mix in a genuine sleep so the event loop
            # actually yields to other scheduled work, not just Textual's own
            # message queue, before asserting.
            active_ids: list[str | None] = []
            for _ in range(50):
                await pilot.pause()
                await asyncio.sleep(0.01)
                active_ids = [
                    b.id for b in screen.query(".rtab") if "-active" in b.classes
                ]
                if active_ids == ["ref-qmx"]:
                    break
            after_click = switcher.current
            return initial, after_click, active_ids

    initial, after_click, active_ids = asyncio.run(run())
    assert initial == "ref-trusdx-pane"
    assert after_click == "ref-qmx-pane"
    assert active_ids == ["ref-qmx"]
