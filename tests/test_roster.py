"""Presence roster: get_roster() and CLI."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from radio_app.cli import main
from radio_app.core.message import DeliveryStatus, UnifiedMessage
from radio_app.core.roster import PresenceEntry, get_roster
from radio_app.core.store import MessageStore


@pytest.fixture()
def store_with_msgs(tmp_path):
    db = tmp_path / "roster.db"
    s = MessageStore(db)
    base = datetime.now(UTC) - timedelta(hours=2)
    msgs = [
        UnifiedMessage(
            sender="W1AW", content="hello", transport="js8call",
            status=DeliveryStatus.RECEIVED, timestamp=base,
            metadata={"snr": 5.0},
        ),
        UnifiedMessage(
            sender="W1AW", content="second", transport="js8call",
            status=DeliveryStatus.RECEIVED,
            timestamp=base + timedelta(minutes=30),
            metadata={"snr": 3.0},
        ),
        UnifiedMessage(
            sender="KE0XYZ", content="hi there", transport="reticulum",
            status=DeliveryStatus.RECEIVED,
            timestamp=base + timedelta(hours=1), metadata={},
        ),
        UnifiedMessage(
            sender="N0CALL", content="old msg", transport="js8call",
            status=DeliveryStatus.RECEIVED,
            timestamp=base - timedelta(hours=30), metadata={},
        ),
    ]
    for m in msgs:
        s.save(m)
    yield s
    s.close()


# -- get_roster() -------------------------------------------------------------


def test_roster_returns_recent(store_with_msgs):
    entries = get_roster(store_with_msgs)
    callsigns = [e.callsign for e in entries]
    assert "W1AW" in callsigns
    assert "KE0XYZ" in callsigns
    # N0CALL is older than 24h default window
    assert "N0CALL" not in callsigns


def test_roster_message_count(store_with_msgs):
    entries = get_roster(store_with_msgs)
    w1aw = next(e for e in entries if e.callsign == "W1AW")
    assert w1aw.message_count == 2


def test_roster_last_snr(store_with_msgs):
    entries = get_roster(store_with_msgs)
    w1aw = next(e for e in entries if e.callsign == "W1AW")
    # Most recent W1AW message has SNR 3.0
    assert w1aw.last_snr == pytest.approx(3.0)


def test_roster_no_snr(store_with_msgs):
    entries = get_roster(store_with_msgs)
    ke0xyz = next(e for e in entries if e.callsign == "KE0XYZ")
    assert ke0xyz.last_snr is None


def test_roster_transport_filter(store_with_msgs):
    entries = get_roster(store_with_msgs, transport="reticulum")
    assert all(e.transport == "reticulum" for e in entries)
    assert len(entries) == 1


def test_roster_since_filter(store_with_msgs):
    # Only KE0XYZ's message is within the last 1.5h
    since = datetime.now(UTC) - timedelta(hours=1, minutes=30)
    entries = get_roster(store_with_msgs, since=since)
    callsigns = [e.callsign for e in entries]
    assert "KE0XYZ" in callsigns
    assert "W1AW" not in callsigns


def test_roster_most_recent_first(store_with_msgs):
    entries = get_roster(store_with_msgs)
    if len(entries) >= 2:
        assert entries[0].last_seen >= entries[1].last_seen


def test_roster_last_content_preview(store_with_msgs):
    entries = get_roster(store_with_msgs)
    w1aw = next(e for e in entries if e.callsign == "W1AW")
    assert w1aw.last_content == "second"


def test_roster_empty_store(tmp_path):
    s = MessageStore(tmp_path / "empty.db")
    try:
        assert get_roster(s) == []
    finally:
        s.close()


# -- CLI wiring ---------------------------------------------------------------


@pytest.fixture()
def roster_cfg(tmp_path, monkeypatch):
    db = tmp_path / "roster.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndatabase = "{db}"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    s = MessageStore(db)
    base = datetime.now(UTC) - timedelta(hours=1)
    s.save(UnifiedMessage(
        sender="W1AW", content="hi", transport="js8call",
        status=DeliveryStatus.RECEIVED, timestamp=base,
        metadata={"snr": 7.0},
    ))
    s.close()
    return cfg


def test_cli_roster(roster_cfg, capsys):
    assert main(["roster"]) == 0
    out = capsys.readouterr().out
    assert "W1AW" in out
    assert "js8call" in out


def test_cli_roster_snr_shown(roster_cfg, capsys):
    assert main(["roster"]) == 0
    out = capsys.readouterr().out
    assert "+7" in out or "7" in out


def test_cli_roster_transport_filter(roster_cfg, capsys):
    assert main(["roster", "--transport", "reticulum"]) == 0
    assert "No stations" in capsys.readouterr().out


def test_cli_roster_empty(tmp_path, monkeypatch, capsys):
    db = tmp_path / "empty.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndatabase = "{db}"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    assert main(["roster"]) == 0
    assert "No stations" in capsys.readouterr().out


def test_cli_roster_since_flag(roster_cfg, capsys):
    assert main(["roster", "--since", "48h"]) == 0
    out = capsys.readouterr().out
    assert "W1AW" in out
