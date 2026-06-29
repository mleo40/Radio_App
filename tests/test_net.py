"""Tests for NetManager — net control / roll-call session manager."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from radio_app.core.net import CheckIn, NetManager, NetSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_store(tmp_path):
    """Return a minimal MessageStore-like object backed by a temp SQLite DB."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    # The real MessageStore initialises a 'messages' table; net.py only needs
    # the connection, so a bare connection is enough.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages (msg_id TEXT PRIMARY KEY, thread_key TEXT, "
        "sender TEXT, recipient TEXT, group_name TEXT, address_type TEXT, content TEXT, "
        "transport TEXT, status TEXT, metadata TEXT, timestamp TEXT, received_at TEXT)"
    )
    conn.commit()
    store = MagicMock()
    store._conn = conn
    return store


@pytest.fixture()
def nm(tmp_path):
    store = _make_store(tmp_path)
    return NetManager(store)


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

def test_schema_creates_tables(nm):
    tables = {
        r[0]
        for r in nm._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "net_sessions" in tables
    assert "net_check_ins" in tables


# ---------------------------------------------------------------------------
# open()
# ---------------------------------------------------------------------------

def test_open_returns_session(nm):
    s = nm.open("EMS Net", transport="js8call", net_control="KC1QKM")
    assert isinstance(s, NetSession)
    assert s.name == "EMS Net"
    assert s.transport == "js8call"
    assert s.net_control == "KC1QKM"
    assert s.is_open
    assert s.closed_at is None


def test_open_persists_to_db(nm):
    nm.open("Morning Net")
    row = nm._conn.execute(
        "SELECT name, status FROM net_sessions"
    ).fetchone()
    assert row[0] == "Morning Net"
    assert row[1] == "open"


def test_open_raises_when_already_open(nm):
    nm.open("First Net")
    with pytest.raises(ValueError, match="already open"):
        nm.open("Second Net")


def test_open_allowed_after_close(nm):
    nm.open("First Net")
    nm.close()
    s2 = nm.open("Second Net")
    assert s2.is_open


# ---------------------------------------------------------------------------
# active property
# ---------------------------------------------------------------------------

def test_active_none_when_no_session(nm):
    assert nm.active is None


def test_active_returns_open_session(nm):
    nm.open("Test Net")
    assert nm.active is not None
    assert nm.active.name == "Test Net"


def test_active_none_after_close(nm):
    nm.open("Test Net")
    nm.close()
    assert nm.active is None


# ---------------------------------------------------------------------------
# check_in()
# ---------------------------------------------------------------------------

def test_check_in_returns_checkin(nm):
    nm.open("EMS Net", transport="js8call")
    ci = nm.check_in("KE0XYZ")
    assert isinstance(ci, CheckIn)
    assert ci.callsign == "KE0XYZ"
    assert ci.note == ""
    assert ci.row_id is not None


def test_check_in_uppercases_callsign(nm):
    nm.open("Net")
    ci = nm.check_in("ke0xyz")
    assert ci.callsign == "KE0XYZ"


def test_check_in_with_note(nm):
    nm.open("Net")
    ci = nm.check_in("W1AW", "FN42 no traffic")
    assert ci.note == "FN42 no traffic"


def test_check_in_appends_to_session(nm):
    nm.open("Net")
    nm.check_in("KE0XYZ")
    nm.check_in("W1AW")
    assert len(nm.active.check_ins) == 2  # type: ignore[union-attr]
    assert nm.active.check_ins[0].callsign == "KE0XYZ"  # type: ignore[union-attr]
    assert nm.active.check_ins[1].callsign == "W1AW"  # type: ignore[union-attr]


def test_check_in_persists_to_db(nm):
    nm.open("Net")
    nm.check_in("W1AW", "FN42")
    row = nm._conn.execute(
        "SELECT callsign, note FROM net_check_ins"
    ).fetchone()
    assert row[0] == "W1AW"
    assert row[1] == "FN42"


def test_check_in_raises_without_open_session(nm):
    with pytest.raises(ValueError, match="No open net"):
        nm.check_in("W1AW")


def test_check_in_raises_after_close(nm):
    nm.open("Net")
    nm.close()
    with pytest.raises(ValueError, match="No open net"):
        nm.check_in("W1AW")


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

def test_close_returns_session(nm):
    nm.open("Net")
    closed = nm.close()
    assert isinstance(closed, NetSession)
    assert not closed.is_open
    assert closed.closed_at is not None


def test_close_persists_to_db(nm):
    nm.open("Net")
    nm.close()
    row = nm._conn.execute(
        "SELECT status, closed_at FROM net_sessions"
    ).fetchone()
    assert row[0] == "closed"
    assert row[1] is not None


def test_close_includes_check_ins(nm):
    nm.open("Net")
    nm.check_in("KE0XYZ")
    nm.check_in("W1AW")
    closed = nm.close()
    assert len(closed.check_ins) == 2


def test_close_raises_without_open_session(nm):
    with pytest.raises(ValueError, match="No open net"):
        nm.close()


def test_duration_min(nm):
    nm.open("Net")
    nm.check_in("W1AW")
    closed = nm.close()
    assert closed.duration_min is not None
    assert closed.duration_min >= 0


# ---------------------------------------------------------------------------
# recent_sessions()
# ---------------------------------------------------------------------------

def test_recent_sessions_empty(nm):
    assert nm.recent_sessions() == []


def test_recent_sessions_returns_all(nm):
    nm.open("First")
    nm.check_in("KE0XYZ")
    nm.close()
    nm.open("Second")
    nm.close()
    sessions = nm.recent_sessions()
    assert len(sessions) == 2
    # Newest first
    assert sessions[0].name == "Second"
    assert sessions[1].name == "First"


def test_recent_sessions_includes_check_ins(nm):
    nm.open("Net")
    nm.check_in("KE0XYZ")
    nm.check_in("W1AW")
    nm.close()
    sessions = nm.recent_sessions()
    assert len(sessions[0].check_ins) == 2


def test_recent_sessions_limit(nm):
    for i in range(5):
        nm.open(f"Net {i}")
        nm.close()
    assert len(nm.recent_sessions(limit=3)) == 3


# ---------------------------------------------------------------------------
# Persistence across reload (_reload_active)
# ---------------------------------------------------------------------------

def test_reload_restores_active_session(tmp_path):
    store = _make_store(tmp_path)
    nm1 = NetManager(store)
    nm1.open("Morning Net", transport="js8call", net_control="KC1QKM")
    nm1.check_in("KE0XYZ")

    # Simulate restart: new NetManager on the same connection.
    nm2 = NetManager(store)
    assert nm2.active is not None
    assert nm2.active.name == "Morning Net"
    assert nm2.active.transport == "js8call"
    assert len(nm2.active.check_ins) == 1
    assert nm2.active.check_ins[0].callsign == "KE0XYZ"


def test_reload_no_active_when_closed(tmp_path):
    store = _make_store(tmp_path)
    nm1 = NetManager(store)
    nm1.open("Net")
    nm1.close()

    nm2 = NetManager(store)
    assert nm2.active is None
