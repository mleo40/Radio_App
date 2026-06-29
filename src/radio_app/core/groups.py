"""Group model (e.g. @EMS, @EMSNE) + cross-transport membership.

A group is a named, broadcast-style destination. This app owns the
cross-transport group convention: each transport adapter maps a group to its
native mechanism (native ``@``-groups on JS8Call, a defined destination
convention on Reticulum, labelled broadcast elsewhere).

On top of that, a group can declare **members** and **tags** so that *incoming*
traffic from different media can be aggregated under one label. Membership is
entirely operator-declared — there is no automatic identity reconciliation:

* a **member** is a ``transport:identifier`` pair (e.g. ``js8call:KE0XYZ``,
  ``meshcore:a1b2c3…``, ``reticulum:ff0011…``). A bare ``identifier`` with no
  ``transport:`` prefix matches on any transport.
* a **tag** is a native group / label (e.g. a JS8 ``@EMS`` group or a Winlink
  subject tag ``ems``) that maps a message into this group regardless of who
  sent it.

:meth:`GroupRegistry.groups_for_message` is the single, pure lookup the router
uses to stamp each inbound message with the group(s) it belongs to — which keeps
that logic unit-testable and out of the transports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _is_hex_id(value: str) -> bool:
    """A string looks like an RNS hex hash if it's hex-only and reasonably long."""
    return bool(_HEX_RE.match(value)) and len(value) >= 8


@dataclass
class GroupMember:
    """One declared member of a group: an identity, optionally per-transport."""

    identifier: str
    transport: str = ""  # "" => match on any transport

    @classmethod
    def parse(cls, spec: str) -> GroupMember:
        """Parse a ``transport:identifier`` (or bare ``identifier``) spec."""
        spec = spec.strip()
        if ":" in spec:
            tport, _, ident = spec.partition(":")
            return cls(identifier=ident.strip(), transport=tport.strip().lower())
        return cls(identifier=spec)

    @property
    def spec(self) -> str:
        if self.transport:
            return f"{self.transport}:{self.identifier}"
        return self.identifier

    def matches(self, transport: str, identifier: str) -> bool:
        """True if an inbound ``(transport, identifier)`` is this member.

        Hex hashes are prefix-tolerant in either direction (announces often
        surface a short hash while the user stored the full one, or vice-versa);
        callsigns / labels match case-insensitively.
        """
        if not identifier:
            return False
        if self.transport and self.transport != (transport or "").lower():
            return False
        a = self.identifier.strip().lower()
        b = identifier.strip().lower()
        if _is_hex_id(self.identifier) and _is_hex_id(identifier):
            return a.startswith(b) or b.startswith(a)
        return a == b


@dataclass
class Group:
    """A named collective: where it's active for sends + who/what feeds it."""

    name: str                               # without the leading '@'
    display_name: str = ""
    transports: list[str] = field(default_factory=list)
    #: Operator-declared members aggregated into this group's inbound view.
    members: list[GroupMember] = field(default_factory=list)
    #: Native group names / labels (e.g. a Winlink subject tag) that also map
    #: a message into this group regardless of sender.
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = self.name.lstrip("@")
        if not self.display_name:
            self.display_name = self.name

    @property
    def tag(self) -> str:
        return f"@{self.name}"

    def member_specs(self) -> list[str]:
        return [m.spec for m in self.members]


