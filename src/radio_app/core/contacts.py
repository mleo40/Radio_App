"""Cross-mode identity linking — the contacts book.

A Contact is a named person with one or more transport-specific addresses
(identities).  Linking JS8Call callsign ``KC1BOB``, MeshCore pubkey
``a1b2c3d4`` and Winlink address ``bob@winlink.org`` under one Contact lets
the TUI and Favorites layer treat them as the same person.

Storage: two tables in the existing MessageStore SQLite DB (same connection
object).  The schema is defined in ``_CONTACTS_SCHEMA`` (imported by
``store.py``) and created idempotently at startup.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

UTC = timezone.utc

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _is_hex(value: str) -> bool:
    return bool(_HEX_RE.match(value)) and len(value) >= 8


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Contact:
    contact_id: str
    display_name: str
    notes: str
    created_at: str  # ISO-8601 string (UTC)


@dataclass
class ContactIdentity:
    identity_id: str
    contact_id: str
    transport: str
    address: str
    label: str


class ContactBook:
    """CRUD layer over the ``contacts`` + ``contact_identities`` tables."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- creation / mutation --------------------------------------------------

    def add(self, display_name: str, notes: str = "") -> Contact:
        """Create and return a new Contact."""
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("display_name must be non-empty")
        contact_id = uuid.uuid4().hex
        created_at = _now_iso()
        self._conn.execute(
            "INSERT INTO contacts (contact_id, display_name, notes, created_at)"
            " VALUES (?, ?, ?, ?)",
            (contact_id, display_name, notes.strip(), created_at),
        )
        self._conn.commit()
        return Contact(contact_id=contact_id, display_name=display_name,
                       notes=notes.strip(), created_at=created_at)

    def rename(self, contact_id: str, display_name: str) -> bool:
        """Rename a contact.  Returns False if the contact_id doesn't exist."""
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("display_name must be non-empty")
        cur = self._conn.execute(
            "UPDATE contacts SET display_name = ? WHERE contact_id = ?",
            (display_name, contact_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def set_notes(self, contact_id: str, notes: str) -> bool:
        cur = self._conn.execute(
            "UPDATE contacts SET notes = ? WHERE contact_id = ?",
            (notes.strip(), contact_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete(self, contact_id: str) -> bool:
        """Delete a contact and cascade-delete all its identities."""
        cur = self._conn.execute(
            "DELETE FROM contacts WHERE contact_id = ?",
            (contact_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def link(
        self,
        contact_id: str,
        transport: str,
        address: str,
        label: str = "",
    ) -> ContactIdentity:
        """Link a transport-specific address to a contact.

        Raises ``ValueError`` if the (transport, address) pair is already
        linked to a *different* contact.  Re-linking the same address to the
        same contact is a no-op that returns the existing row.
        """
        transport = transport.strip().lower()
        address = address.strip()
        label = label.strip()
        if not transport or not address:
            raise ValueError("transport and address must be non-empty")

        existing = self._conn.execute(
            "SELECT identity_id, contact_id, label FROM contact_identities"
            " WHERE transport = ? AND address = ?",
            (transport, address),
        ).fetchone()

        if existing is not None:
            if existing["contact_id"] != contact_id:
                raise ValueError(
                    f"{transport}:{address} is already linked to another contact"
                )
            # Same contact — idempotent
            return ContactIdentity(
                identity_id=existing["identity_id"],
                contact_id=existing["contact_id"],
                transport=transport,
                address=address,
                label=existing["label"],
            )

        identity_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO contact_identities"
            " (identity_id, contact_id, transport, address, label)"
            " VALUES (?, ?, ?, ?, ?)",
            (identity_id, contact_id, transport, address, label),
        )
        self._conn.commit()
        return ContactIdentity(
            identity_id=identity_id,
            contact_id=contact_id,
            transport=transport,
            address=address,
            label=label,
        )

    def unlink(self, transport: str, address: str) -> bool:
        """Remove a transport identity from whichever contact owns it.

        Returns True if a row was deleted, False if the address wasn't linked.
        """
        cur = self._conn.execute(
            "DELETE FROM contact_identities WHERE transport = ? AND address = ?",
            (transport.strip().lower(), address.strip()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # -- queries --------------------------------------------------------------

    def all(self) -> list[Contact]:
        rows = self._conn.execute(
            "SELECT contact_id, display_name, notes, created_at"
            " FROM contacts ORDER BY display_name COLLATE NOCASE"
        ).fetchall()
        return [Contact(**dict(r)) for r in rows]

    def identities_for(self, contact_id: str) -> list[ContactIdentity]:
        rows = self._conn.execute(
            "SELECT identity_id, contact_id, transport, address, label"
            " FROM contact_identities WHERE contact_id = ?"
            " ORDER BY transport, address",
            (contact_id,),
        ).fetchall()
        return [ContactIdentity(**dict(r)) for r in rows]

    def by_address(self, transport: str, address: str) -> Contact | None:
        """Return the Contact whose identity matches (transport, address).

        When ``transport`` is empty-string, search across all transports.
        For hex addresses, prefix-tolerant matching is applied: either the
        stored address or the query can be a prefix of the other.
        """
        address = address.strip()
        tp = transport.strip().lower()

        if tp:
            clause = "WHERE ci.transport = ? AND"
            params: list = [tp, address]
        else:
            clause = "WHERE"
            params = [address]

        # Exact match first (fast path via index).
        row = self._conn.execute(
            f"SELECT c.contact_id, c.display_name, c.notes, c.created_at"
            f" FROM contacts c"
            f" JOIN contact_identities ci ON ci.contact_id = c.contact_id"
            f" {clause} ci.address = ?",
            params,
        ).fetchone()
        if row is not None:
            return Contact(**dict(row))

        # Hex prefix-tolerant fallback (needed for RNS + MeshCore short hashes).
        if not _is_hex(address):
            return None

        if tp:
            rows = self._conn.execute(
                "SELECT ci.address, c.contact_id, c.display_name, c.notes, c.created_at"
                " FROM contact_identities ci"
                " JOIN contacts c ON c.contact_id = ci.contact_id"
                " WHERE ci.transport = ?",
                [tp],
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT ci.address, c.contact_id, c.display_name, c.notes, c.created_at"
                " FROM contact_identities ci"
                " JOIN contacts c ON c.contact_id = ci.contact_id"
            ).fetchall()

        addr_l = address.lower()
        for r in rows:
            stored = r["address"].lower()
            if _is_hex(stored):
                if stored.startswith(addr_l) or addr_l.startswith(stored):
                    return Contact(
                        contact_id=r["contact_id"],
                        display_name=r["display_name"],
                        notes=r["notes"],
                        created_at=r["created_at"],
                    )
        return None

    def by_name(self, name: str) -> list[Contact]:
        """Find contacts whose display_name contains *name* (case-insensitive)."""
        pattern = f"%{name.strip()}%"
        rows = self._conn.execute(
            "SELECT contact_id, display_name, notes, created_at"
            " FROM contacts WHERE display_name LIKE ? COLLATE NOCASE"
            " ORDER BY display_name COLLATE NOCASE",
            (pattern,),
        ).fetchall()
        return [Contact(**dict(r)) for r in rows]

    def display_name_for(self, transport: str, address: str) -> str | None:
        """Return a contact's display_name if the address is linked, else None."""
        contact = self.by_address(transport, address)
        return contact.display_name if contact else None

    def _get_by_id(self, contact_id: str) -> Contact | None:
        row = self._conn.execute(
            "SELECT contact_id, display_name, notes, created_at"
            " FROM contacts WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        return Contact(**dict(row)) if row else None
