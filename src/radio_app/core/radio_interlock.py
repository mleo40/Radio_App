"""Radio interlock — stops two HF transports from keying the one radio at once.

JS8Call and Pat/Winlink-over-RF (and Mercury) all drive the *same* physical HF
station: one sound card, one CAT serial port, one PTT line. If two of them try to
transmit together they corrupt each other's audio and fight over PTT. This app
can't share the hardware, and JS8Call/Pat are external programs we only command —
so the realistic gate is to ensure **our app never initiates a transmit on one
radio transport while another holds the radio**, and to tell the operator clearly
how to hand it over.

This module is the pure, transport-agnostic decision seam (no I/O), mirroring the
``Favorites``/``selector`` pattern so it is fully unit-testable. The owning
:class:`~radio_app.app.App` builds one from the transports whose
``capabilities().uses_shared_radio`` is True; the UI claims/releases the token as
the operator switches modes and starts sessions, and consults it before any TX.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class InterlockDecision:
    """Outcome of an :meth:`RadioInterlock.acquire` request.

    ``granted`` is True when the caller may use the radio. ``blocked_by`` names
    the contending transport currently holding it when ``granted`` is False (so
    the UI can say exactly who to stop / switch away from).
    """

    granted: bool
    blocked_by: str | None = None


class RadioInterlock:
    """Single-owner lock shared by the transports that drive the one HF radio.

    Only *contenders* (transports that ``uses_shared_radio``) are gated; every
    other transport (Reticulum, MeshCore, Winlink-over-telnet) is never blocked,
    so :meth:`acquire` is a no-op for them.
    """

    def __init__(self, contenders: Iterable[str] = ()) -> None:
        self._contenders: set[str] = {c for c in contenders if c}
        self._holder: str | None = None

    # -- queries --------------------------------------------------------------

    def is_contender(self, name: str) -> bool:
        """True if ``name`` shares the physical radio (and is therefore gated)."""
        return name in self._contenders

    @property
    def holder(self) -> str | None:
        """The contender currently holding the radio, or ``None`` if free."""
        return self._holder

    def blocked_by(self, name: str) -> str | None:
        """Who would block ``name`` from taking the radio right now (or None).

        ``None`` means ``name`` can acquire it (free, already held by ``name``,
        or ``name`` isn't a contender). A non-None result is the holder to stop.
        """
        if not self.is_contender(name):
            return None
        if self._holder is not None and self._holder != name:
            return self._holder
        return None

    # -- mutations ------------------------------------------------------------

    def acquire(self, name: str) -> InterlockDecision:
        """Claim the radio for ``name`` (idempotent for the current holder).

        Granted when ``name`` isn't a contender, the radio is free, or ``name``
        already holds it. Denied (without changing the holder) when a *different*
        contender holds it.
        """
        if not self.is_contender(name):
            return InterlockDecision(granted=True)
        if self._holder is None or self._holder == name:
            self._holder = name
            return InterlockDecision(granted=True)
        return InterlockDecision(granted=False, blocked_by=self._holder)

    def release(self, name: str) -> None:
        """Release the radio if ``name`` holds it (otherwise a no-op)."""
        if self._holder == name:
            self._holder = None

    def transfer(self, name: str) -> str | None:
        """Explicit operator handoff (e.g. switching modes): force the holder.

        Sets ``name`` as the holder unconditionally (when it's a contender) and
        returns the *previous* holder, so the caller can warn if an active user
        was displaced. Non-contenders simply release whatever was held (moving to
        a non-radio mode frees the radio).
        """
        prev = self._holder
        if self.is_contender(name):
            self._holder = name
        else:
            self._holder = None
        return prev if prev != name else None

