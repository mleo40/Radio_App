"""JS8Call transport (HF weak-signal radio).

We do not drive the modem directly; we talk to a running JS8Call application over
its TCP/JSON API (default port 2442). JS8Call natively understands ``@``-groups
(e.g. @TTP), which is the model the rest of the app generalises from.

This adapter connects with the standard library only. The connection + JSON
framing is implemented, and inbound ``RX.DIRECTED`` events are mapped to a fully
addressed :class:`UnifiedMessage` (direct / group / broadcast) with a stable id
for cross-path de-duplication.

JS8Call API shape we rely on (stable across recent builds):

* ``RX.DIRECTED`` — a received directed/group message. ``params`` carries
  ``FROM``, ``TO``, ``TEXT`` (and often ``CMD``, ``SNR``, ``FREQ``, ``GRID``,
  ``UTC``, ``_ID``). ``TO`` is the routing target: our own callsign (a direct
  message to us), an ``@GROUP`` (group traffic), or ``@ALLCALL`` (broadcast).
* ``RX.SPOT`` / ``RX.CALL_ACTIVITY`` — heard-station telemetry (no body); used
  for the reachability/"recently heard" heuristics, not stored as chat.
* ``TX.SEND_MESSAGE`` — our outbound send: ``value`` is the literal text JS8Call
  should transmit, with the target encoded as a leading token.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time

from ..core.message import AddressType, UnifiedMessage
from .base import ReachabilityStatus, Transport, TransportCapabilities, probe_tcp

log = logging.getLogger(__name__)

# JS8Call "TO" tokens that mean "not a specific recipient" (broadcast on the air).
_BROADCAST_TARGETS = {"", "@ALLCALL", "ALLCALL", "@HB", "@CQ", "CQ"}

# Matches a leading group/callsign target token in free-form JS8 text, e.g.
# "@TTP net in 5" or "KE7XYZ hello". Used only as a fallback when the structured
# ``TO`` param is absent.
_LEADING_TARGET_RE = re.compile(r"^\s*(@?[A-Z0-9/]{2,})[\s:,-]+(.*)$", re.DOTALL)

# JS8Call directed-message commands we recognise. A trailing '?' marks a *query*
# (someone asking us/the group for data); the bare form is the *response*. We
# surface the command (and any parsed value) in metadata so the UI can render
# reports — e.g. an SNR exchange — cleanly instead of as opaque text.
_JS8_COMMANDS = (
    "SNR?", "SNR", "GRID?", "GRID", "INFO?", "INFO", "QTH?", "QTH",
    "STATUS?", "STATUS", "HEARING?", "HEARING", "QSL?", "QSL",
    "AGN?", "ACK", "NACK", "73", "YES", "NO",
)
# Directed commands the operator may *send* to a station/group (e.g. "W1AW SNR?").
# JS8Call encodes these in the message text; we transmit them via TX.SEND_MESSAGE.
JS8_DIRECTED_COMMANDS = frozenset(_JS8_COMMANDS)
# Decibel value following an SNR token in a report, e.g. "... SNR -07" -> -7.
_SNR_VALUE_RE = re.compile(r"SNR[\s:]*([+-]?\d{1,2})", re.IGNORECASE)


def _extract_command(params: dict, text: str) -> tuple[str | None, dict]:
    """Detect a JS8Call directed command in an event and parse its value.

    Prefers JS8Call's structured ``CMD`` param; otherwise scans the text for a
    known command token. Returns ``(command, extra_metadata)`` where ``command``
    is the normalised token (e.g. ``"SNR?"``) or ``None`` for plain chat, and
    ``extra_metadata`` may carry parsed fields such as ``snr_report``.
    """
    cmd = str(params.get("CMD") or "").strip().upper()
    upper = text.upper()
    if not cmd:
        for candidate in _JS8_COMMANDS:
            # Word-ish boundary so "SNR" doesn't match inside another token.
            if re.search(rf"(?:^|[\s:,])({re.escape(candidate)})(?:$|[\s:,])", upper):
                cmd = candidate
                break
    if not cmd:
        return None, {}

    extra: dict = {"js8_command": cmd, "js8_query": cmd.endswith("?")}
    # An SNR *response* carries a decibel value (the query "SNR?" does not).
    if cmd == "SNR":
        m = _SNR_VALUE_RE.search(text)
        if m:
            extra["snr_report"] = int(m.group(1))
    return cmd, extra


def message_from_event(
    event: dict,
    my_callsign: str = "",
    my_groups: tuple[str, ...] = (),
) -> UnifiedMessage | None:
    """Translate one JS8Call API event into a :class:`UnifiedMessage`.

    Pure (no sockets), so it is unit-tested directly. Returns ``None`` for events
    that carry no displayable text (telemetry/spots are handled separately).

    Addressing is derived from the JS8Call ``TO`` routing target:

    * our own callsign            -> :attr:`AddressType.DIRECT` (recipient = us)
    * an ``@GROUP``               -> :attr:`AddressType.GROUP`
    * ``@ALLCALL`` / empty / CQ   -> :attr:`AddressType.BROADCAST`
    * any OTHER station's call    -> :attr:`AddressType.BROADCAST`, flagged
      ``metadata['overheard']`` — traffic between two other stations that we
      merely copied. This is intentionally NOT classed as DIRECT so it never
      pollutes our private 1:1 thread with the sender (which must hold only
      sender<->me traffic); it still surfaces in the all-traffic Watch view.

    The ``msg_id`` is derived deterministically (JS8Call ``_ID`` when present,
    else a hash of FROM|TO|TEXT|FREQ) so the same frame heard twice — or the same
    logical message seen on two paths — de-duplicates in the router.
    """
    etype = event.get("type", "")
    if etype not in ("RX.DIRECTED", "RX.MESSAGE"):
        return None
    params = event.get("params", {}) or {}

    sender = str(params.get("FROM") or params.get("CALL") or "").strip().upper()
    text = str(event.get("value") or params.get("TEXT") or "").strip()
    if not text:
        return None

    target = str(params.get("TO") or "").strip().upper()
    # Fallback: some builds fold the target into the text body ("@TTP hello").
    if not target:
        m = _LEADING_TARGET_RE.match(text)
        if m:
            target, text = m.group(1).upper(), m.group(2).strip()

    my_call = (my_callsign or "").strip().upper()
    groups_upper = {g.lstrip("@").upper() for g in my_groups}

    target_bare = target.lstrip("@")
    is_group = target not in _BROADCAST_TARGETS and (
        target.startswith("@") or target_bare in groups_upper
    )

    address_type = AddressType.BROADCAST
    recipient: str | None = None
    group: str | None = None
    overheard = False
    if not target or target in _BROADCAST_TARGETS:
        address_type = AddressType.BROADCAST
    elif is_group:
        address_type = AddressType.GROUP
        group = target_bare
    elif my_call and target == my_call:
        # Directed to US: a real 1:1 conversation with the sender.
        address_type = AddressType.DIRECT
        recipient = my_call
    else:
        # Directed to ANOTHER station that we merely overheard. Keep it OUT of
        # our private 1:1 thread with the sender (that thread must only hold
        # sender<->me traffic). Surface it as overheard broadcast traffic so it
        # still appears in Watch/monitor, addressed to the station it was for.
        address_type = AddressType.BROADCAST
        recipient = target
        overheard = True

    metadata = {
        "snr": params.get("SNR"),
        "freq": params.get("FREQ"),
        "grid": params.get("GRID"),
        "offset": params.get("OFFSET"),
    }
    if overheard:
        metadata["overheard"] = True
        metadata["to"] = target
    # Recognise JS8 directed commands (SNR?/SNR/GRID?/...) and parse their value.
    _cmd, extra = _extract_command(params, text)
    metadata.update(extra)

    msg = UnifiedMessage(
        sender=sender or "UNKNOWN",
        content=text,
        address_type=address_type,
        recipient=recipient,
        group=group,
        msg_id=_stable_msg_id(params, sender, target, text),
        metadata=metadata,
    )
    return msg


def _stable_msg_id(params: dict, sender: str, target: str, text: str) -> str:
    """Deterministic id for a JS8 frame so duplicates collapse in the router.

    Prefers JS8Call's own ``_ID`` *only when it is a real, positive identifier*.
    JS8Call uses ``_ID = -1`` (a sentinel) for spontaneous RX events — it is the
    same value on every received frame, so trusting it would make the router
    treat every reply as a duplicate of the first and silently drop them. In that
    case (and whenever no id is present) we hash the addressing + body + frequency
    so the same frame copied twice de-duplicates but distinct frames do not.
    """
    try:
        native = int(params.get("_ID", params.get("ID")))
    except (TypeError, ValueError):
        native = 0
    if native > 0:
        return f"js8-{native}"
    basis = "|".join(
        str(x) for x in (sender, target, text, params.get("FREQ", ""))
    )
    return "js8-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


# -- band plan ----------------------------------------------------------------

# Common JS8Call HF/6m dial frequencies per band (Hz). Used both to LABEL the
# current band and to SWITCH bands remotely from the panel.
JS8_BAND_DIAL_HZ: dict[str, int] = {
    "160m": 1_842_000,
    "80m": 3_578_000,
    "60m": 5_358_000,
    "40m": 7_078_000,
    "30m": 10_130_000,
    "20m": 14_078_000,
    "17m": 18_104_000,
    "15m": 21_078_000,
    "12m": 24_922_000,
    "10m": 28_078_000,
    "6m": 50_318_000,
}

# Amateur HF/6m band edges (Hz) for labelling an arbitrary dial frequency.
_BAND_EDGES: tuple[tuple[str, int, int], ...] = (
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("60m", 5_330_000, 5_410_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
)

# JS8Call submode speeds: STATION.STATUS reports SPEED as a small int.
_SPEED_NAMES = {
    "0": "normal",
    "1": "fast",
    "2": "turbo",
    "4": "slow",
}


def band_for_freq(hz: int | None) -> str | None:
    """Return the amateur band name (e.g. ``"20m"``) for a dial frequency in Hz."""
    if not hz:
        return None
    for name, lo, hi in _BAND_EDGES:
        if lo <= hz <= hi:
            return name
    return None


def dial_for_band(band: str | None) -> int | None:
    """Return the JS8Call dial frequency (Hz) for a band name (e.g. ``"20m"``)."""
    if not band:
        return None
    key = band.strip().lower()
    if not key.endswith("m"):
        key += "m"
    return JS8_BAND_DIAL_HZ.get(key)


class JS8CallTransport(Transport):
    name = "js8call"
    surface = "chat"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        # callsign -> last-heard epoch, used for the reachability heuristic
        self._heard: dict[str, float] = {}
        # Re-emit a presence "announce" for a station only after it has been
        # quiet this long, so favorites surface as "online" without spamming.
        self._presence_quiet_s = float(
            config.get("presence_quiet_seconds", 600)
        )
        # Our own callsign (mirrors JS8Call's) so inbound directed traffic to us
        # is recognised as DIRECT rather than someone else's directed message.
        self._my_callsign = str(config.get("callsign", "")).strip().upper()
        # Bare group names we treat as group traffic even without an ``@``.
        groups = config.get("groups") or ()
        self._my_groups = tuple(str(g) for g in groups)
        # Last-known radio state, learned from JS8Call's RIG.FREQ events (both the
        # reply to our RIG.GET_FREQ and the unsolicited ones it sends when the dial
        # moves). Lets the panel show which band we're on without polling the rig.
        self._dial_freq: int | None = None
        self._audio_offset: int = 1500
        # Fuller rig snapshot from STATION.STATUS (submode/speed + selected call).
        self._speed: str = ""
        self._selected_call: str = ""
        # JS8Call's store-and-forward inbox, refreshed on demand via
        # INBOX.GET_MESSAGES and cached from the INBOX.MESSAGES reply.
        self._inbox: list[dict] = []

    def set_identity(self, callsign: str, groups: tuple[str, ...] = ()) -> None:
        """Update the local callsign/groups used for inbound address routing.

        Lets the app push the operator identity (from ``[station]`` or a live
        JS8Call query) into the transport after construction.
        """
        if callsign:
            self._my_callsign = callsign.strip().upper()
        if groups:
            self._my_groups = tuple(groups)

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=80,          # very small frames
            supports_broadcast=True,
            supports_addressing=True,     # directed messages to a callsign
            supports_groups=True,         # native @GROUP support
            supports_encryption=False,    # prohibited on amateur bands
            supports_delivery_confirmation=False,
            is_realtime=False,            # slow turn-taking
            typical_latency_s=30.0,
            needs_internet=False,
            address_scheme="callsign",
            carries_operator_identity=True,  # must ID with callsign on the air
            prohibits_encryption=True,       # encryption prohibited on amateur HF
        )

    async def start(self) -> None:
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", 2442))
        try:
            self._reader, self._writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            log.warning("JS8Call API not reachable at %s:%s (%s).", host, port, exc)
            self._running = False
            return
        self._running = True
        self._reader_task = asyncio.create_task(self._read_loop())
        log.info("JS8Call transport connected to %s:%s", host, port)
        # Learn the current dial frequency so the panel can show the band.
        await self.request_dial_freq()

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()

    async def check_reachable(self) -> ReachabilityStatus:
        """Reachable iff the JS8Call TCP/JSON API accepts a connection."""
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", 2442))
        return await probe_tcp(host, port)

    async def send(self, msg: UnifiedMessage) -> bool:
        if not self._running or self._writer is None:
            return False
        # Build the JS8Call API command. Directed/group text uses the API's
        # TX.SEND_MESSAGE, with the target encoded as a leading token in the body
        # (JS8Call's on-air convention, e.g. "@TTP net in 5" / "KE7XYZ hello").
        if msg.address_type is AddressType.GROUP and msg.group:
            target = f"@{msg.group.lstrip('@')}"
        elif msg.address_type is AddressType.DIRECT and msg.recipient:
            target = msg.recipient.strip().upper()
        else:
            target = "@ALLCALL"
        body = msg.content.strip()
        # JS8 frames are short; warn rather than silently let JS8Call truncate.
        limit = self.capabilities().max_message_size
        if len(body) > limit:
            log.warning(
                "JS8Call message exceeds %d chars (%d); JS8Call may truncate it.",
                limit,
                len(body),
            )
        payload = {"type": "TX.SEND_MESSAGE", "value": f"{target} {body}"}
        return await self._send_api(payload)

    async def _send_api(self, payload: dict) -> bool:
        """Write one JSON command to the JS8Call API socket. Never raises."""
        if not self._running or self._writer is None:
            return False
        try:
            self._writer.write((json.dumps(payload) + "\n").encode())
            await self._writer.drain()
        except OSError as exc:
            log.warning("JS8Call API write failed (%s).", exc)
            return False
        return True

    # -- frequency / band -----------------------------------------------------

    @property
    def dial_freq(self) -> int | None:
        """Last-known radio dial frequency in Hz (None until learned)."""
        return self._dial_freq

    def current_band(self) -> str | None:
        """Amateur band name for the current dial frequency (e.g. ``"20m"``)."""
        return band_for_freq(self._dial_freq)

    async def request_dial_freq(self) -> bool:
        """Ask JS8Call for the current dial frequency (reply updates the cache)."""
        return await self._send_api({"type": "RIG.GET_FREQ", "value": ""})

    async def set_dial_freq(self, hz: int) -> bool:
        """Move the radio's dial to ``hz`` via JS8Call (needs CAT/rig control).

        JS8Call relays this to the rig through Hamlib, so it only takes effect
        when JS8Call has rig control configured. Returns True once the command
        was handed to JS8Call.
        """
        ok = await self._send_api(
            {
                "type": "RIG.SET_FREQ",
                "params": {"DIAL": int(hz), "OFFSET": int(self._audio_offset)},
            }
        )
        if ok:
            # Optimistically reflect the request; the RIG.FREQ reply confirms it.
            self._dial_freq = int(hz)
        return ok

    def _update_freq(self, params: dict) -> None:
        """Cache dial frequency / audio offset from a RIG.FREQ event."""
        dial = params.get("DIAL")
        if dial is not None:
            try:
                self._dial_freq = int(dial)
            except (TypeError, ValueError):
                pass
        offset = params.get("OFFSET")
        if offset is not None:
            try:
                self._audio_offset = int(offset)
            except (TypeError, ValueError):
                pass

    def _update_status(self, params: dict) -> None:
        """Cache speed/selected-callsign from a STATION.STATUS event."""
        speed = params.get("SPEED")
        if speed is not None:
            # JS8Call reports the submode as an int (0=normal..3=turbo) or name.
            self._speed = _SPEED_NAMES.get(str(speed), str(speed)).strip()
        selected = params.get("SELECTED")
        if selected is not None:
            self._selected_call = str(selected).strip().upper()

    async def radio_status(self) -> bool:
        """Ask JS8Call for a full operating snapshot (STATION.GET_STATUS).

        Passive: this only queries JS8Call's view of the rig (which it knows via
        CAT/Hamlib). The reply arrives asynchronously as a ``STATION.STATUS``
        event and updates the cached snapshot. Returns True once the request was
        handed to JS8Call.
        """
        return await self._send_api({"type": "STATION.GET_STATUS", "value": ""})

    def radio_status_snapshot(self) -> dict:
        """Last-known rig operating state for the Health panel.

        Returns dial/freq/offset/speed/selected-callsign/band plus ``cat`` which
        is False when JS8Call has no dial frequency (i.e. no CAT/rig control).
        """
        offset = self._audio_offset
        dial = self._dial_freq
        return {
            "dial": dial,
            "offset": offset,
            "freq": (dial + offset) if dial is not None else None,
            "band": band_for_freq(dial),
            "speed": self._speed,
            "selected": self._selected_call,
            "cat": dial is not None,
        }

    # -- inbox (store-and-forward relay) --------------------------------------

    async def request_inbox(self) -> bool:
        """Ask JS8Call for its stored inbox messages (reply updates the cache)."""
        return await self._send_api({"type": "INBOX.GET_MESSAGES", "value": ""})

    def inbox_messages(self) -> list[dict]:
        """Last-known JS8Call inbox as ``[{"id","from","to","text","utc"}]``."""
        return list(self._inbox)

    async def store_relay_message(self, callsign: str, text: str) -> bool:
        """Leave a store-and-forward message for ``callsign`` in JS8Call's inbox.

        JS8Call relays it on the air when it next hears that station (its native
        store-and-forward messaging). Returns True once handed to JS8Call.
        """
        call = (callsign or "").strip().upper()
        body = (text or "").strip()
        if not call or not body:
            return False
        return await self._send_api(
            {
                "type": "INBOX.STORE_MESSAGE",
                "params": {"CALLSIGN": call, "TEXT": body},
            }
        )

    def _update_inbox(self, params: dict) -> None:
        """Cache the JS8Call inbox from an INBOX.MESSAGES reply.

        JS8Call returns ``params["MESSAGES"]`` as a list; each entry carries the
        stored message fields (directly, or nested under its own ``params``). We
        normalise to ``{"id","from","to","text","utc"}``.
        """
        raw = params.get("MESSAGES")
        if not isinstance(raw, list):
            return
        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            fields = item.get("params") if isinstance(
                item.get("params"), dict
            ) else item
            out.append({
                "id": str(fields.get("_ID") or fields.get("ID") or ""),
                "from": str(fields.get("FROM") or "").upper(),
                "to": str(fields.get("TO") or "").upper(),
                "text": str(fields.get("TEXT") or fields.get("MESSAGE") or ""),
                "utc": fields.get("UTC"),
            })
        self._inbox = out

    # -- directed commands ----------------------------------------------------

    async def send_directed_command(self, target: str, command: str) -> bool:
        """Transmit a JS8Call directed command (e.g. ``SNR?``) to a station/group.

        JS8Call encodes directed commands in the message text (``CALL CMD``); we
        validate the token against :data:`JS8_DIRECTED_COMMANDS` and transmit it
        via TX.SEND_MESSAGE. ``target`` is a callsign or ``@GROUP``. Returns True
        once handed to JS8Call.
        """
        tgt = (target or "").strip().upper()
        cmd = (command or "").strip().upper()
        if not tgt:
            return False
        if cmd not in JS8_DIRECTED_COMMANDS:
            log.warning("JS8Call: unknown directed command %r", command)
            return False
        return await self._send_api(
            {"type": "TX.SEND_MESSAGE", "value": f"{tgt} {cmd}"}
        )

    def is_reachable(self, msg: UnifiedMessage) -> bool:
        if not self._running:
            return False
        if msg.address_type is AddressType.DIRECT and msg.recipient:
            last = self._heard.get(msg.recipient.upper())
            # Heard within the last 30 minutes -> consider reachable.
            return last is not None and (time.time() - last) < 1800
        return True  # groups/broadcast are always "sendable"

    # -- internals ------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while self._running:
            try:
                line = await self._reader.readline()
            except (asyncio.CancelledError, OSError):
                break
            if not line:
                break
            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            await self._handle_event(event)

    async def _handle_event(self, event: dict) -> None:
        etype = event.get("type", "")
        params = event.get("params", {})
        # Trace every event so "why don't replies show up?" is diagnosable from
        # the log file (set [logging] level = "DEBUG"): you can see exactly what
        # JS8Call is pushing and how it was routed.
        log.debug("JS8Call event: %s %s", etype, params)
        # Radio state: cache the dial frequency from JS8Call's RIG.FREQ events
        # (both the reply to our RIG.GET_FREQ and unsolicited dial-move events).
        if etype == "RIG.FREQ":
            self._update_freq(params)
            return
        # Fuller operating snapshot: JS8Call answers STATION.GET_STATUS with the
        # dial/offset plus the current submode speed and the selected callsign.
        # Cache it so the Health panel can show the rig's operating state.
        if etype == "STATION.STATUS":
            self._update_freq(params)
            self._update_status(params)
            return
        # Store-and-forward inbox: cache the reply to our INBOX.GET_MESSAGES so
        # the UI can list messages JS8Call is holding for relay.
        if etype == "INBOX.MESSAGES":
            self._update_inbox(params)
            return
        # Track stations we hear for the reachability heuristic, and surface a
        # presence "announce" (like RNS) when a station resurfaces, so the UI's
        # favorites alerting works on HF too.
        call = params.get("FROM") or params.get("CALL")
        if call:
            key = str(call).upper()
            now = time.time()
            prev = self._heard.get(key)
            self._heard[key] = now
            if prev is None or (now - prev) >= self._presence_quiet_s:
                await self._emit_presence(key, params)
        # Traffic heartbeat: prove JS8 frames are arriving even when they carry
        # no displayable text (e.g. RX.SPOT with only SNR/FREQ). Like presence
        # announces this is telemetry-only - the router skips storage/filtering
        # and it just feeds the UI's "is the transport hearing the band?" tally.
        if etype.startswith("RX.") and etype not in ("RX.DIRECTED", "RX.MESSAGE"):
            await self._emit_traffic(etype, call, params)
        # Inbound text messages -> fully-addressed UnifiedMessage. The pure
        # parser sets direct/group/broadcast from the JS8 "TO" routing target and
        # assigns a stable id for cross-path de-duplication.
        msg = message_from_event(event, self._my_callsign, self._my_groups)
        if msg is not None:
            log.info(
                "JS8Call rx %s from %s -> %s: %s",
                msg.address_type.value,
                msg.sender,
                msg.recipient or (f"@{msg.group}" if msg.group else "?"),
                msg.content,
            )
            await self._emit(msg)

    async def _emit_presence(self, callsign: str, params: dict) -> None:
        """Emit a presence beacon for a heard station (kind='announce').

        These bypass conversation storage/filtering (the router treats
        ``kind='announce'`` as telemetry) and exist so the UI can surface
        "who is on the air" and fire favorite alerts on HF.
        """
        msg = UnifiedMessage(
            sender=callsign,
            content=f"heard on HF: {callsign}",
            address_type=AddressType.BROADCAST,
            metadata={
                "kind": "announce",
                "display_name": callsign,
                "snr": params.get("SNR"),
                "freq": params.get("FREQ"),
            },
        )
        await self._emit(msg)

    async def _emit_traffic(self, etype: str, call: object, params: dict) -> None:
        """Emit a traffic-heartbeat telemetry event (kind='traffic').

        These bypass conversation storage/filtering (the router treats
        ``kind='traffic'`` as telemetry) and exist so the UI can prove the
        JS8 transport is actively hearing the band - a live counterpart to the
        Reticulum announce indicator - regardless of whether a given frame
        contains displayable text.
        """
        msg = UnifiedMessage(
            sender=str(call or "?"),
            content=f"js8 rx: {etype}",
            address_type=AddressType.BROADCAST,
            metadata={
                "kind": "traffic",
                "event": etype,
                "snr": params.get("SNR"),
                "freq": params.get("FREQ"),
            },
        )
        await self._emit(msg)

