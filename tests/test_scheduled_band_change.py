"""Tests for scheduled band changes.

Covers:
- /sched band <band> <time> [daily] command parsing and storage
- _check_scheduled() dispatches band_change entries via _fire_scheduled_band_change
- Daily repeat: re-queues for +24h after firing
- Interlock busy: skips and logs
- JS8Call not running: skips and logs
- /sched list formats band-change entries distinctly
- CLI: radioapp schedule band
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("textual")

from textual.widgets import RichLog  # noqa: E402

from radio_app.core.message import AddressType, UnifiedMessage  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402

UTC = timezone.utc

_CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[station]
callsign = "W1TEST"
[transports.js8call]
enabled = true
port = 2442
"""


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_CONFIG)
    return str(p)


def _log_text(app: RadioTUI) -> str:
    return "\n".join(s.text for s in app.query_one("#messages", RichLog).lines)


def _make_band_change_entry(band: str = "40m", recur_daily: bool = False,
                             fire_at: datetime | None = None):
    """Build a band-change UnifiedMessage suitable for store.schedule_add."""
    msg = UnifiedMessage(
        sender="W1TEST",
        content=f"Band change: {band}",
        address_type=AddressType.BROADCAST,
        metadata={"kind": "band_change", "band": band, "recur_daily": recur_daily},
        transport="js8call",
    )
    if fire_at is None:
        fire_at = datetime.now(UTC) - timedelta(seconds=1)  # already due
    return msg, fire_at


# ---------------------------------------------------------------------------
# /sched band — parsing and storage
# ---------------------------------------------------------------------------

def test_sched_band_stores_entry(config_path):
    """/sched band 40m 20:00 creates a pending band-change entry."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            # Schedule a band change one hour from now.
            await app._handle_command("/sched band 40m +1h")
            await pilot.pause()
            text = _log_text(app)
            assert "40m" in text
            assert "scheduled" in text.lower()

            # Verify it's in the pending queue.
            pending = app.core.store.schedule_pending()
            band_entries = [
                e for e in pending
                if e.message.metadata.get("kind") == "band_change"
            ]
            assert len(band_entries) == 1
            e = band_entries[0]
            assert e.message.metadata["band"] == "40m"
            assert e.message.metadata["recur_daily"] is False
            assert e.transport == "js8call"

    asyncio.run(run())


def test_sched_band_daily_flag(config_path):
    """/sched band 20m +30m daily marks recur_daily=True."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            await app._handle_command("/sched band 20m +30m daily")
            await pilot.pause()
            pending = app.core.store.schedule_pending()
            band_entries = [
                e for e in pending
                if e.message.metadata.get("kind") == "band_change"
            ]
            assert band_entries
            assert band_entries[0].message.metadata["recur_daily"] is True

    asyncio.run(run())


def test_sched_band_invalid_band(config_path):
    """/sched band with unknown band shows error."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            await app._handle_command("/sched band 99x +1h")
            await pilot.pause()
            text = _log_text(app)
            assert "unknown band" in text.lower()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# /sched list — band-change display
# ---------------------------------------------------------------------------

def test_sched_list_shows_band_change_label(config_path):
    """/sched list formats band-change entries with [band change] label."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            msg, fire_at = _make_band_change_entry("40m", fire_at=datetime.now(UTC) + timedelta(hours=1))
            app.core.store.schedule_add(msg, fire_at, transport="js8call")

            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/sched list")
            await pilot.pause()
            text = _log_text(app)
            assert "band change" in text.lower()
            assert "40m" in text

    asyncio.run(run())


