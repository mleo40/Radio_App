"""Headless TUI tests for the Winlink mode surface.

Drive the Textual app via its test pilot (needs the ``tui`` extra; skips when
Textual is absent). No Pat and no network are required: the connection-trigger
paths are not exercised here, and the one send test patches ``router.send`` to
capture the composed message. We assert the Winlink action bar, the subject /
gateway plumbing, and subject injection into outbound messages.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("textual")

from radio_app.core.message import (  # noqa: E402,E501
    AddressType,
    DeliveryStatus,
    UnifiedMessage,
)
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
            # Telnet with no gateway uses Pat's 'telnet' alias (CMS, with target).
            assert t.build_connect_url() == "telnet"

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
            assert app._attach_queue == [str(f)]

            captured = {}

            async def fake_send(msg, force_transport=None):
                captured["msg"] = msg
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            app._send("body")
            await pilot.pause()
            await pilot.pause()
            return captured.get("msg"), app._attach_queue

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
            return app._attach_queue

    assert asyncio.run(run()) == []


def test_attach_rejected_on_mode_without_attachment_support(config_path, tmp_path):
    """`/attach` is gated by the capability flag, not hardcoded to Winlink."""
    f = tmp_path / "doc.txt"
    f.write_text("hi")

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # JS8Call has no attachment support -> the file isn't queued.
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command(f"/attach {f}")
            await pilot.pause()
            return app._attach_queue

    assert asyncio.run(run()) == []


def test_attach_works_and_injects_in_reticulum_mode(tmp_path):
    """Reticulum direct sends carry queued attachments; groups don't.

    A fake active-transport object provides the attachment/group capabilities so
    the test exercises the TUI's capability-driven gating without starting a real
    RNS instance.
    """
    f = tmp_path / "pic.bin"
    f.write_bytes(b"\x00\x01\x02")

    class _Caps:
        supports_attachments = True
        supports_groups = True
        max_message_size = 1_000_000

    class _FakeRet:
        name = "reticulum"

        def capabilities(self):
            return _Caps()

    async def run():
        # js8call-only config keeps startup hermetic (no real RNS); we stub the
        # active transport to advertise attachment support.
        jcfg = tmp_path / "j.toml"
        jcfg.write_text(CONFIG)
        app = RadioTUI(str(jcfg))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.active_transport = "reticulum"
            app._active_transport_obj = lambda: _FakeRet()  # type: ignore

            await app._handle_command(f"/attach {f}")
            await pilot.pause()
            assert app._attach_queue == [str(f)]

            captured = {}

            async def fake_send(msg, force_transport=None):
                captured["msg"] = msg
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            # Direct target: attachment injected + queue consumed.
            app.current_target = "abcdef0123456789"
            app._send("body")
            await pilot.pause()
            await pilot.pause()
            direct_msg = captured.get("msg")
            # Group target keeps the queue (attachments are direct-only).
            await app._handle_command(f"/attach {f}")
            await pilot.pause()
            app.current_target = "@net"
            app._send("hi all")
            await pilot.pause()
            await pilot.pause()
            return direct_msg, app._attach_queue

    msg, remaining = asyncio.run(run())
    assert msg is not None
    assert msg.metadata.get("attach") == [str(f)]
    assert remaining == [str(f)]  # group send didn't consume the queue


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
    assert RadioTUI._format_winlink_event(ev) == (
        "Winlink \u2191 send 25% of 200B: Net Report"
    )


def test_winlink_event_progress_done():
    ev = {"Progress": {"receiving": True, "subject": "Mail", "done": True}}
    assert RadioTUI._format_winlink_event(ev) == "Winlink \u2193 recv done: Mail"


def test_winlink_event_progress_no_subject():
    ev = {"Progress": {"sending": True, "done": True}}
    assert RadioTUI._format_winlink_event(ev) == (
        "Winlink \u2191 send done: (no subject)"
    )


def test_winlink_event_status_connected_and_dialing():
    assert RadioTUI._format_winlink_event(
        {"Status": {"dialing": True}}
    ) == "Winlink: dialing\u2026"
    assert RadioTUI._format_winlink_event(
        {"Status": {"connected": True, "remote_addr": "KW1U"}}
    ) == "Winlink: connected to KW1U"
    # An idle status update is suppressed.
    assert RadioTUI._format_winlink_event({"Status": {"connected": False}}) is None


def test_winlink_event_notification_and_unknown():
    assert RadioTUI._format_winlink_event(
        {"Notification": {"title": "Done", "body": "1 sent"}}
    ) == "Winlink \U0001f4e8 Done \u2014 1 sent"
    # Bookkeeping events produce no line.
    assert RadioTUI._format_winlink_event({"Ping": True}) is None
    assert RadioTUI._format_winlink_event({"UpdateMailbox": True}) is None


def test_winlink_event_logline_is_surfaced_dim():
    # Pat's live log transcript is surfaced (dim) for verbose session feedback.
    out = RadioTUI._format_winlink_event(
        {"LogLine": "Connecting to telnet://cms.winlink.org ..."}
    )
    assert out is not None
    assert "pat" in out
    assert "Connecting to telnet" in out
    # Blank log lines are suppressed.
    assert RadioTUI._format_winlink_event({"LogLine": "   "}) is None


def test_winlink_live_gate_hides_replayed_log_until_session_starts():
    # Pat replays old LogLines to a new WS client before the session begins;
    # they must be hidden until a live Status/Progress event marks the session.
    state = {"live": False}
    # Historical backlog arrives first -> suppressed.
    assert RadioTUI._winlink_event_is_live(
        {"LogLine": "2026/06/26 10:00:00 old session line"}, state
    ) is False
    # Pat's initial idle status doesn't make it live.
    assert RadioTUI._winlink_event_is_live(
        {"Status": {"connected": False, "dialing": False}}, state
    ) is True  # non-LogLine events always pass
    assert state["live"] is False
    # The live session starts (dialing) -> flips live True.
    assert RadioTUI._winlink_event_is_live(
        {"Status": {"dialing": True}}, state
    ) is True
    assert state["live"] is True
    # Now live LogLines are shown.
    assert RadioTUI._winlink_event_is_live(
        {"LogLine": "Connected to CMS"}, state
    ) is True


def test_winlink_live_gate_progress_also_triggers_live():
    state = {"live": False}
    assert RadioTUI._winlink_event_is_live({"LogLine": "old"}, state) is False
    # A Progress push (live-only event) also marks the session live.
    RadioTUI._winlink_event_is_live({"Progress": {"sending": True}}, state)
    assert state["live"] is True
    assert RadioTUI._winlink_event_is_live({"LogLine": "tx ..."}, state) is True


# -- forms composer (picker + fill screens) ----------------------------------

_FORMS = [
    {"name": "ICS213", "folder": "ICS", "path": "ICS/ICS213.txt"},
    {"name": "Radiogram", "folder": "Welfare", "path": "Welfare/Rgram.txt"},
    {"name": "Check-in", "folder": "Welfare", "path": "Welfare/Checkin.txt"},
]


def test_forms_filter_is_case_insensitive_substring():
    from radio_app.ui.tui import WinlinkFormsScreen

    f = WinlinkFormsScreen.filter_forms
    assert len(f(_FORMS, "")) == 3                 # empty -> all
    assert [x["name"] for x in f(_FORMS, "rgram")] == ["Radiogram"]   # by path
    assert {x["name"] for x in f(_FORMS, "welfare")} == {
        "Radiogram", "Check-in",
    }                                              # by folder
    assert f(_FORMS, "nope") == []


def test_winlink_forms_button_present_in_bar(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            from textual.widgets import Button

            assert app.query_one("#winlink-forms", Button) is not None

    asyncio.run(run())


def test_forms_picker_selection_dismisses_with_path(config_path):
    from radio_app.ui.tui import WinlinkFormsScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            picked = {}
            app.push_screen(
                WinlinkFormsScreen(_FORMS), lambda v: picked.update(v=v)
            )
            await pilot.pause()
            screen = app.screen
            # Filter narrows the list; selecting index 0 returns its path.
            from textual.widgets import Input

            screen.query_one("#wlf-filter", Input).value = "checkin"
            await pilot.pause()
            assert len(screen._visible) == 1
            screen.on_list_view_selected(
                type("E", (), {"list_view": type("L", (), {"index": 0})()})()
            )
            await pilot.pause()
            assert picked["v"] == "Welfare/Checkin.txt"

    asyncio.run(run())


def test_compose_form_screen_builds_result(config_path):
    from radio_app.ui.tui import WinlinkComposeFormScreen

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(WinlinkComposeFormScreen("ICS/ICS213.txt", ["city"]))
            await pilot.pause()
            screen = app.screen
            from textual.widgets import Input

            screen.query_one("#wcf-f-city", Input).value = "Boston"
            screen.query_one("#wcf-to", Input).value = "W1AW"
            # Subject/Cc left blank -> None (form's computed values win).
            result = screen._build_result()
            assert result == {
                "template": "ICS/ICS213.txt",
                "responses": {"city": "Boston"},
                "to": "W1AW",
                "cc": None,
                "subject": None,
            }

    asyncio.run(run())


def test_winlink_open_forms_reports_when_none_installed(config_path):
    """Pressing Forms with Pat unreachable surfaces a helpful system message."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            logged: list[str] = []
            app._log_system = lambda m: logged.append(m)  # type: ignore
            app._winlink_open_forms()
            # Let the worker run (list_forms fails fast against the dead port).
            for _ in range(50):
                await pilot.pause()
                if any("form" in m.lower() for m in logged):
                    break
            assert any(
                "No Winlink forms" in m or "catalog" in m.lower() for m in logged
            )

    asyncio.run(run())


