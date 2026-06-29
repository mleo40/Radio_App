"""Best-transport selection policy.

Given a message and the available transports, produce an ordered list of
candidates (best first) for the active mode. The router tries them in order with
fallback. Selection is driven entirely by :class:`TransportCapabilities` plus a
live reachability check, so transports added later participate automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..transports.base import Transport, TransportCapabilities
from .message import UnifiedMessage


class SelectionMode(str, Enum):
    """User-facing optimisation goals. Same engine, different weights."""

    AUTO = "auto"          # reachability first, then speed (sensible default)
    FASTEST = "fastest"    # lowest latency
    RELIABLE = "reliable"  # prefer delivery confirmation
    SECURE = "secure"      # require end-to-end encryption
    OFFGRID = "offgrid"    # avoid internet-dependent transports
    BROADCAST = "broadcast"  # every broadcast-capable transport at once


@dataclass(frozen=True)
class _Weights:
    reach: float = 5.0
    speed: float = 1.0
    secure: float = 1.0
    reliable: float = 1.0
    avoid_internet: float = 0.0


_MODE_WEIGHTS: dict[SelectionMode, _Weights] = {
    SelectionMode.AUTO: _Weights(reach=5, speed=2, secure=1, reliable=1),
    SelectionMode.FASTEST: _Weights(reach=3, speed=6, reliable=0.5),
    SelectionMode.RELIABLE: _Weights(reach=3, speed=0.5, reliable=6),
    SelectionMode.SECURE: _Weights(reach=3, secure=8, reliable=1),
    SelectionMode.OFFGRID: _Weights(reach=4, speed=1, avoid_internet=8),
    SelectionMode.BROADCAST: _Weights(reach=2),
}


def _speed_factor(caps: TransportCapabilities) -> float:
    # Map latency to (0, 1]; lower latency -> higher factor.
    return 1.0 / (1.0 + caps.typical_latency_s)


class TransportSelector:
    """Filters and ranks transports for a given message + mode."""

    def __init__(self, transports: list[Transport]) -> None:
        self._transports = transports

    def candidates(
        self,
        msg: UnifiedMessage,
        mode: SelectionMode = SelectionMode.AUTO,
    ) -> list[Transport]:
        """Return transports able to carry ``msg``, best first.

        For BROADCAST mode the full capable set is returned (the router fans out);
        for every other mode the list is the ordered fallback chain.
        """
        viable = [t for t in self._transports if self._is_viable(t, msg, mode)]

        if mode is SelectionMode.BROADCAST:
            return [t for t in viable if t.capabilities().supports_broadcast]

        weights = _MODE_WEIGHTS[mode]
        scored = sorted(
            viable,
            key=lambda t: self._score(t, weights),
            reverse=True,
        )
        return scored

    # -- internals ------------------------------------------------------------

    def _is_viable(
        self, transport: Transport, msg: UnifiedMessage, mode: SelectionMode
    ) -> bool:
        if not transport.running:
            return False
        caps = transport.capabilities()

        # Hard requirement for secure mode: drop plaintext transports.
        if mode is SelectionMode.SECURE and not caps.supports_encryption:
            return False
        # Off-grid: drop internet-dependent transports.
        if mode is SelectionMode.OFFGRID and caps.needs_internet:
            return False
        # Size: the router may chunk, but reject what clearly cannot fit.
        if (
            msg.size > caps.max_message_size
            and not caps.supports_addressing
            and not caps.supports_chunking
        ):
            # broadcast-only tiny links can't carry oversize bodies
            return False
        # Reachability of the specific recipient/group right now.
        return transport.is_reachable(msg)

    def _score(self, transport: Transport, weights: _Weights) -> float:
        caps = transport.capabilities()
        score = 0.0
        score += weights.reach * 1.0  # already filtered to reachable
        score += weights.speed * _speed_factor(caps)
        score += weights.secure * (1.0 if caps.supports_encryption else 0.0)
        score += weights.reliable * (
            1.0 if caps.supports_delivery_confirmation else 0.0
        )
        score += weights.avoid_internet * (0.0 if caps.needs_internet else 1.0)
        return score

