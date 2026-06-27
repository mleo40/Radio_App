"""Scheduled message store operations and CLI."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from radio_app.cli import main
from radio_app.core.message import UnifiedMessage
from radio_app.core.store import MessageStore, ScheduledEntry


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "sched.db"
    s = MessageStore(db)
    yield s
    s.close()


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db = tmp_path / "sched.db"
    c = tmp_path / "config.toml"
    c.write_text(
        f'[storage]\ndatabase = "{db}"\n'
        '[station]\ncallsign = "W1TEST"\n'
    )
    monkeypatch.setenv("RADIO_APP_CONFIG", str(c))
    return c


def _msg(content="hello"):
    return UnifiedMessage(sender="W1TEST", content=content)


# -- store operations ---------------------------------------------------------


def test_schedule_add_and_list(store):
    msg = _msg()
    fire = datetime.now(UTC) + timedelta(hours=1)
    returned_id = store.schedule_add(msg, fire)
    assert returned_id == msg.msg_id
    pending = store.schedule_pending()
    assert len(pending) == 1
    assert isinstance(pending[0], ScheduledEntry)
    assert pending[0].message.content == "hello"
    assert pending[0].id == msg.msg_id


def test_schedule_pending_up_to(store):
    now = datetime.now(UTC)
    past = _msg("past")
    future = _msg("future")
    store.schedule_add(past, now - timedelta(minutes=5))
    store.schedule_add(future, now + timedelta(hours=1))
    due = store.schedule_pending(up_to=now)
    assert len(due) == 1
    assert due[0].message.content == "past"


def test_schedule_pending_all(store):
    now = datetime.now(UTC)
    store.schedule_add(_msg("a"), now - timedelta(minutes=5))
    store.schedule_add(_msg("b"), now + timedelta(hours=1))
    all_pending = store.schedule_pending()
    assert len(all_pending) == 2


def test_schedule_cancel(store):
    msg = _msg()
    store.schedule_add(msg, datetime.now(UTC) + timedelta(hours=1))
    assert store.schedule_cancel(msg.msg_id) is True
    assert store.schedule_pending() == []


def test_schedule_cancel_nonexistent(store):
    assert store.schedule_cancel("nonexistent-id") is False


def test_schedule_mark_sent(store):
    msg = _msg()
    store.schedule_add(msg, datetime.now(UTC) + timedelta(hours=1))
    store.schedule_mark_sent(msg.msg_id, success=True)
    assert store.schedule_pending() == []


def test_schedule_mark_failed(store):
    msg = _msg()
    store.schedule_add(msg, datetime.now(UTC) + timedelta(hours=1))
    store.schedule_mark_sent(msg.msg_id, success=False)
    assert store.schedule_pending() == []


def test_schedule_transport_stored(store):
    msg = _msg()
    store.schedule_add(msg, datetime.now(UTC) + timedelta(hours=1), transport="js8call")
    entry = store.schedule_pending()[0]
    assert entry.transport == "js8call"


def test_schedule_ordered_by_fire_at(store):
    now = datetime.now(UTC)
    store.schedule_add(_msg("second"), now + timedelta(hours=2))
    store.schedule_add(_msg("first"), now + timedelta(hours=1))
    entries = store.schedule_pending()
    assert entries[0].message.content == "first"
    assert entries[1].message.content == "second"


# -- CLI wiring ---------------------------------------------------------------


def test_cli_schedule_list_empty(cfg, capsys):
    assert main(["schedule", "list"]) == 0
    assert "No pending" in capsys.readouterr().out


def test_cli_schedule_add_delay(cfg, capsys):
    assert main(["schedule", "add", "--delay", "30m", "--to", "W1AW", "test message"]) == 0
    out = capsys.readouterr().out
    assert "Scheduled" in out


def test_cli_schedule_add_1h_delay(cfg, capsys):
    assert main(["schedule", "add", "--delay", "1h", "--to", "W1AW", "1h msg"]) == 0
    assert "Scheduled" in capsys.readouterr().out


def test_cli_schedule_add_compound_delay(cfg, capsys):
    assert main(["schedule", "add", "--delay", "1h30m", "--group", "TTP", "later"]) == 0
    assert "Scheduled" in capsys.readouterr().out


def test_cli_schedule_add_at_time(cfg, capsys):
    assert main(["schedule", "add", "--at", "23:59", "--group", "TTP", "net check"]) == 0
    assert "Scheduled" in capsys.readouterr().out


def test_cli_schedule_list_after_add(cfg, capsys):
    main(["schedule", "add", "--delay", "60m", "--to", "W1AW", "future msg"])
    capsys.readouterr()
    assert main(["schedule", "list"]) == 0
    assert "future msg" in capsys.readouterr().out


def test_cli_schedule_cancel(cfg, capsys):
    main(["schedule", "add", "--delay", "60m", "--to", "W1AW", "to cancel"])
    capsys.readouterr()
    # Fetch the entry id directly from the store
    from radio_app.config import Config
    from radio_app.core.store import MessageStore as MS
    s = MS(Config.load(cfg).database_path())
    ids = [e.id for e in s.schedule_pending()]
    s.close()
    assert len(ids) == 1
    assert main(["schedule", "cancel", ids[0]]) == 0
    assert "Cancelled" in capsys.readouterr().out


def test_cli_schedule_cancel_unknown(cfg, capsys):
    assert main(["schedule", "cancel", "deadbeef"]) == 1
    assert "No pending" in capsys.readouterr().out


def test_cli_schedule_bad_delay(cfg, capsys):
    assert main(["schedule", "add", "--delay", "xyz", "--to", "W1AW", "msg"]) == 2


def test_cli_schedule_missing_time(cfg, capsys):
    assert main(["schedule", "add", "--to", "W1AW", "msg"]) == 2
    assert "--at or --delay" in capsys.readouterr().err