def test_winlink_open_forms_auto_fetches_when_catalog_empty(config_path, monkeypatch):
    """An empty catalog triggers update_forms() automatically, not just a hint."""

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            t = app._winlink_transport()

            calls: list[str] = []

            async def fake_list_forms():
                calls.append("list")
                # Empty on the first call (nothing installed yet), populated
                # after update_forms() has "downloaded" the catalog.
                if any(c == "update" for c in calls):
                    return [
                        {"name": "ICS213", "path": "ICS/ICS213.txt", "folder": "ICS"}
                    ]
                return []

            async def fake_update_forms():
                calls.append("update")
                return {"version": "1.0", "action": "update"}

            monkeypatch.setattr(t, "list_forms", fake_list_forms)
            monkeypatch.setattr(t, "update_forms", fake_update_forms)

            logged: list[str] = []
            app._log_system = lambda m: logged.append(m)  # type: ignore
            app._winlink_open_forms()
            for _ in range(50):
                await pilot.pause()
                if "update" in calls and calls.count("list") >= 2:
                    break

            assert calls == ["list", "update", "list"]
            assert any("fetching" in m.lower() for m in logged)

    asyncio.run(run())


def test_winlink_connect_failure_shows_toast(config_path, monkeypatch):
    """A failed session (dead Pat) toasts, not just logs to #messages.

    A multi-message session can run for a while; if the operator has switched
    to another mode (clears #messages) or a utility view (F5) while waiting,
    the log line alone never reaches them.
    """
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()

            notifications: list[tuple[str, dict]] = []
            monkeypatch.setattr(
                app, "notify", lambda msg, **kw: notifications.append((msg, kw))
            )

            app._winlink_connect()
            for _ in range(50):
                await pilot.pause()
                if notifications:
                    break

            assert len(notifications) == 1
            msg, kw = notifications[0]
            assert "failed" in msg.lower()
            assert kw.get("severity") == "error"

    asyncio.run(run())


