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

from radio_app.core.message import (  # noqa: E402,E501
    AddressType,
    DeliveryStatus,
    UnifiedMessage,
)
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


def test_health_board_renders_js8_radio_status(config_path):
    class _RecLog:
        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(str(text))

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            rec = _RecLog()
            app._render_js8_status(
                rec,
                {
                    "dial": 14_078_000,
                    "offset": 1500,
                    "freq": 14_079_500,
                    "band": "20m",
                    "speed": "normal",
                    "selected": "KE7XYZ",
                    "cat": True,
                },
            )
            with_cat = "\n".join(rec.written)
            rec2 = _RecLog()
            app._render_js8_status(rec2, {"cat": False})
            return with_cat, "\n".join(rec2.written)

    with_cat, without_cat = asyncio.run(run())
    assert "20m" in with_cat
    assert "offset 1500 Hz" in with_cat
    assert "speed normal" in with_cat
    assert "selected KE7XYZ" in with_cat
    assert "no CAT" in without_cat   # no-rig-control hint


def test_subject_md_prefixes_winlink_mail():
    msg = UnifiedMessage(
        sender="W1AW",
        content="See you at the net.",
        address_type=AddressType.DIRECT,
        recipient="N0CALL",
        status=DeliveryStatus.RECEIVED,
        transport="winlink",
        metadata={"subject": "Net tonight"},
    )
    out = RadioTUI._subject_md(msg)
    assert out == "[b]Net tonight[/b] \u2014 "


def test_subject_md_escapes_brackets_in_subject():
    msg = UnifiedMessage(
        sender="W1AW",
        content="body",
        address_type=AddressType.DIRECT,
        recipient="N0CALL",
        transport="winlink",
        metadata={"subject": "[URGENT] backslash \\ test"},
    )
    out = RadioTUI._subject_md(msg)
    # The opening bracket and backslash are escaped so they can't break markup.
    assert "\\[URGENT]" in out
    assert "\\\\" in out


def test_attachments_md_renders_paperclip():
    msg = UnifiedMessage(
        sender="W1AW",
        content="see files",
        address_type=AddressType.DIRECT,
        recipient="N0CALL",
        transport="winlink",
        metadata={"attachments": ["form.txt", "photo.jpg"]},
    )
    out = RadioTUI._attachments_md(msg)
    assert "\U0001f4ce" in out
    assert "form.txt" in out and "photo.jpg" in out


def test_attachments_md_empty_when_none():
    msg = UnifiedMessage(
        sender="W1AW", content="x", address_type=AddressType.DIRECT,
        recipient="N0CALL", transport="winlink",
    )
    assert RadioTUI._attachments_md(msg) == ""


def test_attach_queues_file_and_injects_into_send(config_path, tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hi")

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            app.current_target = "W1AW"
            await pilot.pause()
            await app._handle_command(f"/attach {f}")
            await pilot.pause()
            assert app._winlink_attach == [str(f)]

            captured = {}

            async def fake_send(msg, force_transport=None):
                captured["msg"] = msg
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            app._send("body")
            await pilot.pause()
            await pilot.pause()
            return captured.get("msg"), app._winlink_attach

    msg, remaining = asyncio.run(run())
    assert msg is not None
    assert msg.metadata.get("attach") == [str(f)]
    assert remaining == []  # queue consumed by the send


def test_attach_missing_file_is_not_queued(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            await app._handle_command("/attach /no/such/file.bin")
            await pilot.pause()
            return app._winlink_attach

    assert asyncio.run(run()) == []


def test_pick_gateway_sets_transport_gateway(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            app.action_pick_gateway("KW1U")
            await pilot.pause()
            return app._winlink_transport().config.get("gateway")

    assert asyncio.run(run()) == "KW1U"


def test_subject_md_empty_for_non_winlink_or_no_subject():
    base = dict(
        sender="W1AW",
        content="body",
        address_type=AddressType.DIRECT,
        recipient="N0CALL",
    )
    # Non-Winlink transport: no subject prefix even if metadata has one.
    other = UnifiedMessage(transport="js8call", metadata={"subject": "x"}, **base)
    assert RadioTUI._subject_md(other) == ""
    # Winlink but no/blank subject: nothing to prefix.
    blank = UnifiedMessage(transport="winlink", metadata={"subject": "  "}, **base)
    assert RadioTUI._subject_md(blank) == ""
    nmeta = UnifiedMessage(transport="winlink", **base)
    assert RadioTUI._subject_md(nmeta) == ""


def test_winlink_event_progress_formats_percent():
    ev = {"Progress": {"bytes_transferred": 50, "bytes_total": 200,
                       "subject": "Net Report", "sending": True}}
    assert RadioTUI._format_winlink_event(ev) == "Winlink tx: 25% Net Report"


def test_winlink_event_progress_done():
    ev = {"Progress": {"receiving": True, "subject": "Mail", "done": True}}
    assert RadioTUI._format_winlink_event(ev) == "Winlink rx: done Mail"


def test_winlink_event_status_connected_and_dialing():
    assert RadioTUI._format_winlink_event(
        {"Status": {"dialing": True}}
    ) == "Winlink: dialing\u2026"
    assert RadioTUI._format_winlink_event(
        {"Status": {"connected": True, "remote_addr": "KW1U"}}
    ) == "Winlink: connected KW1U"
    # An idle status update is suppressed.
    assert RadioTUI._format_winlink_event({"Status": {"connected": False}}) is None


def test_winlink_event_notification_and_unknown():
    assert RadioTUI._format_winlink_event(
        {"Notification": {"title": "Done", "body": "1 sent"}}
    ) == "Winlink: Done \u2014 1 sent"
    # Bookkeeping events produce no line.
    assert RadioTUI._format_winlink_event({"Ping": True}) is None
    assert RadioTUI._format_winlink_event({"UpdateMailbox": True}) is None




