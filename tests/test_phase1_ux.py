"""Phase 1 UX gap tests: surfaces for existing backend features.

Covers all 7 items from the phase1-ux-gaps branch:
  1. MeshCore no-channel hint in _send()
  2. /sched list and /sched cancel
  3. /tmpl add and /tmpl del
  4. /subs list, add, rm
  5. /position set, show, clear
  6. JS8 Beacon button present in bar
  7. /bands activity log
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, RichLog  # noqa: E402

from radio_app.core.message import AddressType, UnifiedMessage  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402

UTC = timezone.utc

_BASE_CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[station]
callsign = "W1TEST"
[transports.js8call]
enabled = true
port = 2442
[transports.meshcore]
enabled = true
connection = "tcp"
tcp_port = 5000
[templates]
welfare = "Welfare check — all OK"
[groups.EMS]
transports = ["js8call"]
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


# ---------------------------------------------------------------------------
# 1. MeshCore no-channel hint
# ---------------------------------------------------------------------------

def test_meshcore_no_channel_hint(config_path):
    """_send() with no current_target in MeshCore mode shows a channel hint."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            app.current_target = ""
            await app._handle_command("/to")  # This clears without selecting
            # Call _send with empty target directly.
            app.current_target = ""
            app._send("hello")
            await pilot.pause()
            text = _log_text(app)
            assert "channel" in text.lower(), f"Expected channel hint, got: {text!r}"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. /sched list and /sched cancel
# ---------------------------------------------------------------------------

def test_sched_list_empty(config_path):
    """/sched list with no pending messages shows a 'none pending' message."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/sched list")
            await pilot.pause()
            text = _log_text(app)
            assert "pending" in text.lower()

    asyncio.run(run())


def test_sched_list_and_cancel(config_path):
    """/sched list shows pending messages; /sched cancel removes one."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Schedule a message via the store directly.
            from radio_app.core.message import UnifiedMessage
            from datetime import timedelta
            msg = UnifiedMessage.direct("W1TEST", "W2TARGET", "test scheduled")
            fire_at = datetime.now(UTC) + timedelta(hours=1)
            app.core.store.schedule_add(msg, fire_at)

            # List should show it.
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/sched list")
            await pilot.pause()
            text = _log_text(app)
            short_id = msg.msg_id[:8]
            assert short_id in text, f"Expected msg id in list, got: {text!r}"

            # Cancel it by partial id.
            app.query_one("#messages", RichLog).clear()
            await app._handle_command(f"/sched cancel {short_id}")
            await pilot.pause()
            text = _log_text(app)
            assert "cancelled" in text.lower() or "cancel" in text.lower()

            # Confirm it's gone from pending.
            pending = app.core.store.schedule_pending()
            assert all(e.id != msg.msg_id for e in pending)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. /tmpl add and /tmpl del
# ---------------------------------------------------------------------------

def test_tmpl_add_and_del(config_path):
    """/tmpl add creates a template; /tmpl del removes it."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            # Add a new template.
            await app._handle_command('/tmpl add ack "Message received, thank you"')
            await pilot.pause()
            text = _log_text(app)
            assert "saved" in text.lower()
            assert "ack" in app.core.config.data.get("templates", {})

            # List shows both old and new.
            app.query_one("#messages", RichLog).clear()
            await app._handle_command("/tmpl list")
            await pilot.pause()
            text = _log_text(app)
            assert "welfare" in text
            assert "ack" in text

            # Delete the new template.
            app.query_one("#messages", RichLog).clear()
            await app._handle_command("/tmpl del ack")
            await pilot.pause()
            text = _log_text(app)
            assert "deleted" in text.lower()
            assert "ack" not in app.core.config.data.get("templates", {})

    asyncio.run(run())


def test_tmpl_del_nonexistent(config_path):
    """/tmpl del with unknown name shows error with available templates."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/tmpl del nonexistent")
            await pilot.pause()
            text = _log_text(app)
            assert "not found" in text.lower()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. /subs list, add, rm
# ---------------------------------------------------------------------------

