"""Inbound filter engine.

Each received :class:`UnifiedMessage` is evaluated against an ordered list of
rules; the first matching rule decides the message's disposition. This is the
"filter messages by the user's desires" layer, and it runs the same way for every
transport, so a new medium inherits filtering for free.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum

from .groups import GroupRegistry
from .message import AddressType, UnifiedMessage


class FilterAction(str, Enum):
    NOTIFY = "notify"  # store + alert the user
    SHOW = "show"      # store + show, no alert
    FILE = "file"      # store silently (no alert)
    MUTE = "mute"      # store but hide from active views
    DROP = "drop"      # discard, do not store


@dataclass
class FilterRule:
    """A set of match conditions plus the action to take on a match.

    Supported match keys: ``group`` (supports ``*`` wildcards), ``sender``,
    ``transport``, ``address_type`` and ``contains`` (case-insensitive substring).
    An empty ``match`` matches everything (use as a trailing default).
    """

    action: FilterAction
    match: dict = field(default_factory=dict)
    name: str = ""

    def to_config(self) -> dict:
        """Serialize back to the ``[[filters]]`` TOML shape."""
        out: dict = {"name": self.name, "action": self.action.value}
        if self.match:
            out["match"] = dict(self.match)
        return out

    def matches(self, msg: UnifiedMessage) -> bool:
        m = self.match
        if "address_type" in m and msg.address_type.value != m["address_type"]:
            return False
        if "transport" in m and msg.transport != m["transport"]:
            return False
        if "sender" in m and not fnmatch.fnmatch(msg.sender, m["sender"]):
            return False
        if "group" in m:
            if msg.group is None:
                return False
            if not fnmatch.fnmatch(msg.group, m["group"].lstrip("@")):
                return False
        if "contains" in m and m["contains"].lower() not in msg.content.lower():
            return False
        return True


_MATCH_KEYS = {"group", "sender", "transport", "address_type", "contains"}


def build_filter_rule(name: str, action: str, match_args: list[str]) -> FilterRule:
    """Build a :class:`FilterRule` from CLI/TUI-style args.

    ``match_args`` is a list of ``key=value`` tokens (e.g. ``["group=EMS",
    "transport=js8call"]``). Raises :class:`ValueError` with an operator-facing
    message on an unknown action or match key.
    """
    try:
        act = FilterAction(action.strip().lower())
    except ValueError:
        valid = ", ".join(a.value for a in FilterAction)
        raise ValueError(
            f"invalid action '{action}' (expected one of: {valid})"
        ) from None
    match: dict[str, str] = {}
    for token in match_args:
        if "=" not in token:
            raise ValueError(f"expected key=value, got '{token}'")
        key, _, value = token.partition("=")
        key = key.strip().lower()
        if key not in _MATCH_KEYS:
            valid = ", ".join(sorted(_MATCH_KEYS))
            raise ValueError(f"unknown match key '{key}' (expected one of: {valid})")
        match[key] = value.strip()
    return FilterRule(name=name.strip(), action=act, match=match)


class FilterEngine:
    """Applies subscriptions + ordered rules to inbound messages."""

    def __init__(self, rules: list[FilterRule], groups: GroupRegistry) -> None:
        self._rules = rules
        self._groups = groups

    # -- management (TUI / CLI driven; persist via save()) --------------------

    @property
    def rules(self) -> list[FilterRule]:
        return list(self._rules)

    def find_index(self, ref: str) -> int | None:
        """Resolve a rule name or 1-based index string to a list index."""
        ref = ref.strip()
        if ref.lstrip("-").isdigit():
            i = int(ref) - 1
            return i if 0 <= i < len(self._rules) else None
        for i, r in enumerate(self._rules):
            if r.name == ref:
                return i
        return None

    def add_rule(self, rule: FilterRule) -> None:
        if rule.name and any(r.name == rule.name for r in self._rules):
            raise ValueError(f"a rule named '{rule.name}' already exists")
        # New rules are necessarily more specific than an existing catch-all
        # (empty match), so insert ahead of the first one instead of appending
        # after it - otherwise the catch-all would always win first and the
        # new rule would never fire.
        for i, r in enumerate(self._rules):
            if not r.match:
                self._rules.insert(i, rule)
                return
        self._rules.append(rule)

    def edit_rule(self, ref: str, action: str, match_args: list[str]) -> bool:
        """Replace an existing rule's action/match in place. Name is kept."""
        idx = self.find_index(ref)
        if idx is None:
            return False
        name = self._rules[idx].name
        self._rules[idx] = build_filter_rule(name, action, match_args)
        return True

    def remove_rule(self, ref: str) -> FilterRule | None:
        idx = self.find_index(ref)
        if idx is None:
            return None
        return self._rules.pop(idx)

    def move_rule(self, ref: str, direction: str) -> bool:
        idx = self.find_index(ref)
        if idx is None:
            return False
        rules = self._rules
        if direction == "up" and idx > 0:
            rules[idx - 1], rules[idx] = rules[idx], rules[idx - 1]
            return True
        if direction == "down" and idx < len(rules) - 1:
            rules[idx + 1], rules[idx] = rules[idx], rules[idx + 1]
            return True
        return False

    def save(self, config: object) -> None:
        """Persist rules (name, action, match) back to config."""
        from ..config import Config

        assert isinstance(config, Config)
        config.data["filters"] = [r.to_config() for r in self._rules]
        config.save()

    @classmethod
    def from_config(cls, config: object, groups: GroupRegistry) -> FilterEngine:
        from ..config import Config  # local import to avoid a cycle

        assert isinstance(config, Config)
        rules: list[FilterRule] = []
        for entry in config.filters:
            rules.append(
                FilterRule(
                    name=entry.get("name", ""),
                    action=FilterAction(entry.get("action", "show")),
                    match=dict(entry.get("match", {})),
                )
            )
        return cls(rules, groups)

    def decide(self, msg: UnifiedMessage) -> FilterAction:
        """Return the action to take for an inbound message."""
        # Subscription gate for *named* group traffic (e.g. amateur @EMS groups
        # you opt into). Numeric group tags are device-managed channels - notably
        # MeshCore channels addressed as @0/@2 - that the operator already joined
        # on the companion radio, so they bypass the opt-in gate; otherwise their
        # replies would be dropped (never stored, missing from the channel view)
        # while still leaking into the Watch feed.
        if (
            msg.address_type is AddressType.GROUP
            and msg.group is not None
            and not msg.group.isdigit()
        ):
            if not self._groups.is_subscribed(msg.group):
                if not self._groups.show_unsubscribed:
                    return FilterAction.DROP

        for rule in self._rules:
            if rule.matches(msg):
                return rule.action
        return FilterAction.SHOW

