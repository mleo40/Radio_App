"""MeshCore transport (license-free ISM LoRa mesh).

We talk to a MeshCore *companion* device over USB serial or TCP using the
official :mod:`meshcore` Python library, which implements the companion binary
protocol (framing, contacts, messages, adverts). Selected via
``[transports.meshcore] connection = "serial" | "tcp"``.

Unlike the HF transports (JS8Call/Mercury), MeshCore runs on license-free ISM
bands and encrypts natively, so it is NOT subject to the amateur callsign /
no-encryption compliance rules - see :meth:`capabilities`.

Addressing mapped onto :class:`UnifiedMessage`:

* DIRECT  -> a MeshCore contact, addressed by its public-key prefix (hex) or a
             known contact name. Inbound direct messages arrive as
             ``CONTACT_MSG_RECV`` events keyed by the sender's pubkey prefix.
* GROUP   -> a MeshCore *channel*; ``msg.group`` is the numeric channel index as
             a string (e.g. "0" for the public channel). Inbound channel traffic
             arrives as ``CHANNEL_MSG_RECV`` events. Configured channels are
             enumerated at startup (and can be named in config) so the Mesh panel
             can list them as conversations - see :meth:`channels`.
* adverts -> surfaced as ``kind='announce'`` telemetry so Watch/Health and the
             favorites "last heard" logic light up (never stored as chat).

If the ``meshcore`` library is not installed the transport stays disabled rather
than crashing the app (mirrors the Reticulum transport's optional dependency).
"""

from __future__ import annotations

import logging
import os

from ..core.message import AddressType, UnifiedMessage
from .base import ReachabilityStatus, Transport, TransportCapabilities, probe_tcp

# The companion protocol + serial/TCP/BLE backends live in the optional
# ``meshcore`` package. Guard the import so other users (and CI) don't require it
# unless this transport is actually enabled.
try:  # pragma: no cover - presence depends on the environment
    from meshcore import EventType, MeshCore  # type: ignore

    _HAVE_MESHCORE = True
except ImportError:  # pragma: no cover
    MeshCore = None  # type: ignore[assignment]
    EventType = None  # type: ignore[assignment]
    _HAVE_MESHCORE = False

log = logging.getLogger(__name__)

_HEX = set("0123456789abcdefABCDEF")