def test_winlink_connect_success_shows_toast(config_path, monkeypatch):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            t = app._winlink_transport()
            monkeypatch.setattr(t, "connect_now", AsyncMock(return_value=2))

            notifications: list[tuple[str, dict]] = []
            monkeypatch.setattr(
                app, "notify", lambda msg, **kw: notifications.append((msg, kw))
            )

            app._winlink_connect()
            for _ in range(50):
                await pilot.pause()
                if notifications:
                    break

            assert len(notifications) == 1
            msg, kw = notifications[0]
            assert "session complete" in msg.lower()
            assert "received 2" in msg.lower()
            assert kw.get("severity") is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# WinlinkEmailComposeScreen + template tests
# ---------------------------------------------------------------------------

def test_wl_template_substitution():
    """_substitute_wl_template replaces all three placeholders."""
    from radio_app.ui.tui import _substitute_wl_template

    result = _substitute_wl_template(
        "From {callsign} on {date_utc} at {time_utc}",
        callsign="W1AW",
        date_utc="28 Jun 2026",
        time_utc="1430",
    )
    assert result == "From W1AW on 28 Jun 2026 at 1430"


def test_wl_templates_list_not_empty():
    """_WL_TEMPLATES contains at least blank + standard ham radio forms."""
    from radio_app.ui.tui import _WL_TEMPLATES

    names = [t.name for t in _WL_TEMPLATES]
    assert "(blank)" in names
    assert any("Radiogram" in n for n in names)
    assert any("Welfare" in n for n in names)
    assert any("EmComm" in n for n in names)


