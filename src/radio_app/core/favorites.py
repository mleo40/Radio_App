"""Favorite peers — identities the operator wants to be alerted about.

A favorite is an identity string (callsign / display label OR an RNS
destination hex hash) with an optional human-friendly label. Matching is:

* case-insensitive equality for non-hex strings (callsigns, names),
* prefix-tolerant for hex hashes in either direction, because announces are
  often surfaced as a 12-char short hash while the user stored the full one
  (or vice-versa).

The list is persisted in ``config.toml`` under ``[[favorites]]`` array tables
so it is hand-editable and survives across runs alongside everything else.
Each entry also remembers ``last_seen`` so restarts don't spuriously re-alert
and the UI can show "last heard" ages immediately.

The :meth:`Favorites.note_sighting` method is the single, pure decision point
that both the TUI and CLI use to decide "is this announce from a favorite, and
is it a fresh come-online event?" — which keeps that logic unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .._compat import UTC
from ..config import Config

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

#: Reserved keys that have their own column in the persisted entry and therefore
#: must never be stored inside the free-form ``meta`` map.
_RESERVED_META_KEYS = frozenset({"id", "label", "last_seen", "kind", "meta"})

# A favorite silent for at least this long is treated as a fresh "back online"
# event the next time it is heard.
DEFAULT_QUIET_SECONDS = 600.0


def _is_hex_id(value: str) -> bool:
    """A string looks like an RNS hex hash if it's hex-only and reasonably long."""
    return bool(_HEX_RE.match(value)) and len(value) >= 8


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    # Normalise to aware-UTC so comparisons never mix naive/aware.
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _parse_meta(entry: dict) -> dict[str, str]:
    """Extract the free-form metadata map from a persisted favorite entry.

    Accepts metadata nested under a ``meta`` table (the canonical form) and is
    tolerant of legacy flat keys: any unknown top-level key is folded into the
    metadata map so hand-edited configs keep working. Values are coerced to
    strings for predictable display and TOML round-tripping.
    """
    meta: dict[str, str] = {}
    raw = entry.get("meta")
    if isinstance(raw, dict):
        for key, value in raw.items():
            key = str(key).strip()
            if key and value not in (None, ""):
                meta[key] = str(value)
    # Tolerate metadata stored as flat top-level keys (hand-edited configs).
    for key, value in entry.items():
        key = str(key).strip()
        if key in _RESERVED_META_KEYS or key in meta:
            continue
        if value not in (None, ""):
            meta[key] = str(value)
    return meta


@dataclass
class Favorite:
    id: str                       # callsign/label OR full/long hex hash
    label: str = ""               # optional human-friendly name shown in the UI
    last_seen: datetime | None = None  # last time we heard this peer (aware UTC)
    #: How this favorite should be opened/grouped. One of "node" (NomadNet
    #: server), "peer" (LXMF/messageable hash), "callsign" (HF), or "" (infer).
    #: Persisted so a node saved while offline still opens the page browser
    #: rather than being mistaken for a peer.
    kind: str = ""
    #: Free-form, operator-supplied metadata about the contact — e.g.
    #: ``{"name": "Bob", "gridsquare": "FN31pr", "power": "5W",
    #: "notes": "QRP CW"}``. Keys are arbitrary so callers can store whatever
    #: they like; values are kept as strings for clean TOML round-tripping.
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return self.meta.get("name") or self.label or self.id


@dataclass
class Sighting:
    """Result of :meth:`Favorites.note_sighting` for a matched favorite."""

    favorite: Favorite
    is_back_online: bool   # True if resurfacing after a quiet period (alert!)
    when: datetime