def test_subs_list_shows_groups(config_path):
    """/subs shows configured groups with subscription markers."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/subs")
            await pilot.pause()
            text = _log_text(app)
            # EMS is configured and subscribed.
            assert "EMS" in text

    asyncio.run(run())


def test_subs_add_and_remove(config_path):
    """/subs add creates a subscription; /subs rm removes it."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            # Add a subscription.
            await app._handle_command("/subs add @NET")
            await pilot.pause()
            text = _log_text(app)
            assert "NET" in text
            subs = app.core.config.subscriptions.get("groups", [])
            assert "NET" in subs

            # Remove it.
            app.query_one("#messages", RichLog).clear()
            await app._handle_command("/subs rm @NET")
            await pilot.pause()
            text = _log_text(app)
            assert "unsubscribed" in text.lower() or "NET" in text
            subs = app.core.config.subscriptions.get("groups", [])
            assert "NET" not in subs

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. /position set, show, clear
# ---------------------------------------------------------------------------

def test_position_set_and_show(config_path):
    """/position <grid> sets lat/lon and /position shows it."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            # Set via grid square.
            await app._handle_command("/position FN31")
            await pilot.pause()
            text = _log_text(app)
            assert "FN31" in text
            pos_cfg = app.core.config.data.get("position", {})
            assert "lat" in pos_cfg and "lon" in pos_cfg

            # Show current.
            app.query_one("#messages", RichLog).clear()
            await app._handle_command("/position")
            await pilot.pause()
            text = _log_text(app)
            assert "FN31" in text or "lat" in text.lower()

    asyncio.run(run())


def test_position_invalid_grid(config_path):
    """/position with bad input shows an error."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/position ZZ99")
            await pilot.pause()
            text = _log_text(app)
            assert "not a valid" in text.lower() or "invalid" in text.lower()

    asyncio.run(run())


def test_position_clear(config_path):
    """/position clear removes stored lat/lon."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Set a position first.
            app.core.config.set("position", "lat", 41.5)
            app.core.config.set("position", "lon", -72.5)

            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/position clear")
            await pilot.pause()
            text = _log_text(app)
            assert "cleared" in text.lower()
            pos_cfg = app.core.config.data.get("position", {})
            assert "lat" not in pos_cfg and "lon" not in pos_cfg

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. JS8 Beacon button is present in the JS8 bar
# ---------------------------------------------------------------------------

def test_js8_beacon_button_exists(config_path):
    """The #js8-beacon button is present in the JS8 action bar."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            btn = app.query_one("#js8-beacon", Button)
            assert btn is not None

    asyncio.run(run())


def test_js8_beacon_no_position_logs_error(config_path):
    """Clicking Beacon without a configured position logs a helpful message."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            # Ensure no position is set.
            app._position = None
            pos_data = app.core.config.data.get("position", {})
            for k in ("lat", "lon", "fixed_grid"):
                pos_data.pop(k, None)

            app._js8_send_beacon()
            for _ in range(10):
                await pilot.pause()
                await asyncio.sleep(0)
            text = _log_text(app)
            # Either "not running" (JS8Call down) or "no position" message.
            assert any(
                phrase in text.lower()
                for phrase in ("not running", "no position", "position")
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 7. /bands activity log
# ---------------------------------------------------------------------------

def test_bands_activity_empty(config_path):
    """/bands activity with no HF messages reports no activity."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/bands activity")
            await pilot.pause()
            text = _log_text(app)
            assert "no hf" in text.lower() or "activity" in text.lower()

    asyncio.run(run())


def test_bands_activity_shows_band_messages(config_path):
    """/bands activity shows messages that have band metadata."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Save a message with band metadata.
            msg = UnifiedMessage(
                sender="W2TEST",
                content="Net check-in",
                transport="js8call",
                address_type=AddressType.BROADCAST,
                metadata={"band": "40m", "snr": -10},
            )
            app.core.store.save(msg)

            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/bands activity")
            await pilot.pause()
            text = _log_text(app)
            assert "40m" in text
            assert "W2TEST" in text

    asyncio.run(run())


def test_bands_activity_band_filter(config_path):
    """/bands activity 40m filters to just that band."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for band, call in (("40m", "W2TEST"), ("20m", "K1TEST")):
                msg = UnifiedMessage(
                    sender=call,
                    content=f"on {band}",
                    transport="js8call",
                    address_type=AddressType.BROADCAST,
                    metadata={"band": band},
                )
                app.core.store.save(msg)

            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/bands activity 40m")
            await pilot.pause()
            text = _log_text(app)
            assert "W2TEST" in text
            assert "K1TEST" not in text

    asyncio.run(run())
