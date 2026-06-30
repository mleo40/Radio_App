"""Cross-mode bridge / gateway engine.

A bridge rule forwards an inbound message from one transport to another,
letting a node with HF + LoRa act as a relay between disjoint radio nets.

Configuration (``config.toml``)::

    [[bridge]]
    from   = "js8call"
    to     = "reticulum"
    filter = "*"          # optional: *, broadcast, group, direct

    [[bridge]]
    from   = "reticulum"
    to     = "meshcore"

Loop detection: every bridged message carries ``metadata["bridge_path"]``
— a list of transport names it has already traversed. A target transport
already in that list is never used again, so A→B→A cycles are impossible
regardless of how the rules are configured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .message import AddressType, UnifiedMessage

log = logging.getLogger(__name__)

_ADDR_FILTERS = {"*", "broadcast", "group", "direct"}


@dataclass(frozen=True)
class BridgeRule:
    """One forwarding rule: messages arriving on ``from_transport`` are
    re-injected onto ``to_transport``, subject to the address-type filter."""

    from_transport: str
    to_transport: str
    address_filter: str = "*"   # "*" | "broadcast" | "group" | "direct"

    def __post_init__(self) -> None:
        if self.address_filter not in _ADDR_FILTERS:
            raise ValueError(
                f"bridge address_filter must be one of {sorted(_ADDR_FILTERS)!r}, "
                f"got {self.address_filter!r}"
            )

    def _matches_address(self, msg: UnifiedMessage) -> bool:
        f = self.address_filter
        if f == "*":
            return True
        if f == "broadcast":
            return msg.address_type is AddressType.BROADCAST
        if f == "group":
            return msg.address_type is AddressType.GROUP
        if f == "direct":
            return msg.address_type is AddressType.DIRECT
        return False  # unreachable


class BridgeEngine:
    """Evaluates bridge rules and returns the set of target transports for
    each inbound message."""

    def __init__(self, rules: list[BridgeRule]) -> None:
        self._rules = rules

    @classmethod
    def from_config(cls, config: object) -> BridgeEngine:
        """Build from a ``Config`` object (reads ``config.bridges``)."""
        raw: list[dict] = getattr(config, "bridges", []) or []
        rules: list[BridgeRule] = []
        for entry in raw:
            try:
                rules.append(BridgeRule(
                    from_transport=str(entry["from"]),
                    to_transport=str(entry["to"]),
                    address_filter=str(entry.get("filter", "*")),
                ))
            except (KeyError, ValueError) as exc:
                log.warning("skipping invalid bridge rule %r: %s", entry, exc)
        return cls(rules)

    @property
    def rules(self) -> list[BridgeRule]:
        return list(self._rules)

    def targets(self, msg: UnifiedMessage) -> list[str]:
        """Return transport names to forward *msg* onto.

        Excludes any transport already in ``msg.metadata["bridge_path"]``
        (loop guard) and any whose address-type filter doesn't match.
        """
        path: list[str] = msg.metadata.get("bridge_path", [])
        seen: set[str] = set(path)
        out: list[str] = []
        for rule in self._rules:
            if rule.from_transport != msg.transport:
                continue
            if rule.to_transport in seen:
                log.debug(
                    "bridge loop guard: %s already in path %s, skipping",
                    rule.to_transport, path,
                )
                continue
            if not rule._matches_address(msg):
                continue
            if rule.to_transport not in out:
                out.append(rule.to_transport)
        return out