class Favorites:
    """Mutable, persistable collection of favorite identities."""

    def __init__(self, items: list[Favorite] | None = None) -> None:
        self._items: list[Favorite] = list(items or [])
        # Set when last_seen/list membership changes so callers can avoid
        # rewriting the config file when nothing actually changed.
        self._dirty = False

    # -- loading / saving -----------------------------------------------------

    @classmethod
    def from_config(cls, config: Config) -> Favorites:
        raw = config.data.get("favorites", []) or []
        items: list[Favorite] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            ident = str(entry.get("id", "")).strip()
            if not ident:
                continue
            items.append(
                Favorite(
                    id=ident,
                    label=str(entry.get("label", "") or ""),
                    last_seen=_parse_ts(entry.get("last_seen")),
                    kind=str(entry.get("kind", "") or ""),
                    meta=_parse_meta(entry),
                )
            )
        return cls(items)

    def save(self, config: Config) -> None:
        """Persist the list (including last_seen) back to the config file."""
        config.data["favorites"] = [
            {
                "id": f.id,
                "label": f.label,
                **({"kind": f.kind} if f.kind else {}),
                **(
                    {"last_seen": f.last_seen.isoformat()}
                    if f.last_seen is not None
                    else {}
                ),
                **({"meta": dict(f.meta)} if f.meta else {}),
            }
            for f in self._items
        ]
        config.save()
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    # -- queries --------------------------------------------------------------

    def all(self) -> list[Favorite]:
        return list(self._items)

    def match(self, identity: str) -> Favorite | None:
        """Return the favorite an inbound identity matches, if any."""
        if not identity:
            return None
        ident = identity.strip()
        ident_l = ident.lower()
        for fav in self._items:
            fid_l = fav.id.lower()
            if _is_hex_id(fav.id) and _is_hex_id(ident):
                # Tolerant on either side: announce may carry the long hash
                # while the user stored a short one, or vice-versa.
                if fid_l.startswith(ident_l) or ident_l.startswith(fid_l):
                    return fav
            elif fid_l == ident_l:
                return fav
        return None

    def is_favorite(self, identity: str) -> bool:
        return self.match(identity) is not None

    # -- the decision seam (pure + unit-tested) -------------------------------

    def note_sighting(
        self,
        identity: str,
        *,
        display_name: str = "",
        when: datetime | None = None,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    ) -> Sighting | None:
        """Record that ``identity`` was heard; decide if it's alert-worthy.

        Returns ``None`` when the identity is not a favorite. Otherwise returns
        a :class:`Sighting` with ``is_back_online`` set when the peer had been
        silent for at least ``quiet_seconds`` (or was never heard before).

        Matching tries the destination hash/callsign first, then the optional
        ``display_name`` (so anonymous RNS peers can still match a friendly
        favorite label they advertise).
        """
        fav = self.match(identity)
        if fav is None and display_name:
            fav = self.match(display_name)
        if fav is None:
            return None
        when = when or datetime.now(UTC)
        prev = fav.last_seen
        is_back = prev is None or (when - prev).total_seconds() >= quiet_seconds
        fav.last_seen = when
        self._dirty = True
        return Sighting(favorite=fav, is_back_online=is_back, when=when)

    # -- mutations ------------------------------------------------------------

    def add(self, identity: str, label: str = "", kind: str = "",
            meta: dict[str, str] | None = None) -> Favorite:
        """Add or update a favorite; returns the live instance.

        ``kind`` ("node" | "peer" | "callsign" | "") records how the favorite
        should be opened/grouped and is persisted, so e.g. a NomadNet server
        saved while offline still opens the page browser instead of being
        treated as a messageable peer.

        ``meta`` is a free-form map of contact details (name, gridsquare,
        power, notes, ...). When updating an existing favorite the supplied
        keys are merged in; pass an empty string as a value to delete a key.
        """
        ident = identity.strip()
        if not ident:
            raise ValueError("favorite id must be non-empty")
        existing = self.match(ident)
        if existing is not None:
            if label:
                existing.label = label
                self._dirty = True
            if kind and existing.kind != kind:
                existing.kind = kind
                self._dirty = True
            if meta:
                self._merge_meta(existing, meta)
            return existing
        fav = Favorite(id=ident, label=label.strip(), kind=kind.strip())
        if meta:
            self._merge_meta(fav, meta)
        self._items.append(fav)
        self._dirty = True
        return fav

    def remove(self, identity: str) -> bool:
        target = self.match(identity)
        if target is None:
            return False
        self._items.remove(target)
        self._dirty = True
        return True

    def set_label(self, identity: str, label: str) -> Favorite | None:
        """Set (or clear, with an empty string) the friendly name for an id.

        Creates the entry if it doesn't exist yet (so you can name a contact you
        haven't otherwise saved); clearing a non-existent entry is a no-op and
        returns ``None``. Returns the live :class:`Favorite` otherwise.
        """
        label = label.strip()
        fav = self.match(identity)
        if fav is None:
            if not label:
                return None
            return self.add(identity, label=label)
        if fav.label != label:
            fav.label = label
            self._dirty = True
        return fav

    def set_meta(self, identity: str, meta: dict[str, str]) -> Favorite | None:
        """Set/merge free-form metadata (name, gridsquare, power, notes, ...).

        Creates the entry if it doesn't exist yet (so you can annotate a
        contact you haven't otherwise saved). A key whose value is an empty
        string is removed. Returns the live :class:`Favorite`, or ``None`` if
        there was nothing to do (no existing entry and no non-empty values).
        """
        fav = self.match(identity)
        if fav is None:
            if not any(v.strip() for v in meta.values()):
                return None
            fav = self.add(identity)
        self._merge_meta(fav, meta)
        return fav

    def _merge_meta(self, fav: Favorite, meta: dict[str, str]) -> None:
        """Apply a metadata patch in-place, deleting keys with empty values."""
        for key, value in meta.items():
            key = str(key).strip()
            if key in _RESERVED_META_KEYS or not key:
                continue
            value = str(value).strip()
            if not value:
                if fav.meta.pop(key, None) is not None:
                    self._dirty = True
            elif fav.meta.get(key) != value:
                fav.meta[key] = value
                self._dirty = True