class GroupRegistry:
    """In-memory view of configured groups + the user's subscriptions."""

    def __init__(
        self,
        groups: dict[str, Group] | None = None,
        subscriptions: set[str] | None = None,
        show_unsubscribed: bool = False,
    ) -> None:
        self._groups: dict[str, Group] = groups or {}
        self._subscriptions: set[str] = subscriptions or set()
        self.show_unsubscribed = show_unsubscribed
        self._dirty = False

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_config(cls, config: object) -> GroupRegistry:
        from ..config import Config  # local import to avoid a cycle

        assert isinstance(config, Config)
        groups = {
            name: Group(
                name=name,
                display_name=data.get("display_name", name),
                transports=list(data.get("transports", [])),
                members=[
                    GroupMember.parse(str(s))
                    for s in data.get("members", [])
                    if str(s).strip()
                ],
                tags=[
                    str(t).lstrip("@").strip().lower()
                    for t in data.get("tags", [])
                    if str(t).strip()
                ],
            )
            for name, data in config.groups.items()
        }
        subs = {g.lstrip("@") for g in config.subscriptions.get("groups", [])}
        show = bool(config.subscriptions.get("show_unsubscribed", False))
        return cls(groups, subs, show)

    def save(self, config: object) -> None:
        """Persist groups (name, display, transports, members, tags) to config."""
        from ..config import Config

        assert isinstance(config, Config)
        out: dict[str, dict] = {}
        for name, g in self._groups.items():
            entry: dict = {}
            if g.display_name and g.display_name != g.name:
                entry["display_name"] = g.display_name
            if g.transports:
                entry["transports"] = list(g.transports)
            if g.members:
                entry["members"] = g.member_specs()
            if g.tags:
                entry["tags"] = list(g.tags)
            out[name] = entry
        config.data["groups"] = out
        config.save()
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    # -- queries --------------------------------------------------------------

    def get(self, name: str) -> Group | None:
        return self._groups.get(name.lstrip("@"))

    def all(self) -> list[Group]:
        return list(self._groups.values())

    def is_subscribed(self, name: str) -> bool:
        return name.lstrip("@") in self._subscriptions

    def transports_for(self, name: str) -> list[str]:
        group = self.get(name)
        return list(group.transports) if group else []

    # -- reverse lookup (the read-side aggregation seam) ----------------------

    def groups_for(self, transport: str, identifier: str) -> list[str]:
        """Group names a ``(transport, identifier)`` sender belongs to."""
        if not identifier:
            return []
        out: list[str] = []
        for g in self._groups.values():
            if any(m.matches(transport, identifier) for m in g.members):
                out.append(g.name)
        return out

    def groups_for_tag(self, tag: str | None) -> list[str]:
        """Group names that claim a native group / label ``tag``.

        A group always claims its own name (so a JS8 ``@EMS`` message maps to
        group ``EMS`` with no extra config), plus any explicit ``tags`` it
        declares (e.g. a Winlink subject tag).
        """
        if not tag:
            return []
        t = str(tag).lstrip("@").strip().lower()
        return [
            g.name
            for g in self._groups.values()
            if t == g.name.lower() or t in g.tags
        ]

    def groups_for_message(self, msg: object) -> list[str]:
        """All group names an inbound message aggregates into (deduped, ordered).

        Resolves by sender membership (``transport:identifier``) and by tag — the
        message's native ``group`` field plus an optional ``metadata['tag']``
        (e.g. a Winlink subject tag). Pure and side-effect free.
        """
        transport = getattr(msg, "transport", "") or ""
        sender = getattr(msg, "sender", "") or ""
        names: list[str] = list(self.groups_for(transport, sender))
        native = getattr(msg, "group", None)
        for n in self.groups_for_tag(native):
            if n not in names:
                names.append(n)
        meta = getattr(msg, "metadata", None)
        if isinstance(meta, dict):
            for n in self.groups_for_tag(meta.get("tag")):
                if n not in names:
                    names.append(n)
        return names

    # -- mutation (GUI / CLI driven; persist via save()) ----------------------

    def ensure_group(self, name: str, display_name: str = "") -> Group:
        """Return the named group, creating an empty one if needed."""
        key = name.lstrip("@")
        g = self._groups.get(key)
        if g is None:
            g = Group(name=key, display_name=display_name or key)
            self._groups[key] = g
            self._dirty = True
        elif display_name and g.display_name != display_name:
            g.display_name = display_name
            self._dirty = True
        return g

    def remove_group(self, name: str) -> bool:
        if self._groups.pop(name.lstrip("@"), None) is not None:
            self._dirty = True
            return True
        return False

    def add_member(self, name: str, spec: str) -> GroupMember:
        """Add a ``transport:identifier`` member to a group (creating it)."""
        member = GroupMember.parse(spec)
        if not member.identifier:
            raise ValueError("member identifier must be non-empty")
        g = self.ensure_group(name)
        for existing in g.members:
            if (
                existing.transport == member.transport
                and existing.identifier.lower() == member.identifier.lower()
            ):
                return existing
        g.members.append(member)
        self._dirty = True
        return member

    def remove_member(self, name: str, spec: str) -> bool:
        g = self.get(name)
        if g is None:
            return False
        target = GroupMember.parse(spec)
        before = len(g.members)
        g.members = [
            m
            for m in g.members
            if not (
                m.transport == target.transport
                and m.identifier.lower() == target.identifier.lower()
            )
        ]
        if len(g.members) != before:
            self._dirty = True
            return True
        return False

    def add_tag(self, name: str, tag: str) -> str:
        t = tag.lstrip("@").strip().lower()
        if not t:
            raise ValueError("tag must be non-empty")
        g = self.ensure_group(name)
        if t not in g.tags:
            g.tags.append(t)
            self._dirty = True
        return t

    def remove_tag(self, name: str, tag: str) -> bool:
        g = self.get(name)
        if g is None:
            return False
        t = tag.lstrip("@").strip().lower()
        if t in g.tags:
            g.tags.remove(t)
            self._dirty = True
            return True
        return False

    def subscribe(self, name: str) -> None:
        self._subscriptions.add(name.lstrip("@"))

    def unsubscribe(self, name: str) -> None:
        self._subscriptions.discard(name.lstrip("@"))

    @property
    def subscriptions(self) -> set[str]:
        return set(self._subscriptions)

