"""CLI history + search over the message store (offline, read-only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from radio_app.cli import main
from radio_app.core.message import (
    AddressType,
    DeliveryStatus,
    UnifiedMessage,
)
from radio_app.core.store import MessageStore


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """A config pointing at a DB seeded with a few cross-mode messages."""
    db = tmp_path / "hist.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndatabase = "{db}"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))

    store = MessageStore(db)
    base = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    rows = [
        ("W1AW", "js8call", "TTP", "net starting on 40m", AddressType.GROUP, 0),
        ("KE0XYZ", "js8call", None, "copy, checking in", AddressType.DIRECT, 1),
        ("a1b2c3", "meshcore", None, "mesh relay up here", AddressType.DIRECT, 2),
        ("N0CALL", "reticulum", "TTP", "anyone need a bridge?", AddressType.GROUP, 3),
        ("W1AW", "winlink", None, "SITREP attached", AddressType.DIRECT, 5),
    ]
    for sender, tp, grp, content, at, doff in rows:
        store.save(
            UnifiedMessage(
                sender=sender,
                content=content,
                transport=tp,
                address_type=at,
                group=grp,
                recipient=None if at is AddressType.GROUP else "me",
                status=DeliveryStatus.RECEIVED,
                timestamp=base + timedelta(days=doff),
            )
        )
    store.close()
    return cfg


# -- store.query() (the engine) ---------------------------------------------


def _store(cfg):
    from radio_app.config import Config

    return MessageStore(Config.load(cfg).database_path())


def test_query_no_filters_returns_all_oldest_first(seeded):
    s = _store(seeded)
    try:
        msgs = s.query(newest_first=False)
        assert [m.sender for m in msgs] == [
            "W1AW", "KE0XYZ", "a1b2c3", "N0CALL", "W1AW",
        ]
    finally:
        s.close()


def test_query_filters_by_transport(seeded):
    s = _store(seeded)
    try:
        assert len(s.query(transport="js8call")) == 2
        assert len(s.query(transport="winlink")) == 1
    finally:
        s.close()


def test_query_filters_by_group_tolerates_at_prefix(seeded):
    s = _store(seeded)
    try:
        assert len(s.query(group="TTP")) == 2
        assert len(s.query(group="@TTP")) == 2
    finally:
        s.close()


def test_query_sender_is_case_insensitive_substring(seeded):
    s = _store(seeded)
    try:
        # 'W1AW' appears in two messages; substring + case-insensitive.
        assert len(s.query(sender="w1aw")) == 2
        assert len(s.query(sender="a1b2")) == 1  # mesh hash prefix
    finally:
        s.close()


def test_query_text_search(seeded):
    s = _store(seeded)
    try:
        hits = s.query(text="bridge")
        assert len(hits) == 1 and hits[0].sender == "N0CALL"
    finally:
        s.close()


def test_query_since_until_window(seeded):
    s = _store(seeded)
    try:
        # since 2026-06-24 -> only the 06-25 winlink message
        since = datetime(2026, 6, 24, tzinfo=UTC)
        assert [m.transport for m in s.query(since=since)] == ["winlink"]
        # until 2026-06-21 end-of-day -> first two messages
        until = datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC)
        assert len(s.query(until=until)) == 2
    finally:
        s.close()


def test_query_limit_keeps_most_recent(seeded):
    s = _store(seeded)
    try:
        # Most-recent 2, returned oldest-first for reading.
        msgs = s.query(limit=2, newest_first=False)
        assert [m.transport for m in msgs] == ["reticulum", "winlink"]
    finally:
        s.close()


# -- CLI wiring -------------------------------------------------------------


def test_cli_history_all(seeded, capsys):
    assert main(["history"]) == 0
    out = capsys.readouterr().out
    assert "net starting on 40m" in out
    assert "SITREP attached" in out
    # Cross-mode listing annotates each line with its thread.
    assert "{@TTP}" in out


def test_cli_history_thread_scopes_and_hides_thread_tag(seeded, capsys):
    assert main(["history", "--thread", "@TTP"]) == 0
    out = capsys.readouterr().out
    assert "net starting on 40m" in out and "anyone need a bridge?" in out
    assert "copy, checking in" not in out
    # With a specific thread the {thread} annotation is omitted.
    assert "{@TTP}" not in out


def test_cli_history_mode_and_group_filters(seeded, capsys):
    assert main(["history", "--mode", "meshcore"]) == 0
    out = capsys.readouterr().out
    assert "mesh relay up here" in out and "net starting" not in out


def test_cli_search(seeded, capsys):
    assert main(["search", "checking"]) == 0
    out = capsys.readouterr().out
    assert "copy, checking in" in out and "SITREP" not in out


def test_cli_search_no_match_message(seeded, capsys):
    assert main(["search", "zzz-nonexistent"]) == 0
    assert "no matching messages" in capsys.readouterr().out


def test_cli_history_json_format(seeded, capsys):
    import json

    assert main(["history", "--mode", "winlink", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1 and data[0]["sender"] == "W1AW"


def test_cli_history_bad_date_errors(seeded, capsys):
    assert main(["history", "--since", "not-a-date"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err