class MeshCoreTransport(Transport):
    name = "meshcore"
    surface = "chat"

    #: How many channel slots to probe on the companion at startup. MeshCore
    #: firmware exposes a small fixed set (commonly 8); unconfigured slots are
    #: simply skipped.
    MAX_CHANNELS = 8

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # "serial" (USB) or "tcp" - picks which backend start()/probe use.
        self._connection = str(config.get("connection", "serial")).lower()
        self._mc = None  # the meshcore.MeshCore instance once connected
        self._subs: list = []  # event subscriptions to clean up on stop()
        self._fetch_sub = None  # auto message-fetching subscription
        # Channels (group conversations) keyed by index. Names typed by the
        # operator in [transports.meshcore].channels are merged with whatever the
        # device reports at start(), so the Mesh panel can list them up front.
        self._config_channels = self._parse_config_channels(config.get("channels"))
        self._device_channels: list[dict] = []

    @staticmethod
    def _parse_config_channels(raw: object) -> list[dict]:
        """Normalise the optional ``channels`` config into channel specs.

        Each spec is ``{index, name}`` plus optionally ``secret`` (hex key for a
        secret channel) and ``hashtag`` (``False`` to mark a label-only entry for
        a device-managed channel that must NOT be recreated). Accepts a list of
        tables (``{index=0, name="public"}``), bare ints, or ``"<index>:<name>"``
        strings. Anything unparseable is ignored so a typo never breaks startup.
        """
        out: list[dict] = []
        if not isinstance(raw, (list, tuple)):
            return out
        for item in raw:
            secret = ""
            hashtag: bool | None = None
            try:
                if isinstance(item, dict):
                    idx = int(item.get("index"))
                    name = str(item.get("name", "")).strip()
                    secret = str(item.get("secret", "") or "").strip()
                    if "hashtag" in item:
                        hashtag = bool(item.get("hashtag"))
                elif isinstance(item, int):
                    idx, name = int(item), ""
                elif isinstance(item, str) and ":" in item:
                    head, name = item.split(":", 1)
                    idx, name = int(head), name.strip()
                elif isinstance(item, str) and item.strip().isdigit():
                    idx, name = int(item.strip()), ""
                else:
                    continue
            except (TypeError, ValueError):
                continue
            spec: dict = {"index": idx, "name": name}
            if secret:
                spec["secret"] = secret
            if hashtag is not None:
                spec["hashtag"] = hashtag
            out.append(spec)
        return out

    # -- introspection --------------------------------------------------------

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=134,            # MeshCore direct text payload (bytes)
            supports_broadcast=True,
            supports_addressing=True,        # direct messages to a contact
            supports_groups=True,            # MeshCore channels
            supports_encryption=True,        # MeshCore encrypts natively
            supports_delivery_confirmation=True,  # MSG_SENT / ACK
            is_realtime=False,
            typical_latency_s=5.0,
            needs_internet=False,
            address_scheme="node_id",
            # ISM band, mesh identity (not a callsign): the operator-identity and
            # encryption-prohibition compliance rules that apply to amateur HF do
            # NOT apply here.
            carries_operator_identity=False,
            prohibits_encryption=False,
        )

    def local_identity(self) -> str | None:
        """Our MeshCore public key, used as the anonymous sender on this medium."""
        return self._self_info().get("public_key") or None

    def local_display_name(self) -> str:
        """Our MeshCore node name (as configured on the companion device).

        Surfaced in the TUI status bar's ``id:`` fragment so the operator can
        see which named node they're transmitting as. Blank until the device
        reports its self-info.
        """
        return str(self._self_info().get("name") or "")

    def channels(self) -> list[dict]:
        """Configured group channels as ``[{"index": int, "name": str}]``.

        Merges operator-named channels from config with whatever the device
        reported at :meth:`start`; the public channel (index 0) is always
        present so the Mesh panel can offer it even on a fresh device. Sorted by
        index for a stable list.
        """
        merged: dict[int, str] = {0: ""}  # public channel always available
        for ch in self._device_channels:
            merged[ch["index"]] = ch.get("name", "") or merged.get(ch["index"], "")
        # An explicit config name wins over the device-reported (often blank) one.
        for ch in self._config_channels:
            name = ch.get("name", "")
            merged[ch["index"]] = name or merged.get(ch["index"], "")
        return [{"index": idx, "name": merged[idx]} for idx in sorted(merged)]

    def config_channels(self) -> list[dict]:
        """The operator-named channels (for persisting back to config)."""
        return [dict(c) for c in self._config_channels]

    def name_channel(
        self, index: int, name: str, secret_hex: str | None = None
    ) -> None:
        """Remember a channel's display name (and secret) for the config.

        Updates the in-memory config-channel list so :meth:`channels` reflects
        it immediately; the caller persists it to the config file. Passing an
        empty name removes the local label. When ``secret_hex`` is given the
        channel is recorded as a **secret channel** so it can be recreated on the
        device after a restart; without a secret it is treated as a **hashtag
        channel** (its key is re-derived from ``#name``) on restore.
        """
        label = (name or "").lstrip("#").strip()
        self._config_channels = [
            c for c in self._config_channels if c["index"] != index
        ]
        if label:
            spec: dict = {"index": index, "name": label}
            if secret_hex:
                spec["secret"] = secret_hex
            self._config_channels.append(spec)

    async def create_channel(
        self, index: int, name: str, secret_hex: str | None = None
    ) -> bool:
        """Create/update a channel on the companion device.

        A **hashtag channel** — a name beginning with ``#`` — needs no secret:
        MeshCore derives the channel key from the hash of the name, so anyone
        who knows ``#name`` can join. Otherwise a 16-byte secret (32 hex chars)
        may be supplied. Returns True when the device accepted the change.
        """
        if not self._running or self._mc is None:
            return False
        try:
            secret = bytes.fromhex(secret_hex) if secret_hex else None
        except ValueError:
            log.warning("[meshcore] channel secret must be hex (32 chars / 16 bytes)")
            return False
        try:
            # Pass the name verbatim (keeping a leading '#') so the firmware's
            # hashtag-channel key derivation kicks in for '#'-prefixed names.
            ev = await self._mc.commands.set_channel(index, name, secret)
        except Exception:  # noqa: BLE001 - a UI action must never crash the app
            log.exception("[meshcore] set_channel failed")
            return False
        ok = ev is not None and getattr(ev, "type", None) is not EventType.ERROR
        if ok:
            label = name.lstrip("#").strip()
            self._device_channels = [
                c for c in self._device_channels if c["index"] != index
            ]
            self._device_channels.append({"index": index, "name": label})
        return ok

    # -- announce + device telemetry ------------------------------------------

    async def send_advert(self, flood: bool = False) -> bool:
        """Broadcast our node advert (the MeshCore equivalent of an announce).

        ``flood=False`` is a zero-hop advert heard only by immediate neighbours;
        ``flood=True`` propagates across the mesh so distant nodes learn of us.
        Returns True when the device accepted the command.
        """
        if not self._running or self._mc is None:
            return False
        try:
            ev = await self._mc.commands.send_advert(flood=flood)
        except Exception:  # noqa: BLE001 - a UI action must never crash the app
            log.exception("[meshcore] advert failed")
            return False
        return ev is not None and getattr(ev, "type", None) is not EventType.ERROR

    async def device_telemetry(self) -> dict:
        """Passive health snapshot of the attached MeshCore companion (no TX).

        Combines the device's self-info (radio frequency/bandwidth/SF/CR, TX
        power, node name, public key) with a live battery reading. Used by the
        Health board, mirroring Reticulum's ``interface_stats`` RNode telemetry.
        Returns ``{}`` when the transport is down; never raises.
        """
        if not self._running or self._mc is None:
            return {}
        info = self._self_info()
        out: dict = {
            "name": info.get("name", ""),
            "public_key": info.get("public_key", ""),
            "radio_freq": info.get("radio_freq"),
            "radio_bw": info.get("radio_bw"),
            "radio_sf": info.get("radio_sf"),
            "radio_cr": info.get("radio_cr"),
            "tx_power": info.get("tx_power"),
            "max_tx_power": info.get("max_tx_power"),
        }
        try:
            ev = await self._mc.commands.get_bat()
            if ev is not None and getattr(ev, "type", None) is not EventType.ERROR:
                payload = getattr(ev, "payload", None) or {}
                out["battery"] = payload.get("level")
        except Exception:  # noqa: BLE001 - telemetry probe must never crash the UI
            log.debug("[meshcore] battery query failed", exc_info=True)
        return out

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if not _HAVE_MESHCORE:
            log.warning(
                "MeshCore transport disabled: install extras "
                "(pip install 'radio-app[meshcore]')."
            )
            self._running = False
            return
        try:
            # NOTE: create_tcp/create_serial are async classmethods that build
            # the connection AND connect (calling connect() internally), then
            # return the live MeshCore instance - or None if the device never
            # responded. They must be awaited; do NOT call connect() again.
            if self._connection == "tcp":
                host = self.config.get("host", "127.0.0.1")
                port = int(self.config.get("tcp_port", 5000))
                self._mc = await MeshCore.create_tcp(host, port)
                where = f"tcp://{host}:{port}"
            else:  # serial (USB)
                dev = self.config.get("port", "/dev/ttyACM0")
                baud = int(self.config.get("baud", 115200))
                self._mc = await MeshCore.create_serial(dev, baud)
                where = f"serial {dev} @ {baud}"
        except Exception as exc:  # noqa: BLE001 - never crash the whole app
            log.warning("MeshCore could not connect (%s).", exc)
            self._running = False
            self._mc = None
            return
        if self._mc is None:
            log.warning("MeshCore connected but the device did not start; disabling.")
            self._running = False
            return

        # Inbound: direct messages, channel messages, and adverts.
        self._subs = [
            self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg),
            self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg),
            self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advert),
        ]
        # Have the library poll the device for queued messages and dispatch them
        # as the events we subscribed to above.
        try:
            self._fetch_sub = await self._mc.start_auto_message_fetching()
        except Exception:  # noqa: BLE001
            log.debug("MeshCore auto message fetching unavailable", exc_info=True)

        # Enumerate the device's configured channels so the Mesh panel can list
        # them as conversations (a failure here just leaves config-only channels).
        try:
            self._device_channels = await self._fetch_channels()
        except Exception:  # noqa: BLE001
            log.debug("MeshCore channel enumeration failed", exc_info=True)

        self._running = True
        # Re-create our saved channels on the device so hashtag/secret channels
        # keep working after a restart even if the companion did not retain their
        # keys (the symptom: "only the public channel works again").
        try:
            await self._restore_config_channels()
        except Exception:  # noqa: BLE001
            log.debug("MeshCore channel restore failed", exc_info=True)

        info = self._self_info()
        log.info(
            "MeshCore up via %s: name=%s pubkey=%s",
            where,
            info.get("name") or "(unnamed)",
            (info.get("public_key") or "")[:12],
        )

    async def stop(self) -> None:
        self._running = False
        self._device_channels = []
        if self._mc is None:
            return
        for sub in self._subs:
            try:
                self._mc.unsubscribe(sub)
            except Exception:  # noqa: BLE001
                pass
        self._subs = []
        if self._fetch_sub is not None:
            try:
                await self._mc.stop_auto_message_fetching()
            except Exception:  # noqa: BLE001
                pass
            self._fetch_sub = None
        await self._safe_disconnect()
        self._mc = None

    async def check_reachable(self) -> ReachabilityStatus:
        """Passively probe the companion endpoint (no transmission).

        Reflects the live connection when up; otherwise TCP confirms the console
        socket accepts a connection and serial confirms the device path exists.
        """
        if self._mc is not None and self._running:
            try:
                if self._mc.is_connected():
                    return ReachabilityStatus.OK
            except Exception:  # noqa: BLE001
                pass
        if self._connection == "tcp":
            host = self.config.get("host", "127.0.0.1")
            port = int(self.config.get("tcp_port", 5000))
            return await probe_tcp(host, port)
        dev = self.config.get("port", "/dev/ttyACM0")
        return (
            ReachabilityStatus.OK if os.path.exists(dev) else ReachabilityStatus.DOWN
        )

    # -- sending --------------------------------------------------------------

    async def send(self, msg: UnifiedMessage) -> bool:
        if not self._running or self._mc is None:
            return False
        try:
            if msg.address_type is AddressType.GROUP:
                chan = self._channel_index(msg.group)
                ev = await self._mc.commands.send_chan_msg(
                    chan, self._channel_wire_text(msg.content)
                )
            else:
                dst = self._resolve_dst(msg.recipient)
                if dst is None:
                    log.warning(
                        "[meshcore] no usable destination for %r", msg.recipient
                    )
                    return False
                ev = await self._mc.commands.send_msg(dst, msg.content)
        except Exception:  # noqa: BLE001 - one bad send must not break the app
            log.exception("[meshcore] send failed")
            return False
        return ev is not None and getattr(ev, "type", None) is not EventType.ERROR

    def _resolve_dst(self, recipient: str | None):
        """Resolve a recipient to a MeshCore destination for ``send_msg``.

        Prefer a known contact (carries the full key + path); fall back to a raw
        hex pubkey/prefix string, which the library accepts directly.
        """
        if not recipient or self._mc is None:
            return None
        ident = recipient.strip()
        try:
            contact = self._mc.get_contact_by_key_prefix(
                ident
            ) or self._mc.get_contact_by_name(ident)
        except Exception:  # noqa: BLE001
            contact = None
        if contact is not None:
            return contact
        if len(ident) >= 12 and all(c in _HEX for c in ident):
            return ident
        return None

    @staticmethod
    def _channel_index(group: str | None) -> int:
        """Map a group tag to a MeshCore channel index (default 0 = public)."""
        if group and group.isdigit():
            return int(group)
        return 0

    # -- receiving (callbacks run on this loop via the meshcore dispatcher) ----

    async def _on_contact_msg(self, event) -> None:
        payload = getattr(event, "payload", None) or {}
        text = payload.get("text", "")
        if not text:
            return
        sender = payload.get("pubkey_prefix") or "unknown"
        await self._emit(
            UnifiedMessage(
                sender=sender,
                content=text,
                address_type=AddressType.DIRECT,
                recipient=self._self_prefix(),
                metadata={
                    "encrypted": True,
                    "snr": payload.get("SNR"),
                    "path_len": payload.get("path_len"),
                },
            )
        )

    async def _on_channel_msg(self, event) -> None:
        payload = getattr(event, "payload", None) or {}
        text = payload.get("text", "")
        if not text:
            return
        chan = payload.get("channel_idx", 0)
        # MeshCore channel messages carry no per-sender identity on the wire
        # (the channel is a shared symmetric key). By convention the sending
        # node prepends its name as "Name: message", so parse that out to show
        # — and make replyable — the actual user rather than a "chanN"
        # placeholder. When no name prefix is present, fall back to an
        # anonymous channel sender that the UI renders non-clickable.
        sender, body, named = self._split_channel_sender(text)
        meta: dict = {"encrypted": True, "channel": chan}
        if named:
            meta["display_name"] = sender
        else:
            sender = f"chan{chan}"
            meta["mc_anon"] = True  # not an addressable identity
        await self._emit(
            UnifiedMessage(
                sender=sender,
                content=body,
                address_type=AddressType.GROUP,
                group=str(chan),
                metadata=meta,
            )
        )

    def _channel_wire_text(self, content: str) -> str:
        """Prepend our node name so channel peers see 'Name: message'.

        MeshCore channels carry no per-sender identity on the wire, so the
        convention is for the sender to embed its name in the text. Doing this
        on send means other nodes (and our own second station) can attribute —
        and reply to — our channel messages instead of seeing an anonymous
        'chanN'. When our node name is unknown the content is sent as-is.
        """
        name = (self._self_info().get("name") or "").strip()
        return f"{name}: {content}" if name else content

    @staticmethod
    def _split_channel_sender(text: str) -> tuple[str, str, bool]:
        """Split a channel message into ``(sender, body, named)``.

        Honors the MeshCore ``"Name: message"`` channel convention: when the
        text begins with a plausible ``name`` followed by ``": "`` the name is
        returned and stripped from the body (``named=True``). Otherwise the
        whole text is the body and ``named`` is ``False``. The separator is a
        colon **followed by a space**, so URLs like ``http://host`` are not
        misread as a sender.
        """
        head, sep, rest = text.partition(": ")
        if sep and head and "\n" not in head and len(head) <= 40:
            return head.strip(), rest, True
        return "", text, False

    async def _on_advert(self, event) -> None:
        """Surface a node advert as 'announce' telemetry (presence, not chat)."""
        payload = getattr(event, "payload", None) or {}
        pubkey = payload.get("public_key") or payload.get("pubkey_prefix") or ""
        if not pubkey:
            return
        name = ""
        try:
            contact = self._mc.get_contact_by_key_prefix(pubkey[:12])
            if contact:
                name = contact.get("adv_name", "")
        except Exception:  # noqa: BLE001
            name = ""
        await self._emit(
            UnifiedMessage(
                sender=pubkey,
                content=f"advert: {name or pubkey[:12]}",
                address_type=AddressType.BROADCAST,
                metadata={
                    "kind": "announce",
                    "display_name": name,
                    "aspect": "peer",
                },
            )
        )

    # -- helpers --------------------------------------------------------------

    async def _restore_config_channels(self) -> None:
        """Re-create saved non-public channels on the device after a connect.

        Companion firmware does not always retain channel keys across an app
        restart, which leaves hashtag/secret channels silently broken (only the
        public channel works). For each saved channel we therefore re-apply its
        key to the device:

        * a ``secret`` channel -> ``set_channel(name, secret)``;
        * otherwise a **hashtag** channel -> ``set_channel("#name", None)`` so the
          firmware re-derives the key from the name.

        A channel explicitly marked ``hashtag = false`` in config is a label for a
        device-managed channel and is left untouched. Re-applying a channel the
        device already holds is idempotent (it derives the same key).
        """
        if self._mc is None:
            return
        for ch in self._config_channels:
            idx = ch.get("index")
            if not isinstance(idx, int) or idx == 0:
                continue
            name = str(ch.get("name", "") or "")
            secret = str(ch.get("secret", "") or "")
            if secret:
                await self.create_channel(idx, name, secret)
            elif ch.get("hashtag", True) and name:
                # Default-True: channels saved by the app's /channel add are
                # hashtag channels, so legacy entries (no flag) restore correctly.
                await self.create_channel(idx, f"#{name.lstrip('#')}", None)

    async def _fetch_channels(self) -> list[dict]:
        """Query the companion for its configured channels (no transmission).

        Probes channel slots ``0..MAX_CHANNELS-1`` via ``get_channel``; a slot is
        kept when the device reports a non-empty name, and the public channel
        (index 0) is always kept so it can be selected on a fresh device.
        """
        found: list[dict] = []
        if self._mc is None:
            return found
        for idx in range(self.MAX_CHANNELS):
            try:
                ev = await self._mc.commands.get_channel(idx)
            except Exception:  # noqa: BLE001 - one bad slot shouldn't abort all
                continue
            if ev is None or getattr(ev, "type", None) is EventType.ERROR:
                continue
            payload = getattr(ev, "payload", None) or {}
            name = str(payload.get("channel_name", "") or "").strip()
            if name or idx == 0:
                found.append({"index": idx, "name": name})
        return found

    def _self_info(self) -> dict:
        if self._mc is None:
            return {}
        try:
            return self._mc.self_info or {}
        except Exception:  # noqa: BLE001
            return {}

    def _self_prefix(self) -> str | None:
        pubkey = self._self_info().get("public_key") or ""
        return pubkey[:12] if pubkey else None

    async def _safe_disconnect(self) -> None:
        if self._mc is None:
            return
        try:
            await self._mc.disconnect()
        except Exception:  # noqa: BLE001
            pass

