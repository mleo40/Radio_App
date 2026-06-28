"""Headless TUI tests for the radio interlock (one HF radio, one transmitter).

Uses a config where BOTH JS8Call and Winlink contend for the radio (Winlink over
an RF modem path), then verifies the app refuses to key the radio on one mode
while the other holds it. Needs the ``tui`` extra; skips when Textual is absent.
No radio or network is touched.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from radio_app.ui.tui import RadioTUI  # noqa: E402

# Winlink over varahf (RF) => it shares the radio with JS8Call.
CONFIG_RF = """\
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
connect = "varahf"
"""


@pytest.fixture()
def config_rf(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_RF)
    return str(p)


def test_both_js8_and_rf_winlink_are_contenders(config_rf):
    async def run():
        app = RadioTUI(config_rf)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            il = app.core.radio_interlock
            return il.is_contender("js8call"), il.is_contender("winlink")

    js8, winlink = asyncio.run(run())
    assert js8 is True
    assert winlink is True


def test_entering_js8_mode_claims_the_radio(config_rf):
    async def run():
        app = RadioTUI(config_rf)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            return app.core.radio_interlock.holder

    assert asyncio.run(run()) == "js8call"


def test_js8_send_blocked_while_winlink_holds_radio(config_rf):
    async def run():
        app = RadioTUI(config_rf)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            app.current_target = "W1AW"
            # Simulate a Winlink RF session holding the radio.
            app.core.radio_interlock._holder = "winlink"

            called = {"n": 0}

            async def fake_send(msg, force_transport=None):
                called["n"] += 1
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            app._send("hello on the air")
            await pilot.pause()
            await pilot.pause()
            return called["n"]

    assert asyncio.run(run()) == 0  # the transmit was gated, not sent


def test_js8_send_allowed_when_radio_free(config_rf):
    async def run():
        app = RadioTUI(config_rf)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")  # claims the radio for js8call
            app.current_target = "W1AW"

            called = {"n": 0}

            async def fake_send(msg, force_transport=None):
                called["n"] += 1
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            app._send("hello on the air")
            await pilot.pause()
            await pilot.pause()
            return called["n"]

    assert asyncio.run(run()) == 1  # js8call holds the radio -> send proceeds

