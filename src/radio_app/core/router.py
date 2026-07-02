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
from dataclasses import replace

from ..transports.base import Transport
from .chunking import (
    Reassembler,
    group_id,
    make_ack,
    needs_chunking,
    parse_ack,
    parse_chunk,
    segment,
)
from .bridge import BridgeEngine
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
        ack_timeout: float = 30.0,
        ack_retries: int = 2,
        bridge: BridgeEngine | None = None,
        interlock: object | None = None,
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
        self._bridge = bridge
        self._interlock = interlock
        self._ui_callbacks: list[UiCallback] = []
        self._seen: set[str] = set()  # in-memory dedup cache
        # -- app-level chunking / ACK-retry (small-MTU transports) ------------
        self._reassembler = Reassembler()
        self._ack_timeout = ack_timeout
        self._ack_retries = ack_retries
        # Outbound chunk groups awaiting ACKs: (transport, gid) -> state dict.
        self._pending_sends: dict[tuple[str, str], dict] = {}
        # Background retransmit tasks, same key, so we can cancel/replace them.
        self._retry_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # Inbound groups we've already fully reassembled (drop late duplicates).
        self._completed: set[tuple[str, str]] = set()

        for transport in transports:
            transport.on_receive(self._handle_inbound)

    # -- UI hooks -------------------------------------------------------------

    def add_ui_callback(self, callback: UiCallback) -> None:
        """Register a callback invoked with (UnifiedMessage, FilterAction)."""
        self._ui_callbacks.append(callback)

    def remove_ui_callback(self, callback: UiCallback) -> None:
        """Unregister a previously added UI callback (no-op if not registered)."""
        try:
            self._ui_callbacks.remove(callback)
        except ValueError:
            pass

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
        caps = transport.capabilities()
        # App-level chunking: split a body that won't fit one frame on a small-MTU
        # medium into several framed parts the receiver reassembles.
        if caps.supports_chunking and needs_chunking(
            outbound.content, caps.max_message_size
        ):
            return await self._send_chunked(transport, outbound, caps)
        try:
            return await transport.send(outbound)
        except Exception:  # noqa: BLE001 - never let one transport break a send
            log.exception("send failed on %s", transport.name)
            return False

    # -- chunking / ACK-retry -------------------------------------------------

    async def _raw_send(self, transport: Transport, msg: UnifiedMessage) -> bool:
        """Hand one already-prepared frame to a transport, swallowing errors."""
        try:
            return await transport.send(msg)
        except Exception:  # noqa: BLE001
            log.exception("send failed on %s", transport.name)
            return False

    async def _send_chunked(
        self, transport: Transport, outbound: UnifiedMessage, caps
    ) -> bool:
        """Segment ``outbound`` and transmit each part; arm ACK-retry if useful."""
        gid = group_id(outbound.msg_id)
        parts = segment(outbound.content, caps.max_message_size, gid=gid)
        log.info(
            "[chunk] %s: %d parts over %s", gid, len(parts), transport.name
        )
        ok = True
        for part in parts:
            if not await self._raw_send(transport, replace(outbound, content=part)):
                ok = False
        # Only retransmit where the medium can't confirm delivery itself and we
        # can address a reply target (DIRECT messages get ACKs from the peer).
        if (
            ok
            and self._ack_retries > 0
            and not caps.supports_delivery_confirmation
            and outbound.address_type is AddressType.DIRECT
        ):
            self._arm_retry(transport, outbound, gid, parts)
        return ok

    def _arm_retry(
        self,
        transport: Transport,
        outbound: UnifiedMessage,
        gid: str,
        parts: list[str],
    ) -> None:
        key = (transport.name, gid)
        self._pending_sends[key] = {"acked": set(), "total": len(parts)}
        old = self._retry_tasks.pop(key, None)
        if old is not None:
            old.cancel()
        try:
            self._retry_tasks[key] = asyncio.create_task(
                self._retry_loop(transport, outbound, key, parts)
            )
        except RuntimeError:
            # No running loop (e.g. a unit test sending synchronously): skip the
            # background retransmit — the parts were already sent once.
            self._pending_sends.pop(key, None)

    async def _retry_loop(
        self,
        transport: Transport,
        outbound: UnifiedMessage,
        key: tuple[str, str],
        parts: list[str],
    ) -> None:
        total = len(parts)
        try:
            for attempt in range(self._ack_retries):
                await asyncio.sleep(self._ack_timeout)
                state = self._pending_sends.get(key)
                if state is None:
                    return
                missing = [s for s in range(1, total + 1) if s not in state["acked"]]
                if not missing:
                    return
                log.info(
                    "[chunk] %s: retransmit %d/%d parts (attempt %d)",
                    key[1],
                    len(missing),
                    total,
                    attempt + 1,
                )
                for seq in missing:
                    await self._raw_send(
                        transport, replace(outbound, content=parts[seq - 1])
                    )
        finally:
            self._pending_sends.pop(key, None)
            self._retry_tasks.pop(key, None)

    def _note_ack(self, transport_name: str, gid: str, received: set[int]) -> None:
        state = self._pending_sends.get((transport_name, gid))
        if state is not None:
            state["acked"].update(received)

    def _maybe_send_ack(
        self, msg: UnifiedMessage, part, *, final: bool = False
    ) -> None:
        """Acknowledge received chunk parts so the sender can stop retransmitting.

        Only for DIRECT messages on media without native delivery confirmation;
        the ACK is a tiny control frame addressed back to the original sender.
        """
        if msg.address_type is not AddressType.DIRECT:
            return
        transport = self._by_name.get(msg.transport)
        if transport is None or not transport.running:
            return
        if transport.capabilities().supports_delivery_confirmation:
            return
        if final:
            received: set[int] = set(range(1, part.total + 1))
        else:
            received = self._reassembler.received_seqs(msg.sender or "", part.gid)
        frame = make_ack(part.gid, received, part.total)
        ack_msg = UnifiedMessage.direct(
            sender=msg.recipient or "", recipient=msg.sender or "", content=frame
        )
        try:
            asyncio.create_task(self._raw_send(transport, ack_msg))
        except RuntimeError:
            pass  # no loop running (sync test path)

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

        # App-level chunking: absorb ACK frames and reassemble multi-frame
        # messages before they reach dedup / filtering / persistence. Frames that
        # aren't chunk/ACK control frames fall straight through untouched.
        text = msg.content or ""
        ack = parse_ack(text)
        if ack is not None:
            self._note_ack(msg.transport, ack.gid, set(ack.received))
            return
        part = parse_chunk(text)
        if part is not None:
            source = msg.sender or ""
            gkey = (source, part.gid)
            if gkey in self._completed:
                # A late duplicate of an already-finished message: re-ACK so the
                # sender stops, but don't surface it twice.
                self._maybe_send_ack(msg, part, final=True)
                return
            full = self._reassembler.add(source, part)
            if full is None:
                self._maybe_send_ack(msg, part)
                return
            self._completed.add(gkey)
            if len(self._completed) > 1024:
                self._completed.clear()
            msg.content = full
            self._maybe_send_ack(msg, part, final=True)
            # Fall through with the fully reassembled message.

        # De-duplicate across paths (same logical message on two transports).
        if msg.msg_id in self._seen or self._store.exists(msg.msg_id):
            log.debug("dropping duplicate %s", msg.msg_id)
            return
        self._seen.add(msg.msg_id)
        msg.status = DeliveryStatus.RECEIVED

        # Cross-mode grouping: stamp the message with any operator-declared
        # group(s) it belongs to (by sender membership or tag) so the store and
        # UI can aggregate one collective's traffic across every transport. This
        # is read-side only — outbound fan-out is deliberately not done here.
        try:
            group_names = self._groups.groups_for_message(msg)
            if group_names:
                msg.metadata["groups"] = group_names
        except Exception:  # noqa: BLE001 - grouping must never break delivery
            log.exception("group stamping failed for %s", msg.msg_id)

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

        # Cross-mode bridge: re-inject onto peer transports per config rules.
        # Skipped for DROPped messages and when no bridge is configured.
        if self._bridge and action is not FilterAction.DROP:
            self._dispatch_bridge(msg)

    def _dispatch_bridge(self, msg: UnifiedMessage) -> None:
        """Forward *msg* to configured peer transports (fire-and-forget tasks)."""
        import uuid as _uuid

        assert self._bridge is not None
        targets = self._bridge.targets(msg)
        if not targets:
            return

        new_path = msg.metadata.get("bridge_path", []) + [msg.transport]

        for target in targets:
            # Check the radio interlock without acquiring — the bridge is a
            # relay, not an operator TX session. Skip if busy, don't retry.
            if self._interlock is not None:
                blocker = self._interlock.blocked_by(target)
                if blocker:
                    log.warning(
                        "bridge %s→%s skipped: radio busy (%s)",
                        msg.transport, target, blocker,
                    )
                    continue

            bridged = replace(
                msg,
                msg_id=_uuid.uuid4().hex,
                metadata={
                    **msg.metadata,
                    "bridge_path": new_path,
                    "bridged": True,
                    "bridge_origin": msg.transport,
                },
            )
            asyncio.create_task(
                self.send(bridged, force_transport=target),
                name=f"bridge-{msg.transport}-{target}",
            )