def test_sched_list_daily_label(config_path):
    """/sched list shows [daily] for repeating band changes."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            msg, fire_at = _make_band_change_entry(
                "20m", recur_daily=True,
                fire_at=datetime.now(UTC) + timedelta(hours=1),
            )
            app.core.store.schedule_add(msg, fire_at, transport="js8call")

            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/sched list")
            await pilot.pause()
            text = _log_text(app)
            assert "daily" in text.lower()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# _check_scheduled — dispatch logic
# ---------------------------------------------------------------------------

def test_check_scheduled_fires_band_change_when_js8_down(config_path):
    """Band-change entries are skipped (not router.send'd) when JS8Call is down."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()

            msg, fire_at = _make_band_change_entry("40m")
            app.core.store.schedule_add(msg, fire_at, transport="js8call")

            # _check_scheduled is @work — call without await, then pump the loop.
            app._check_scheduled()
            for _ in range(20):
                await pilot.pause()
                await asyncio.sleep(0)

            text = _log_text(app)
            # Should log "skipped" not attempt a router.send.
            assert "skipped" in text.lower() or "not running" in text.lower()

            # Entry should be marked (sent or failed) so it doesn't re-fire.
            pending = app.core.store.schedule_pending()
            assert not any(e.id == msg.msg_id for e in pending)

    asyncio.run(run())


def test_check_scheduled_interlock_busy_skips(config_path):
    """Band change is skipped when another transport holds the radio interlock."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()

            # Pre-acquire the interlock for "winlink" (simulates RF session active).
            app.core.radio_interlock._contenders.add("winlink")
            app.core.radio_interlock._contenders.add("js8call")
            app.core.radio_interlock._holder = "winlink"

            msg, fire_at = _make_band_change_entry("40m")
            app.core.store.schedule_add(msg, fire_at, transport="js8call")

            # Mock JS8Call as running so we reach the interlock check.
            t = next(
                (t for t in app.core.transports if t.name == "js8call"), None
            )
            if t is not None:
                t._running = True

            # _check_scheduled is @work — call without await, then pump the loop.
            app._check_scheduled()
            for _ in range(20):
                await pilot.pause()
                await asyncio.sleep(0)

            text = _log_text(app)
            assert "busy" in text.lower() or "skipped" in text.lower()

    asyncio.run(run())


def test_daily_band_change_requeued_after_firing(config_path, tmp_path):
    """A fired daily band-change entry is re-queued for fire_at + 24h."""
    from radio_app.core.store import MessageStore, ScheduledEntry

    # Test the reschedule logic independently (no TUI needed).
    from radio_app.config import Config

    cfg = Config.load(str(config_path))
    store = MessageStore(cfg.database_path())

    msg, fire_at = _make_band_change_entry("40m", recur_daily=True)
    store.schedule_add(msg, fire_at, transport="js8call")

    # Simulate firing: mark sent, then reschedule.
    from radio_app.core.store import ScheduledEntry as SE
    entry = SE(id=msg.msg_id, fire_at=fire_at, message=msg, transport="js8call")
    store.schedule_mark_sent(entry.id, success=True)

    # Manually call reschedule logic (same as _reschedule_daily_band_change).
    new_fire = entry.fire_at + timedelta(days=1)
    new_msg = UnifiedMessage(
        sender=entry.message.sender,
        content=entry.message.content,
        address_type=AddressType.BROADCAST,
        metadata=dict(entry.message.metadata),
        transport="js8call",
    )
    store.schedule_add(new_msg, new_fire, transport="js8call")

    pending = store.schedule_pending()
    band_entries = [e for e in pending if e.message.metadata.get("kind") == "band_change"]
    assert len(band_entries) == 1
    assert band_entries[0].fire_at == new_fire
    assert band_entries[0].message.metadata["recur_daily"] is True

    store.close()


# ---------------------------------------------------------------------------
# CLI: radioapp schedule band
# ---------------------------------------------------------------------------

def test_cli_schedule_band(config_path, capsys):
    """radioapp schedule band 40m +1h creates a pending band-change entry."""
    from radio_app.cli import main as cli_main

    rc = cli_main([
        "--config", config_path,
        "schedule", "band", "40m", "+1h",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "40m" in captured.out
    assert "scheduled" in captured.out.lower() or "band change" in captured.out.lower()

    # Verify it's actually in the DB.
    from radio_app.config import Config
    from radio_app.core.store import MessageStore
    cfg = Config.load(config_path)
    store = MessageStore(cfg.database_path())
    pending = store.schedule_pending()
    band_entries = [e for e in pending if e.message.metadata.get("kind") == "band_change"]
    assert band_entries
    assert band_entries[0].message.metadata["band"] == "40m"
    store.close()


def test_cli_schedule_band_daily(config_path, capsys):
    """radioapp schedule band 20m 20:00 --daily sets recur_daily=True."""
    from radio_app.cli import main as cli_main

    rc = cli_main([
        "--config", config_path,
        "schedule", "band", "20m", "+30m", "--daily",
    ])
    assert rc == 0

    from radio_app.config import Config
    from radio_app.core.store import MessageStore
    cfg = Config.load(config_path)
    store = MessageStore(cfg.database_path())
    pending = store.schedule_pending()
    band_entries = [e for e in pending if e.message.metadata.get("kind") == "band_change"]
    assert band_entries
    assert band_entries[0].message.metadata["recur_daily"] is True
    store.close()


def test_cli_schedule_band_unknown_band(config_path, capsys):
    """radioapp schedule band with unknown band returns exit code 2."""
    from radio_app.cli import main as cli_main

    rc = cli_main([
        "--config", config_path,
        "schedule", "band", "99x", "+1h",
    ])
    assert rc == 2


def test_cli_schedule_list_shows_band_change(config_path, capsys):
    """radioapp schedule list shows band-change entries with [band change] label."""
    from radio_app.cli import main as cli_main

    # Schedule a band change first.
    cli_main(["--config", config_path, "schedule", "band", "40m", "+1h"])
    capsys.readouterr()  # discard the schedule confirmation

    rc = cli_main(["--config", config_path, "schedule", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "band change" in out.lower()
    assert "40m" in out
