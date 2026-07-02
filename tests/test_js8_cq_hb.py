"""Tests for the JS8Call CQ and Heartbeat one-click action-bar buttons.

Covers:
- JS8CallTransport.send_cq() wire format
- #js8-cq / #js8-hb buttons present in the JS8 action bar
- Clicking them transmits and logs a confirmation
- Not-running guard
"""
from __future__ import annotations

import asyncio
import json

import pytest

from radio_app.transports.js8call_transport import JS8CallTransport


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass


def _running() -> tuple[JS8CallTransport, _FakeWriter]:
    t = JS8CallTransport({})
    t._running = True
    w = _FakeWriter()
    t._writer = w  # type: ignore[assignment]
    return t, w


def _last(w: _FakeWriter) -> dict:
    return json.loads(w.sent[-1].decode())


# -- JS8CallTransport.send_cq() -----------------------------------------------

def test_send_cq_with_callsign():
    t, w = _running()
    assert asyncio.run(t.send_cq("W1AW")) is True
    payload = _last(w)
    assert payload["type"] == "TX.SEND_MESSAGE"
    assert payload["value"] == "@ALLCALL CQ CQ CQ DE W1AW"


def test_send_cq_without_callsign():
    t, w = _running()
    assert asyncio.run(t.send_cq("")) is True
    assert _last(w)["value"] == "@ALLCALL CQ CQ CQ"


def test_send_cq_uppercases_callsign():
    t, w = _running()
    asyncio.run(t.send_cq("w1aw"))
    assert _last(w)["value"] == "@ALLCALL CQ CQ CQ DE W1AW"


def test_send_cq_false_when_not_running():
    t = JS8CallTransport({})
    assert asyncio.run(t.send_cq("W1AW")) is False


# -- TUI: CQ / HB buttons ------------------------------------------------------

pytest.importorskip("textual")
from textual.widgets import Button, RichLog  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402

_TUI_CONFIG = """\
[general]
display_name = "T"
[logging]
file = ""
[station]
callsign = "W1TEST"
grid_square = "FN31"
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


def test_js8_cq_and_hb_buttons_exist(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.query_one("#js8-cq", Button) is not None
            assert app.query_one("#js8-hb", Button) is not None

    asyncio.run(run())


def test_js8_cq_button_not_running(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            app._js8_send_cq()
            for _ in range(10):
                await pilot.pause()
                await asyncio.sleep(0)
            assert "not running" in _log_text(app).lower()

    asyncio.run(run())


def test_js8_hb_button_sends_heartbeat(tui_config, monkeypatch):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            t = next(x for x in app.core.transports if x.name == "js8call")
            monkeypatch.setattr(type(t), "running", property(lambda self: True))
            send_hb = AsyncMock(return_value=True)
            monkeypatch.setattr(t, "send_heartbeat", send_hb)

            app._js8_send_hb()
            for _ in range(10):
                await pilot.pause()
                await asyncio.sleep(0)

            send_hb.assert_awaited_with("FN31")
            assert "heartbeat sent" in _log_text(app).lower()

    asyncio.run(run())


def test_js8_cq_button_sends_cq(tui_config, monkeypatch):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            t = next(x for x in app.core.transports if x.name == "js8call")
            monkeypatch.setattr(type(t), "running", property(lambda self: True))
            send_cq = AsyncMock(return_value=True)
            monkeypatch.setattr(t, "send_cq", send_cq)

            app._js8_send_cq()
            for _ in range(10):
                await pilot.pause()
                await asyncio.sleep(0)

            send_cq.assert_awaited_with("W1TEST")
            assert "cq sent" in _log_text(app).lower()

    asyncio.run(run())
