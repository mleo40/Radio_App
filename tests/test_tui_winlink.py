"""Headless TUI tests for the Winlink mode surface.

Drive the Textual app via its test pilot (needs the ``tui`` extra; skips when
Textual is absent). No Pat and no network are required: the connection-trigger
paths are not exercised here, and the one send test patches ``router.send`` to
capture the composed message. We assert the Winlink action bar, the subject /
gateway plumbing, and subject injection into outbound messages.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from radio_app.transports.base import TRANSPORT_REGISTRY  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402

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


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG)
    TRANSPORT_REGISTRY.pop("mercury", None)
    return str(p)


def test_winlink_mode_shows_action_bar(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            assert app.query_one("#winlink-bar").display is True
            # Other transports' bars stay hidden in winlink mode.
            assert app.query_one("#mesh-bar").display is False
            assert app.query_one("#js8-bar").display is False

    asyncio.run(run())


def test_winlink_bar_hidden_in_other_modes(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            assert app.query_one("#winlink-bar").display is False

    asyncio.run(run())


def test_subject_command_sets_pending_and_shows_in_bar(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            await app._handle_command("/subject Net Report")
            await pilot.pause()
            assert app._winlink_subject == "Net Report"
            label = str(app.query_one("#winlink-bar-label").render())
            assert "Net Report" in label

    asyncio.run(run())


def test_subject_button_prefills_composer(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            app._winlink_subject_prompt()
            await pilot.pause()
            assert app.query_one("#composer").value == "/subject "

    asyncio.run(run())


def test_gateway_command_updates_transport_and_bar(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            await app._handle_command("/gateway KW1U")
            await pilot.pause()
            t = app._winlink_transport()
            assert t.config["gateway"] == "KW1U"
            assert "KW1U" in str(app.query_one("#winlink-bar-label").render())

    asyncio.run(run())


def test_subject_is_injected_into_outbound_and_cleared(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            app.current_target = "W1AW"
            app._set_winlink_subject("Status update")
            await pilot.pause()

            captured = {}

            async def fake_send(msg, force_transport=None):
                captured["msg"] = msg
                captured["transport"] = force_transport
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            app._send("body of the message")
            await pilot.pause()
            await pilot.pause()

            msg = captured.get("msg")
            assert msg is not None
            assert captured["transport"] == "winlink"
            assert msg.metadata.get("subject") == "Status update"
            assert msg.recipient == "W1AW"
            # Subject is consumed so it doesn't bleed into the next message.
            assert app._winlink_subject == ""

    asyncio.run(run())


def test_winlink_transport_helper_returns_instance(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            t = app._winlink_transport()
            assert t is not None and t.name == "winlink"
            assert t.build_connect_url() == "telnet://"

    asyncio.run(run())


def test_health_board_renders_winlink_path_rows(config_path):
    class _RecLog:
        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(str(text))

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._winlink_paths = [
                {"name": "telnet", "scheme": "telnet", "label": "Telnet",
                 "reachable": None, "detail": "via Pat"},
                {"name": "varahf", "scheme": "varahf", "label": "VARA HF modem",
                 "reachable": False, "detail": "127.0.0.1:8300"},
            ]
            rec = _RecLog()
            app._render_winlink_paths(rec)
            return "\n".join(rec.written)

    rendered = asyncio.run(run())
    assert "VARA HF modem" in rendered
    assert "down" in rendered           # varahf modem probed down
    assert "Telnet" in rendered and "n/a" in rendered  # telnet not probeable



