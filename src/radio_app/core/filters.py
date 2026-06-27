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


class FilterEngine:
    """Applies subscriptions + ordered rules to inbound messages."""

    def __init__(self, rules: list[FilterRule], groups: GroupRegistry) -> None:
        self._rules = rules
        self._groups = groups

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