def test_winlink_compose_button_exists(config_path):
    """The Winlink action bar has a '✉ Compose' button."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()
            # Button must be present and enabled even when transport is DOWN.
            btn = app.query_one("#winlink-compose")
            assert btn is not None

    asyncio.run(run())


def test_winlink_compose_cancel_returns_none(config_path):
    """Cancelling the compose modal does not queue any message."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()

            sent: list = []

            async def fake_send(msg, force_transport=None):
                sent.append(msg)
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            app._winlink_email_compose()
            # Wait for the modal to appear.
            from radio_app.ui.tui import WinlinkEmailComposeScreen
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, WinlinkEmailComposeScreen):
                    break
            assert isinstance(app.screen, WinlinkEmailComposeScreen)
            # Cancel — nothing should be sent.
            app.screen.dismiss(None)
            await pilot.pause()
            assert sent == []

    asyncio.run(run())


def test_winlink_compose_prefills_subject(config_path):
    """Compose modal pre-fills Subject from _winlink_subject."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            app._winlink_subject = "Pre-set subject"
            await pilot.pause()

            app._winlink_email_compose()
            from radio_app.ui.tui import WinlinkEmailComposeScreen
            from textual.widgets import Input
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, WinlinkEmailComposeScreen):
                    break
            assert isinstance(app.screen, WinlinkEmailComposeScreen)
            val = app.screen.query_one("#wecf-subject", Input).value
            assert val == "Pre-set subject"
            app.screen.dismiss(None)

    asyncio.run(run())


def test_winlink_compose_builds_message_with_correct_metadata(config_path):
    """Submitting the compose modal queues an email with subject/cc/body."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._select_mode("winlink")
            await pilot.pause()

            captured: dict = {}

            async def fake_send(msg, force_transport=None):
                captured["msg"] = msg
                captured["transport"] = force_transport
                return True

            app.core.router.send = fake_send  # type: ignore[assignment]
            app._winlink_email_compose()

            from radio_app.ui.tui import WinlinkEmailComposeScreen
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, WinlinkEmailComposeScreen):
                    break
            assert isinstance(app.screen, WinlinkEmailComposeScreen)

            # Simulate user filling in fields and submitting.
            app.screen.dismiss({
                "to": "W1AW",
                "cc": "K1ABC",
                "subject": "Test Subject",
                "body": "Line one\nLine two\nLine three",
                "attachments": [],
            })
            # Wait for the worker to process the result.
            for _ in range(30):
                await pilot.pause()
                if captured.get("msg"):
                    break

            msg = captured.get("msg")
            assert msg is not None
            assert msg.recipient == "W1AW"
            assert msg.content == "Line one\nLine two\nLine three"
            assert msg.metadata.get("subject") == "Test Subject"
            assert msg.metadata.get("cc") == "K1ABC"
            assert captured["transport"] == "winlink"

    asyncio.run(run())





