"""The message router — the heart of the app.

Outbound: picks the best transport(s) for a message (via the selector) and tries
them in order with fallback. Inbound: de-duplicates, applies the filter engine,
and persists. Cross-cutting concerns (dedup, retry, ACKs, group fan-out) live here
so every transport — including future ones — inherits them for free.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..transports.base import Transport
from .compliance import ComplianceGuard
from .filters import FilterAction, FilterEngine
from .groups import GroupRegistry
from .message import AddressType, DeliveryStatus, UnifiedMessage
from .privacy import apply_outbound_privacy
from .selector import SelectionMode, TransportSelector
from .station import Station
from .store import MessageStore

log = logging.getLogger(__name__)

# Callback the UI registers to be told about freshly accepted inbound messages.
UiCallback = Callable[[UnifiedMessage, FilterAction], None]


class Router:
    """Routes messages between the unified core and the transports."""

    def __init__(
        self,
        transports: list[Transport],
        store: MessageStore,
        groups: GroupRegistry,
        filters: FilterEngine,
        default_mode: SelectionMode = SelectionMode.AUTO,
        station: Station | None = None,
        compliance: ComplianceGuard | None = None,
    ) -> None:
        self._transports = transports
        self._by_name = {t.name: t for t in transports}
        self._store = store
        self._groups = groups
        self._filters = filters
        self._selector = TransportSelector(transports)
        self._default_mode = default_mode
        self._station = station or Station()
        self._compliance = compliance or ComplianceGuard()
        self._ui_callbacks: list[UiCallback] = []
        self._seen: set[str] = set()  # in-memory dedup cache

        for transport in transports:
            transport.on_receive(self._handle_inbound)

    # -- UI hooks -------------------------------------------------------------

    def add_ui_callback(self, callback: UiCallback) -> None:
        """Register a callback invoked with (UnifiedMessage, FilterAction)."""
        self._ui_callbacks.append(callback)

    # -- outbound -------------------------------------------------------------

    async def send(
        self,
        msg: UnifiedMessage,
        mode: SelectionMode | None = None,
        force_transport: str | None = None,
    ) -> bool:
        """Send a message, returning True if any transport accepted it."""
        mode = mode or self._default_mode
        # Secure mode expresses intent to encrypt the payload.
        if mode is SelectionMode.SECURE:
            msg.encrypt = True
        # When the caller pins a transport, attribute the message to it up front
        # so the saved row (and therefore its thread) is scoped correctly even
        # before delivery completes. For auto-selection the transport is
        # recorded on the first successful candidate (see _finalize).
        if force_transport:
            msg.transport = force_transport
        self._store.save(msg)
        self._seen.add(msg.msg_id)

        if force_transport:
            ok = await self._try_one(force_transport, msg)
            self._finalize(msg, ok, force_transport)
            return ok

        if msg.address_type is AddressType.GROUP:
            return await self._send_to_group(msg, mode)

        candidates = self._selector.candidates(msg, mode)
        if not candidates:
            log.warning("No viable transport for %s", msg.msg_id)
            self._finalize(msg, False)
            return False

        # Fallback chain: try best first, then next on failure.
        for transport in candidates:
            if await self._safe_send(transport, msg):
                self._finalize(msg, True, transport.name)
                return True
        self._finalize(msg, False)
        return False

    async def _send_to_group(self, msg: UnifiedMessage, mode: SelectionMode) -> bool:
        """Fan out a group message to every configured + viable transport."""
        wanted = set(self._groups.transports_for(msg.group or ""))
        candidates = [
            t
            for t in self._selector.candidates(msg, SelectionMode.BROADCAST)
            if not wanted or t.name in wanted
        ]
        if not candidates:
            self._finalize(msg, False)
            return False
        results = await asyncio.gather(
            *(self._safe_send(t, msg) for t in candidates)
        )
        ok = any(results)
        self._finalize(msg, ok)
        return ok

    async def _try_one(self, name: str, msg: UnifiedMessage) -> bool:
        transport = self._by_name.get(name)
        if transport is None or not transport.running:
            log.error("Transport %s unavailable", name)
            return False
        return await self._safe_send(transport, msg)

    async def _safe_send(self, transport: Transport, msg: UnifiedMessage) -> bool:
        # Regulatory guard: refuse encrypted payloads on prohibited (HF) media.
        decision = self._compliance.check_outbound(msg, transport)
        if not decision.allowed:
            log.warning("compliance blocked %s: %s", transport.name, decision.reason)
            return False
        # Privacy: per-transport identity (callsign on HF, anonymous on RNS).
        outbound = apply_outbound_privacy(msg, transport, self._station)
        try:
            return await transport.send(outbound)
        except Exception:  # noqa: BLE001 - never let one transport break a send
            log.exception("send failed on %s", transport.name)
            return False

    def _finalize(
        self, msg: UnifiedMessage, ok: bool, transport: str | None = None
    ) -> None:
        msg.status = DeliveryStatus.SENT if ok else DeliveryStatus.FAILED
        if transport:
            msg.transport = transport
        # Persist BOTH the status and the (now-known) transport so the message
        # is attributed to the medium that carried it - the thread list scopes
        # conversations by transport, so an unset transport would hide them.
        self._store.update_status(
            msg.msg_id, msg.status, transport=msg.transport or None
        )

    # -- inbound --------------------------------------------------------------

    async def _handle_inbound(self, msg: UnifiedMessage) -> None:
        # Network "telemetry" events (e.g. RNS announces, JS8 traffic beacons,
        # LXMF delivery receipts) bypass dedup, filtering and persistence - they
        # aren't conversations. They flow only to the UI so the Monitor / status
        # bar / sent-message indicators can surface them.
        if msg.metadata.get("kind") in ("announce", "traffic", "delivery"):
            for callback in self._ui_callbacks:
                try:
                    callback(msg, FilterAction.SHOW)
                except Exception:  # noqa: BLE001
                    log.exception("ui callback failed")
            return

        # De-duplicate across paths (same logical message on two transports).
        if msg.msg_id in self._seen or self._store.exists(msg.msg_id):
            log.debug("dropping duplicate %s", msg.msg_id)
            return
        self._seen.add(msg.msg_id)
        msg.status = DeliveryStatus.RECEIVED

        action = self._filters.decide(msg)
        # Persist everything except explicit DROPs, but ALWAYS notify the UI so
        # the Monitor can show a complete, all-transport feed. The UI honours the
        # action (drop/mute/notify) for the active views and alerts.
        if action is not FilterAction.DROP:
            self._store.save(msg)
        for callback in self._ui_callbacks:
            try:
                callback(msg, action)
            except Exception:  # noqa: BLE001
                log.exception("ui callback failed")
