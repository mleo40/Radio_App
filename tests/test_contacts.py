"""Tests for the cross-mode contacts book.

Covers:
- ContactBook CRUD (add, rename, delete, set_notes)
- Identity link / unlink
- Lookup: by_address (exact + hex prefix-tolerant + any-transport),
         by_name (case-insensitive)
- display_name_for
- Cascade delete
- Double-link error
- Favorites auto-link (TUI path)
- CLI: contacts list / add / link / unlink / rename / show
- TUI: /contacts command
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import MagicMock

import pytest

from radio_app.core.contacts import ContactBook


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    """In-memory SQLite connection with contact tables initialised via MessageStore."""
    from radio_app.core.store import MessageStore
    store = MessageStore(str(tmp_path / "test.db"))
    return store._conn


@pytest.fixture()
def book(conn):
    return ContactBook(conn)


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

def test_add_creates_contact(book):
    c = book.add("Bob Smith")
    assert c.display_name == "Bob Smith"
    assert c.contact_id
    assert c.notes == ""
    assert c.created_at


def test_add_with_notes(book):
    c = book.add("Alice", notes="QRP operator")
    assert c.notes == "QRP operator"


def test_add_empty_name_raises(book):
    with pytest.raises(ValueError, match="non-empty"):
        book.add("  ")


def test_all_returns_alphabetical(book):
    book.add("Zelda")
    book.add("Alice")
    book.add("Bob")
    names = [c.display_name for c in book.all()]
    assert names == ["Alice", "Bob", "Zelda"]


def test_rename_contact(book):
    c = book.add("Old Name")
    ok = book.rename(c.contact_id, "New Name")
    assert ok
    updated = book.by_name("New Name")
    assert updated and updated[0].display_name == "New Name"


def test_rename_returns_false_unknown_id(book):
    ok = book.rename("nonexistent-id", "Whatever")
    assert not ok


def test_delete_removes_contact(book):
    c = book.add("Temp")
    ok = book.delete(c.contact_id)
    assert ok
    assert book.all() == []


def test_delete_returns_false_unknown_id(book):
    assert not book.delete("no-such-id")


# ---------------------------------------------------------------------------
# Link / unlink
# ---------------------------------------------------------------------------

def test_link_and_retrieve(book):
    c = book.add("Bob")
    ident = book.link(c.contact_id, "js8call", "KC1BOB", label="HF")
    assert ident.transport == "js8call"
    assert ident.address == "KC1BOB"
    assert ident.label == "HF"
    ids = book.identities_for(c.contact_id)
    assert len(ids) == 1


def test_link_multiple_transports(book):
    c = book.add("Bob")
    book.link(c.contact_id, "js8call", "KC1BOB")
    book.link(c.contact_id, "meshcore", "a1b2c3d4")
    book.link(c.contact_id, "reticulum", "ff001122aabbccdd")
    ids = book.identities_for(c.contact_id)
    assert len(ids) == 3


def test_link_idempotent_same_contact(book):
    c = book.add("Bob")
    ident1 = book.link(c.contact_id, "js8call", "KC1BOB")
    ident2 = book.link(c.contact_id, "js8call", "KC1BOB")
    assert ident1.identity_id == ident2.identity_id
    assert len(book.identities_for(c.contact_id)) == 1


def test_link_raises_if_different_contact(book):
    c1 = book.add("Bob")
    c2 = book.add("Alice")
    book.link(c1.contact_id, "js8call", "KC1BOB")
    with pytest.raises(ValueError, match="already linked"):
        book.link(c2.contact_id, "js8call", "KC1BOB")


def test_unlink_removes_identity(book):
    c = book.add("Bob")
    book.link(c.contact_id, "js8call", "KC1BOB")
    ok = book.unlink("js8call", "KC1BOB")
    assert ok
    assert book.identities_for(c.contact_id) == []


def test_unlink_returns_false_when_not_linked(book):
    assert not book.unlink("js8call", "NOBODY")


def test_delete_cascades_identities(book):
    c = book.add("Bob")
    book.link(c.contact_id, "js8call", "KC1BOB")
    book.link(c.contact_id, "meshcore", "a1b2c3")
    book.delete(c.contact_id)
    # Identities should be gone too (CASCADE).
    rows = book._conn.execute("SELECT * FROM contact_identities").fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# Lookup: by_address
# ---------------------------------------------------------------------------

def test_by_address_exact(book):
    c = book.add("Bob")
    book.link(c.contact_id, "js8call", "KC1BOB")
    found = book.by_address("js8call", "KC1BOB")
    assert found is not None
    assert found.display_name == "Bob"


def test_by_address_not_found(book):
    assert book.by_address("js8call", "NOBODY") is None


def test_by_address_hex_prefix_stored_long(book):
    """Stored full hash, query with short prefix."""
    c = book.add("Alice")
    book.link(c.contact_id, "reticulum", "ff001122aabbccdd1234")
    found = book.by_address("reticulum", "ff001122aabb")
    assert found is not None
    assert found.display_name == "Alice"


def test_by_address_hex_prefix_stored_short(book):
    """Stored short prefix, query with full hash."""
    c = book.add("Bob")
    book.link(c.contact_id, "meshcore", "a1b2c3d4")
    found = book.by_address("meshcore", "a1b2c3d4e5f6")
    assert found is not None


def test_by_address_any_transport(book):
    """transport='' searches across all transports."""
    c = book.add("Bob")
    book.link(c.contact_id, "js8call", "KC1BOB")
    found = book.by_address("", "KC1BOB")
    assert found is not None
    assert found.display_name == "Bob"


# ---------------------------------------------------------------------------
# Lookup: by_name
# ---------------------------------------------------------------------------

def test_by_name_case_insensitive(book):
    book.add("Bob Smith")
    assert book.by_name("bob") != []
    assert book.by_name("SMITH") != []


def test_by_name_no_match(book):
    book.add("Alice")
    assert book.by_name("Zelda") == []


# ---------------------------------------------------------------------------
# display_name_for
# ---------------------------------------------------------------------------

def test_display_name_for_linked(book):
    c = book.add("Bob")
    book.link(c.contact_id, "js8call", "KC1BOB")
    assert book.display_name_for("js8call", "KC1BOB") == "Bob"


def test_display_name_for_unlinked(book):
    assert book.display_name_for("js8call", "KC1BOB") is None


# ---------------------------------------------------------------------------
# Favorites auto-link (TUI path)
# ---------------------------------------------------------------------------

def test_favorites_auto_link(tmp_path):
    """Adding a favorite for a linked address also favorites the other identities."""
    from radio_app.core.store import MessageStore
    from radio_app.core.favorites import Favorites

    store = MessageStore(str(tmp_path / "test.db"))
    book = ContactBook(store._conn)
    favs = Favorites()

    c = book.add("Bob")
    book.link(c.contact_id, "js8call", "KC1BOB")
    book.link(c.contact_id, "meshcore", "a1b2c3d4")

    # Simulate what TUI._contacts_auto_link_favorite does.
    address = "KC1BOB"
    contact = book.by_address("", address)
    assert contact is not None

    newly_added = []
    for ident in book.identities_for(contact.contact_id):
        if ident.address != address and not favs.is_favorite(ident.address):
            favs.add(ident.address, label=contact.display_name)
            newly_added.append(ident.address)

    assert "a1b2c3d4" in newly_added
    assert favs.is_favorite("a1b2c3d4")


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

_CLI_BASE = "[general]\n[logging]\nfile = \"\"\n[station]\ncallsign = \"W1T\"\n"


def test_cli_contacts_list_empty(tmp_path, capsys):
    from radio_app.cli import main as cli_main
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CLI_BASE)
    rc = cli_main(["--config", str(cfg), "contacts", "list"])
    assert rc == 0
    assert "no contacts" in capsys.readouterr().out.lower()


def test_cli_contacts_add(tmp_path, capsys):
    from radio_app.cli import main as cli_main
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CLI_BASE)
    rc = cli_main(["--config", str(cfg), "contacts", "add", "Bob Smith"])
    assert rc == 0
    assert "Bob Smith" in capsys.readouterr().out


def test_cli_contacts_list_shows_entry(tmp_path, capsys):
    from radio_app.cli import main as cli_main
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CLI_BASE)
    cli_main(["--config", str(cfg), "contacts", "add", "Alice"])
    capsys.readouterr()
    rc = cli_main(["--config", str(cfg), "contacts", "list"])
    assert rc == 0
    assert "Alice" in capsys.readouterr().out


def test_cli_contacts_link_and_show(tmp_path, capsys):
    from radio_app.cli import main as cli_main
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CLI_BASE)
    cli_main(["--config", str(cfg), "contacts", "add", "Bob"])
    capsys.readouterr()
    rc = cli_main([
        "--config", str(cfg), "contacts", "link", "Bob", "js8call", "KC1BOB",
        "--label", "HF call",
    ])
    assert rc == 0
    capsys.readouterr()
    rc2 = cli_main(["--config", str(cfg), "contacts", "show", "Bob"])
    assert rc2 == 0
    out = capsys.readouterr().out
    assert "KC1BOB" in out
    assert "js8call" in out


def test_cli_contacts_unlink(tmp_path, capsys):
    from radio_app.cli import main as cli_main
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CLI_BASE)
    cli_main(["--config", str(cfg), "contacts", "add", "Bob"])
    cli_main(["--config", str(cfg), "contacts", "link", "Bob", "js8call", "KC1BOB"])
    capsys.readouterr()
    rc = cli_main(["--config", str(cfg), "contacts", "unlink", "js8call", "KC1BOB"])
    assert rc == 0
    assert "KC1BOB" in capsys.readouterr().out


def test_cli_contacts_rename(tmp_path, capsys):
    from radio_app.cli import main as cli_main
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CLI_BASE)
    cli_main(["--config", str(cfg), "contacts", "add", "Bob"])
    capsys.readouterr()
    rc = cli_main([
        "--config", str(cfg), "contacts", "rename", "Bob", "Robert",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Robert" in out


# ---------------------------------------------------------------------------
# TUI: /contacts command
# ---------------------------------------------------------------------------

pytest.importorskip("textual")
from textual.widgets import RichLog  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402

_TUI_CONFIG = """\
[general]
display_name = "T"
[logging]
file = ""
[station]
callsign = "W1TEST"
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


def test_tui_contacts_empty_list(tui_config):
    """/contacts with no contacts shows a helpful hint."""
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._handle_command("/contacts")
            await pilot.pause()
            text = _log_text(app)
            assert "none" in text.lower() or "no contacts" in text.lower()

    asyncio.run(run())


def test_tui_contacts_new_and_list(tui_config):
    """/contacts new creates a contact; /contacts then lists it."""
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._handle_command("/contacts new Bob Smith")
            await pilot.pause()
            await app._handle_command("/contacts")
            await pilot.pause()
            text = _log_text(app)
            assert "Bob Smith" in text

    asyncio.run(run())


def test_tui_contacts_link_and_show(tui_config):
    """/contacts link stores an identity; /contacts <name> shows it."""
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._handle_command("/contacts new Alice")
            await pilot.pause()
            await app._handle_command("/contacts link Alice js8call W5XYZ")
            await pilot.pause()
            await app._handle_command("/contacts Alice")
            await pilot.pause()
            text = _log_text(app)
            assert "W5XYZ" in text
            assert "js8call" in text

    asyncio.run(run())
