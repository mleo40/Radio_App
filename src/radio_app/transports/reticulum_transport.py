"""Reticulum transport (internet / LoRa / serial) using RNS + LXMF.
This is the recommended core transport. It provides an encrypted, store-and-forward
network that already abstracts the physical interface (TCP/internet, RNode/LoRa,
serial). Identity here is an anonymous Reticulum identity hash - the operator's
callsign/grid are never attached on this medium (see core.privacy).
RNS runs its own threads, so inbound LXMF deliveries are marshalled back onto the
app's asyncio loop captured at start().
If ``rns``/``lxmf`` are not installed the transport stays disabled rather than
crashing the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
from datetime import datetime

from .._compat import UTC
from ..core.message import AddressType, UnifiedMessage
from .base import ReachabilityStatus, Transport, TransportCapabilities

log = logging.getLogger(__name__)
try:
    import LXMF  # type: ignore
    import RNS  # type: ignore

    _HAVE_RNS = True
except ModuleNotFoundError:
    RNS = None  # type: ignore
    LXMF = None  # type: ignore
    _HAVE_RNS = False
# Single-packet opportunistic delivery is fine below this size; larger messages
# use a link + resource transfer (DIRECT).
_OPPORTUNISTIC_MAX = 200

# -- shared group / broadcast channels ---------------------------------------
# Groups/broadcast use RNS GROUP destinations: a shared, symmetric-key channel
# whose addressing identity AND encryption key are both derived from the group
# *name* (not a per-node identity), so every node that knows the name lands on
# the same destination hash and can decrypt the same traffic - the same model as
# a MeshCore hashtag channel or a JS8 @GROUP. Delivery rides shared / broadcast
# Reticulum interfaces (LoRa mesh, a local segment); multi-hop transport-routed
# group delivery would need a propagation node (future work).
_GROUP_APP_NAME = "radio_app"
_GROUP_ASPECT = "group"
# AddressType.BROADCAST maps to this reserved well-known channel, so "everyone"
# is simply a group that every node joins on start.
_BROADCAST_GROUP = "broadcast"
# Group messages are single RNS packets; cap the encoded body near the packet
# MDU so we reject (rather than silently truncate) oversize group sends.
_GROUP_PAYLOAD_MAX = 383


@contextlib.contextmanager
def _suppress_signal_registration():
    """Neutralise ``signal.signal`` while RNS initialises.

    ``RNS.Reticulum.__init__`` calls ``signal.signal()`` to install its own
    SIGINT/SIGTERM handlers. That raises "signal only works in main thread of
    the main interpreter" whenever we build RNS off the main thread — which we
    do, since the blocking connect runs via ``asyncio.to_thread`` — and even on
    the main thread we don't want RNS hijacking the host application's signal
    handling. Swapping in a no-op for the duration of construction sidesteps
    both problems; RNS itself never relies on those handlers being installed.
    """
    original = signal.signal
    signal.signal = lambda *args, **kwargs: None  # type: ignore[assignment]
    try:
        yield
    finally:
        signal.signal = original  # type: ignore[assignment]


def group_shared_key(name: str) -> bytes:
    """Deterministic 32-byte symmetric key for a named group channel.

    Everyone who knows the group *name* derives the same key, turning a GROUP
    destination into a shared encrypted channel. Pure/stdlib so it is
    unit-testable without RNS.
    """
    norm = name.strip().lstrip("@").lower()
    return hashlib.sha256(f"radio_app.group:{norm}".encode()).digest()


def encode_group_payload(sender_hex: str, display_name: str, content: str) -> bytes:
    """Pack a group message body (sender hash + display name + text)."""
    return json.dumps(
        {"s": sender_hex or "", "n": display_name or "", "c": content or ""},
        separators=(",", ":"),
    ).encode("utf-8")


def decode_group_payload(data: object) -> dict | None:
    """Parse a GROUP packet body produced by :func:`encode_group_payload`.

    Returns ``{"sender", "name", "content"}`` or ``None`` for anything that is
    not a well-formed group payload.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None
    try:
        obj = json.loads(bytes(data).decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "c" not in obj:
        return None
    return {
        "sender": str(obj.get("s") or ""),
        "name": str(obj.get("n") or ""),
        "content": str(obj.get("c") or ""),
    }



def _display_name_from_app_data(app_data: object, use_lxmf: bool = True) -> str:
    """Best-effort display label from RNS/LXMF announce ``app_data``.

    RNS does not always hand us bytes here: on path responses (and some
    announces) ``app_data`` can be ``True``/``None`` or another non-bytes value.
    Passing those into ``LXMF.display_name_from_app_data`` or ``.decode`` raises
    (e.g. "'bool' object has no attribute 'decode'") and spams the log, so we
    only attempt decoding when we actually have bytes.
    """
    if not isinstance(app_data, (bytes, bytearray)):
        return ""
    if use_lxmf and LXMF is not None:
        try:
            label = LXMF.display_name_from_app_data(app_data) or ""
        except Exception:  # noqa: BLE001
            label = ""
        if label:
            return label
    try:
        return bytes(app_data).decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return ""


class ReticulumTransport(Transport):
    name = "reticulum"
    surface = "chat"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._reticulum = None
        self._lxmf = None
        self._identity = None
        self._local_destination = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Discovered NomadNet nodes ("sites") keyed by dest_hex, built from
        # nomadnetwork.node announces for the read-only page browser.
        self._nodes: dict[str, dict] = {}
        # Discovered LXMF peers (messageable identities) keyed by dest_hex, from
        # lxmf.delivery announces. Lets us distinguish peers from sites.
        self._peers: dict[str, dict] = {}
        # Outbound LXMessages we're awaiting delivery confirmation for, keyed by
        # id(lxm) -> the originating UnifiedMessage's (msg_id, recipient). Lets
        # the delivery/failure callbacks emit a 'delivery' telemetry event the UI
        # can use to annotate the sent message.
        self._sent_refs: dict[int, dict] = {}
        # Named group channels to join at start (pushed in via set_identity), plus
        # the live IN/OUT GROUP destinations keyed by normalised group name.
        self._group_names: tuple[str, ...] = ()
        self._groups_in: dict[str, object] = {}
        self._groups_out: dict[str, object] = {}
        # We only ever ATTACH to an external rnsd shared instance — we never start
        # our own. If rnsd isn't up yet we keep retrying in the background so the
        # transport comes online automatically once rnsd appears.
        self._configdir: str | None = None
        self._stopping = False
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_interval = float(
            (config or {}).get("reconnect_interval", 10.0)
        )
        self._connect_setup_done = False

    def set_identity(self, callsign: str = "", groups: tuple[str, ...] = ()) -> None:
        """Learn which named groups to join.

        The callsign is intentionally ignored — Reticulum is anonymous and never
        carries operator identity — but the app pushes the configured group names
        here (the same call it makes to HF transports), and we join each as a
        shared GROUP channel at :meth:`start`.
        """
        if groups:
            self._group_names = tuple(
                dict.fromkeys(g.strip().lstrip("@").lower() for g in groups if g)
            )

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=1_000_000,  # LXMF handles large/chunked payloads
            supports_broadcast=True,  # shared GROUP broadcast channel
            supports_addressing=True,
            supports_groups=True,  # shared-key GROUP destinations
            supports_encryption=True,  # E2E by default
            supports_delivery_confirmation=True,
            supports_attachments=True,  # LXMF file fields (DIRECT/Resource)
            is_realtime=True,  # RNS Links enable live chat
            typical_latency_s=1.0,
            needs_internet=False,  # also runs over LoRa / serial
            address_scheme="rns_hash",
            carries_operator_identity=False,  # anonymous: no callsign/grid leaked
            prohibits_encryption=False,  # E2E encryption is the norm here
        )

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        if not _HAVE_RNS:
            log.warning(
                "Reticulum transport disabled: install extras "
                "(pip install 'radio-app[reticulum]')."
            )
            self._running = False
            return
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        raw_configdir = self.config.get("config_path") or None
        # RNS does NOT expand "~" itself: a literal "~/.reticulum" becomes a
        # NEW directory under the cwd, which won't match where rnsd is running
        # and breaks shared-instance attachment. Expand env vars + "~" here.
        configdir: str | None = None
        if raw_configdir:
            configdir = os.path.expanduser(os.path.expandvars(str(raw_configdir)))
            if configdir != raw_configdir:
                log.info("expanded config_path %r -> %r", raw_configdir, configdir)
        self._configdir = configdir
        # Try once now; if rnsd isn't up yet, keep retrying in the background so
        # the transport attaches automatically the moment rnsd comes online.
        if not await asyncio.to_thread(self._attempt_connect):
            log.info(
                "Reticulum: rnsd not reachable yet; retrying every %.0fs in the "
                "background. The transport will come online once rnsd appears.",
                self._reconnect_interval,
            )
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Start (or keep) the background task that retries attaching to rnsd."""
        if self._stopping:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        try:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        except RuntimeError:
            # No running loop (e.g. a synchronous unit test): skip retries.
            self._reconnect_task = None

    async def _reconnect_loop(self) -> None:
        while not self._stopping and not self._running:
            await asyncio.sleep(self._reconnect_interval)
            if self._stopping or self._running:
                break
            if await asyncio.to_thread(self._attempt_connect):
                log.info("Reticulum: attached to rnsd after retry; transport up.")
                break

    def _release_squatted_listener(self) -> None:
        """Close any shared-instance listener RNS left bound on a failed attempt.

        When our process starts *before* rnsd, ``RNS.Reticulum.__init__`` (with
        ``require_shared_instance=True``) binds the local shared-instance socket
        as a side effect of probing, decides it should abort, and calls
        ``interface.detach()`` — but ``Interface.detach`` is a no-op for the
        ``LocalServerInterface``, so the bound listening socket is **leaked** into
        our process. We then squat on the shared-instance socket for the whole
        retry interval, and when rnsd finally starts it connects to *us* instead
        of binding ("connected to another shared local instance, this is probably
        NOT what you want!"). Closing the leaked listener here means we only ever
        hold the socket for the microseconds inside one attempt, never across the
        retry interval, so rnsd can bind normally once it appears.
        """
        if not _HAVE_RNS:
            return
        try:
            RNS.Transport.detach_interfaces()
        except Exception:  # noqa: BLE001 - best-effort cleanup, never fatal
            pass

    def _reset_rns_singleton(self, force: bool = False) -> None:
        """Clear RNS's leaked process-global ``Reticulum`` singleton + listeners.

        ``RNS.Reticulum.__init__`` assigns the singleton (``Reticulum.__instance``)
        *before* it tries to attach to the shared instance, then raises
        ``SystemError`` when ``require_shared_instance=True`` and rnsd isn't up.
        RNS never clears that reference (not even in ``exit_handler``), so a second
        ``RNS.Reticulum(...)`` raises ``OSError("Attempt to reinitialise...")`` —
        which would permanently wedge our reconnect loop after the first failed
        probe. We clear it ourselves so the next retry can build a fresh instance,
        and release any shared-instance listener socket RNS leaked along the way.

        ``force`` clears the singleton even when it *is* a live, fully-attached
        instance — used by :meth:`stop` to deliberately detach so a later
        :meth:`start` can reattach (the default keeps the safety guard for the
        retry path, where the instance is never live).
        """
        if not _HAVE_RNS:
            return
        # First close any leaked shared-instance listener so we don't squat on
        # the socket that rnsd needs to bind.
        self._release_squatted_listener()
        ret_cls = getattr(RNS, "Reticulum", None)
        if ret_cls is None:
            return
        # Only clear the singleton if it isn't a live, fully-attached instance
        # (it never is on a failed attempt, but guard anyway) — unless forced.
        try:
            current = getattr(ret_cls, "_Reticulum__instance", None)
            if current is not None and (
                force
                or not getattr(current, "is_connected_to_shared_instance", False)
            ):
                ret_cls._Reticulum__instance = None
        except Exception:  # noqa: BLE001
            pass
        # Re-arm the exit-handler guards so a future genuine shutdown still runs.
        for flag in (
            "_Reticulum__exit_handler_ran",
            "_Reticulum__interface_detach_ran",
        ):
            try:
                setattr(ret_cls, flag, False)
            except Exception:  # noqa: BLE001
                pass

        # Only clear the singleton if it isn't a live, fully-attached instance
        # (it never is on a failed attempt, but guard anyway).
        try:
            current = getattr(ret_cls, "_Reticulum__instance", None)
            if current is not None and not getattr(
                current, "is_connected_to_shared_instance", False
            ):
                ret_cls._Reticulum__instance = None
        except Exception:  # noqa: BLE001
            pass
        # Re-arm the exit-handler guards so a future genuine shutdown still runs.
        for flag in (
            "_Reticulum__exit_handler_ran",
            "_Reticulum__interface_detach_ran",
        ):
            try:
                setattr(ret_cls, flag, False)
            except Exception:  # noqa: BLE001
                pass

    def _attempt_connect(self) -> bool:
        """Attach to an external rnsd shared instance. Never starts our own.

        Returns True once attached and fully set up, False if rnsd isn't
        reachable yet (caller keeps retrying) or setup failed. Synchronous (RNS
        calls block); run it via ``asyncio.to_thread``.
        """
        # require_shared_instance=True makes RNS refuse to spin up its own
        # instance: it attaches to a running rnsd or raises. We NEVER want this
        # process to own the hardware / become the shared instance itself.
        try:
            with _suppress_signal_registration():
                self._reticulum = RNS.Reticulum(
                    configdir=self._configdir,
                    require_shared_instance=True,
                )
        except SystemError as exc:
            # rnsd isn't running (or not sharing this config dir) yet. RNS has
            # already stashed a half-built singleton; clear it so the next retry
            # can construct a fresh one instead of hitting "already running".
            log.debug("Reticulum: shared instance not available yet: %s", exc)
            self._reticulum = None
            self._reset_rns_singleton()
            return False
        except Exception as exc:  # noqa: BLE001 - never crash the whole app
            log.warning("Reticulum connect attempt failed: %s", exc)
            self._reticulum = None
            self._reset_rns_singleton()
            return False

        # Belt-and-suspenders: if somehow we didn't attach to a shared instance,
        # refuse to run as a standalone instance and retry instead.
        is_connected_shared = bool(
            getattr(self._reticulum, "is_connected_to_shared_instance", False)
        )
        if not is_connected_shared:
            log.debug(
                "Reticulum: not attached to a shared instance; will keep trying."
            )
            # Tear down the unwanted standalone instance safely. Do NOT call
            # RNS.exit() (os._exit(0) would kill the whole app); clearing the
            # singleton + detaching interfaces fully releases it.
            self._reticulum = None
            self._reset_rns_singleton(force=True)
            return False
        log.info("Reticulum: connected to shared instance (rnsd).")

        # One-time setup (identity, LXMF, handlers, group joins) the first time we
        # successfully attach. Guarded so a reconnect never double-registers.
        # LXMF.LXMRouter also installs its own signal handlers during init, which
        # fails off the main thread (we're in asyncio.to_thread here), so this
        # runs under the same signal-suppression guard as the RNS construction.
        if not self._connect_setup_done:
            with _suppress_signal_registration():
                self._setup_after_connect()
            self._connect_setup_done = True

        self._running = True
        log.info(
            "Reticulum transport up. Local LXMF address: %s",
            RNS.prettyhexrep(self._local_destination.hash),
        )
        return True

    def _setup_after_connect(self) -> None:
        """One-time identity / LXMF / announce-handler / group setup."""
        # Persistent anonymous identity stored under the RNS storage path.
        storage = self._storage_dir()
        os.makedirs(storage, exist_ok=True)
        id_path = os.path.join(storage, "identity")
        if os.path.isfile(id_path):
            self._identity = RNS.Identity.from_file(id_path)
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(id_path)
        # LXMF router + our delivery destination. display_name is PUBLIC, so it
        # defaults to anonymous (never the callsign) to preserve privacy.
        self._lxmf = LXMF.LXMRouter(
            identity=self._identity,
            storagepath=os.path.join(storage, "lxmf"),
        )
        display_name = self.config.get("display_name") or None
        self._local_destination = self._lxmf.register_delivery_identity(
            self._identity, display_name=display_name
        )
        self._lxmf.register_delivery_callback(self._lxmf_delivery)
        if self.config.get("announce_on_start", True):
            self._lxmf.announce(self._local_destination.hash)

        # Aspect-specific classifiers, registered FIRST (record-only) so the
        # generic monitor handler below can label each announce peer/site/other.
        self._peer_handler = _PeerAnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._peer_handler)
        self._node_handler = _NodeAnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._node_handler)

        # Surface inbound RNS announces in the Monitor (off by default).
        if self.config.get("announce_monitor", True):
            self._announce_handler = _AnnounceHandler(self)
            RNS.Transport.register_announce_handler(self._announce_handler)

        # Join shared group channels (incl. the reserved broadcast channel) so we
        # receive group/broadcast traffic. GROUP destination hashes are derived
        # from the channel name, so no announce/path discovery is needed.
        self._join_group(_BROADCAST_GROUP)
        for name in self._group_names:
            self._join_group(name)

    async def stop(self) -> None:
        self._running = False
        self._stopping = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if getattr(self, "_announce_handler", None) is not None and _HAVE_RNS:
            try:
                RNS.Transport.deregister_announce_handler(self._announce_handler)
            except Exception:  # noqa: BLE001
                pass
            self._announce_handler = None
        if getattr(self, "_node_handler", None) is not None and _HAVE_RNS:
            try:
                RNS.Transport.deregister_announce_handler(self._node_handler)
            except Exception:  # noqa: BLE001
                pass
            self._node_handler = None
        if getattr(self, "_peer_handler", None) is not None and _HAVE_RNS:
            try:
                RNS.Transport.deregister_announce_handler(self._peer_handler)
            except Exception:  # noqa: BLE001
                pass
            self._peer_handler = None
        try:
            if self._lxmf is not None:
                self._lxmf.exit_handler()
        except Exception:  # noqa: BLE001
            pass
        # Detach our client side from the shared rnsd instance. We deliberately do
        # NOT call RNS.exit(): it ends in os._exit(0), which would hard-kill the
        # whole process — skipping every other transport's stop() and any final
        # flush — and is uncatchable (os._exit doesn't raise). RNS's own worker
        # threads are daemonised, so the interpreter still exits cleanly without
        # it. Releasing our interface + clearing RNS's process-global singleton
        # also lets a later start() reattach instead of hitting "already running".
        if _HAVE_RNS:
            self._reset_rns_singleton(force=True)
        self._reticulum = None

    # -- sending --------------------------------------------------------------
    async def send(self, msg: UnifiedMessage) -> bool:
        if not self._running or self._lxmf is None:
            return False
        # Group / broadcast go to a shared GROUP channel (broadcast = a reserved
        # well-known group). Direct messages use the LXMF SINGLE path below.
        if msg.address_type is AddressType.GROUP:
            return self._send_group(msg.group or "", msg)
        if msg.address_type is AddressType.BROADCAST:
            return self._send_group(_BROADCAST_GROUP, msg)
        if msg.address_type is not AddressType.DIRECT or not msg.recipient:
            log.info("[reticulum] unsupported address type for send")
            return False
        try:
            dest_hash = bytes.fromhex(msg.recipient)
        except ValueError:
            log.warning("[reticulum] recipient is not a valid hash: %s", msg.recipient)
            return False
        # Ensure we know a path/identity for the destination.
        if not RNS.Transport.has_path(dest_hash):
            RNS.Transport.request_path(dest_hash)
            log.info("[reticulum] no path yet to %s; requested.", msg.recipient)
            return False
        recipient_identity = RNS.Identity.recall(dest_hash)
        if recipient_identity is None:
            log.info("[reticulum] identity not yet known for %s", msg.recipient)
            return False
        dest = RNS.Destination(
            recipient_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        method = (
            LXMF.LXMessage.OPPORTUNISTIC
            if msg.size <= _OPPORTUNISTIC_MAX
            else LXMF.LXMessage.DIRECT
        )
        lxm = LXMF.LXMessage(
            dest,
            self._local_destination,
            msg.content,
            title="",
            desired_method=method,
        )
        # Attach any queued files (DIRECT/Resource carries large payloads). LXMF
        # file fields are [[name, bytes], ...]; record names for UI display.
        files = self._read_attachments(msg.attach_paths)
        if files:
            lxm.desired_method = LXMF.LXMessage.DIRECT
            lxm.fields[LXMF.FIELD_FILE_ATTACHMENTS] = [
                [name, data] for name, data in files
            ]
            msg.metadata["attachments"] = [name for name, _ in files]
        # Remember which UnifiedMessage this LXMessage corresponds to so the
        # delivery/failure callbacks can report status back to the UI.
        self._sent_refs[id(lxm)] = {
            "msg_id": msg.msg_id,
            "recipient": msg.recipient,
        }
        lxm.register_delivery_callback(self._on_delivered)
        lxm.register_failed_callback(self._on_failed)
        self._lxmf.handle_outbound(lxm)
        return True

    # -- group / broadcast ----------------------------------------------------
    def _send_group(self, name: str, msg: UnifiedMessage) -> bool:
        """Send a message to a shared GROUP channel (single encrypted packet)."""
        norm = name.strip().lstrip("@").lower()
        if not norm:
            log.warning("[reticulum] group send with no channel name")
            return False
        our_hex = (
            self._local_destination.hash.hex()
            if self._local_destination is not None
            else ""
        )
        payload = encode_group_payload(
            our_hex, self.local_display_name(), msg.content
        )
        if len(payload) > _GROUP_PAYLOAD_MAX:
            log.warning(
                "[reticulum] group message too large (%d > %d bytes); not sent.",
                len(payload),
                _GROUP_PAYLOAD_MAX,
            )
            return False
        return self._transmit_group(norm, payload)

    def _transmit_group(self, norm: str, payload: bytes) -> bool:
        """Transmit an encoded group payload on its GROUP destination.

        Split out from :meth:`_send_group` so the encode/size logic is testable
        without RNS. Returns True once the packet was handed to RNS.
        """
        out = self._group_out_destination(norm)
        if out is None:
            return False
        try:
            RNS.Packet(out, payload).send()
            log.info("[reticulum] sent group message on #%s", norm)
            return True
        except Exception:  # noqa: BLE001 - never let one send crash the app
            log.exception("[reticulum] group send failed on #%s", norm)
            return False

    def _join_group(self, name: str) -> object | None:
        """Create + cache the IN GROUP destination for ``name`` and listen on it.

        Idempotent. Returns the IN destination (or ``None`` when RNS is down /
        creation failed). The packet callback is bound to the channel name so
        inbound packets are attributed to the right group.
        """
        norm = name.strip().lstrip("@").lower()
        if not norm or not _HAVE_RNS:
            return None
        existing = self._groups_in.get(norm)
        if existing is not None:
            return existing
        try:
            dest = self._make_group_destination(norm, RNS.Destination.IN)
            dest.set_packet_callback(
                lambda data, packet, _n=norm: self._group_packet_received(
                    _n, data, packet
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("[reticulum] could not join group #%s", norm)
            return None
        self._groups_in[norm] = dest
        log.info(
            "[reticulum] joined group channel #%s <%s>", norm, dest.hash.hex()[:12]
        )
        return dest

    def _group_out_destination(self, norm: str) -> object | None:
        """Create + cache the OUT GROUP destination for a (normalised) name."""
        if not _HAVE_RNS:
            return None
        cached = self._groups_out.get(norm)
        if cached is not None:
            return cached
        try:
            out = self._make_group_destination(norm, RNS.Destination.OUT)
        except Exception:  # noqa: BLE001
            log.exception("[reticulum] could not open group #%s for sending", norm)
            return None
        self._groups_out[norm] = out
        # Sending to an ad-hoc group also subscribes us so we hear the replies.
        self._join_group(norm)
        return out

    def _make_group_destination(self, norm: str, direction):
        """Build a shared GROUP destination for a channel name.

        Both the addressing identity and the symmetric encryption key are derived
        deterministically from the name, so every node that knows the name lands
        on the same destination hash and can decrypt the channel.
        """
        ident = RNS.Identity(create_keys=False)
        ident.load_private_key(
            hashlib.sha512(f"radio_app.gid:{norm}".encode()).digest()
        )
        dest = RNS.Destination(
            ident,
            direction,
            RNS.Destination.GROUP,
            _GROUP_APP_NAME,
            _GROUP_ASPECT,
            norm,
        )
        dest.load_private_key(group_shared_key(norm))
        return dest

    def _group_packet_received(self, group_name: str, data: object, packet) -> None:
        """Handle an inbound GROUP packet (called from an RNS thread)."""
        parsed = decode_group_payload(data)
        if parsed is None:
            return
        our_hex = (
            self._local_destination.hash.hex()
            if self._local_destination is not None
            else None
        )
        sender = parsed["sender"]
        # Ignore our own transmissions echoed back on a shared medium.
        if our_hex and sender and sender.lower() == our_hex.lower():
            return
        is_broadcast = group_name == _BROADCAST_GROUP
        msg = UnifiedMessage(
            sender=sender or "unknown",
            content=parsed["content"],
            address_type=(
                AddressType.BROADCAST if is_broadcast else AddressType.GROUP
            ),
            group=None if is_broadcast else group_name,
            transport=self.name,
            metadata={
                "encrypted": True,
                "rns_source": sender,
                "display_name": parsed["name"],
            },
        )
        self._dispatch_to_loop(msg)

    # -- receiving (called from an RNS thread) --------------------------------
    def _lxmf_delivery(self, lxm) -> None:
        try:
            content = lxm.content_as_string()
        except Exception:  # noqa: BLE001
            content = (lxm.content or b"").decode("utf-8", errors="replace")
        source_hex = (
            lxm.source_hash.hex() if getattr(lxm, "source_hash", None) else "unknown"
        )
        our_hex = (
            self._local_destination.hash.hex()
            if self._local_destination is not None
            else None
        )
        metadata: dict = {"encrypted": True, "rns_source": source_hex}
        # Save any inbound file attachments to disk and record their names +
        # saved paths so the UI can show (and the operator can open) them.
        names, saved = self._save_inbound_attachments(lxm, source_hex)
        if names:
            metadata["attachments"] = names
            metadata["attachments_saved"] = saved
        msg = UnifiedMessage(
            sender=source_hex,
            content=content or "",
            address_type=AddressType.DIRECT,
            recipient=our_hex,
            transport=self.name,
            metadata=metadata,
        )
        self._dispatch_to_loop(msg)

    # -- attachments ----------------------------------------------------------

    def _read_attachments(self, paths: list[str]) -> list[tuple[str, bytes]]:
        """Read outbound attachment file paths into ``(name, bytes)`` pairs.

        Unreadable paths are skipped with a warning (never abort a send over one
        bad path). Names are reduced to their base name for the wire.
        """
        out: list[tuple[str, bytes]] = []
        for p in paths:
            try:
                with open(p, "rb") as fh:
                    out.append((os.path.basename(str(p)), fh.read()))
            except OSError as exc:
                log.warning("[reticulum] cannot read attachment %s: %s", p, exc)
        return out

    def set_download_dir(self, path: str) -> None:
        """Set the central download directory (where all modes save files).

        Called by the app from ``[storage].download_dir``. A per-transport
        ``attachments_dir`` override (if set) still wins; otherwise inbound
        attachments are saved here so downloads from every mode land together.
        """
        self._central_download_dir = str(path or "").strip()

    def attachments_dir(self) -> str:
        """Directory where received attachments are saved.

        Resolution order: a per-transport ``attachments_dir`` override, then the
        central ``[storage].download_dir`` (shared by all modes), then the XDG
        data default.
        """
        configured = str(self.config.get("attachments_dir", "")).strip()
        if configured:
            return os.path.expanduser(os.path.expandvars(configured))
        central = str(getattr(self, "_central_download_dir", "") or "").strip()
        if central:
            return os.path.expanduser(os.path.expandvars(central))
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
        return os.path.join(base, "radio_app", "downloads")

    def _save_inbound_attachments(
        self, lxm, source_hex: str
    ) -> tuple[list[str], list[str]]:
        """Persist LXMF file attachments to disk. Returns (names, saved_paths).

        Filenames are sanitised to their base name (no path traversal) and made
        unique on collision. Returns empty lists when the message carries none.
        """
        fields = getattr(lxm, "fields", None) or {}
        items = fields.get(LXMF.FIELD_FILE_ATTACHMENTS) if LXMF else None
        if not items:
            return [], []
        dest_dir = self.attachments_dir()
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            log.warning("[reticulum] cannot create %s: %s", dest_dir, exc)
            return [], []
        names: list[str] = []
        saved: list[str] = []
        for item in items:
            try:
                raw_name, data = item[0], item[1]
            except (TypeError, IndexError):
                continue
            name = os.path.basename(str(raw_name or "attachment")) or "attachment"
            path = self._unique_path(dest_dir, name)
            try:
                with open(path, "wb") as fh:
                    fh.write(bytes(data))
            except (OSError, TypeError) as exc:
                log.warning("[reticulum] cannot save attachment %s: %s", name, exc)
                continue
            names.append(name)
            saved.append(path)
        if saved:
            log.info(
                "[reticulum] saved %d attachment(s) from %s to %s",
                len(saved), source_hex, dest_dir,
            )
        return names, saved

    @staticmethod
    def _unique_path(dest_dir: str, name: str) -> str:
        """A non-colliding path in ``dest_dir`` for ``name`` (adds -1, -2, ...)."""
        path = os.path.join(dest_dir, name)
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(os.path.join(dest_dir, f"{stem}-{i}{ext}")):
            i += 1
        return os.path.join(dest_dir, f"{stem}-{i}{ext}")

    def _on_delivered(self, lxm) -> None:
        log.info("[reticulum] message delivered")
        self._emit_delivery(lxm, "delivered")

    def _on_failed(self, lxm) -> None:
        log.warning("[reticulum] delivery failed for a message")
        self._emit_delivery(lxm, "failed")

    def _emit_delivery(self, lxm, status: str) -> None:
        """Surface an outbound delivery result as a 'delivery' telemetry event.

        The router treats ``kind='delivery'`` as telemetry (no storage/filtering)
        and forwards it to the UI, which uses ``ref_msg_id`` to annotate the
        previously-sent message with a delivered/failed indicator.
        """
        ref = self._sent_refs.pop(id(lxm), None)
        if ref is None:
            return
        msg = UnifiedMessage(
            sender=self.name,
            content=f"delivery {status}",
            address_type=AddressType.BROADCAST,
            transport=self.name,
            metadata={
                "kind": "delivery",
                "status": status,
                "ref_msg_id": ref.get("msg_id"),
                "recipient": ref.get("recipient"),
            },
        )
        self._dispatch_to_loop(msg)

    # -- introspection --------------------------------------------------------
    def local_identity(self) -> str | None:
        if self._local_destination is not None:
            return self._local_destination.hash.hex()
        return "rns:anonymous"

    def local_display_name(self) -> str:
        """The public LXMF display name we announce (blank = fully anonymous)."""
        return str(self.config.get("display_name") or "")

    def announce_now(self) -> bool:
        """Re-announce our LXMF identity on demand. Returns True if announced.

        Useful after attaching a new interface or to make ourselves promptly
        reachable to a specific peer without restarting the app.
        """
        if not self._running or self._lxmf is None or self._local_destination is None:
            return False
        try:
            self._lxmf.announce(self._local_destination.hash)
            log.info("[reticulum] announced local identity on demand")
            return True
        except Exception:  # noqa: BLE001 - never let a UI action crash the app
            log.exception("[reticulum] on-demand announce failed")
            return False

    def has_path(self, dest_hex: str) -> bool:
        """True when RNS already knows a path to ``dest_hex`` (a 32-hex hash)."""
        if not self._running or not _HAVE_RNS:
            return False
        try:
            return bool(RNS.Transport.has_path(bytes.fromhex(dest_hex)))
        except ValueError:
            return False

    def request_path(self, dest_hex: str) -> bool:
        """Ask the network for a path to a known contact and cache it.

        RNS stores the resolved path itself, so a subsequent send to this
        contact can proceed. Returns True if a request was issued (or a path is
        already known), False if the hash was invalid / RNS is down.
        """
        if not self._running or not _HAVE_RNS:
            return False
        try:
            dest_hash = bytes.fromhex(dest_hex)
        except ValueError:
            return False
        if RNS.Transport.has_path(dest_hash):
            return True
        try:
            RNS.Transport.request_path(dest_hash)
            log.info("[reticulum] requested path to %s", dest_hex[:12])
            return True
        except Exception:  # noqa: BLE001
            log.exception("[reticulum] path request failed")
            return False

    async def check_reachable(self) -> ReachabilityStatus:
        """Reachable iff RNS is up AND we can actually hear the network.

        That means either attached to a shared instance (rnsd) or owning at
        least one interface. A running RNS with zero interfaces and no shared
        instance can send/receive nothing, so it reports DOWN.
        """
        if not self._running or not _HAVE_RNS or self._reticulum is None:
            return ReachabilityStatus.DOWN
        try:
            connected_shared = bool(
                getattr(self._reticulum, "is_connected_to_shared_instance", False)
            )
            iface_count = len(getattr(RNS.Transport, "interfaces", []) or [])
        except Exception:  # noqa: BLE001
            return ReachabilityStatus.DOWN
        if connected_shared or iface_count > 0:
            return ReachabilityStatus.OK
        return ReachabilityStatus.DOWN

    def known_nodes(self) -> list[dict]:
        """Discovered NomadNet nodes, most recently heard first."""
        nodes = [
            {
                "dest": dest,
                "name": info.get("name", ""),
                "last_seen": info.get("last_seen"),
            }
            for dest, info in self._nodes.items()
        ]
        _floor = datetime.min.replace(tzinfo=UTC)
        nodes.sort(key=lambda n: n["last_seen"] or _floor, reverse=True)
        return nodes

    def known_peers(self) -> list[dict]:
        """Discovered LXMF peers (messageable identities), recently heard first."""
        peers = [
            {
                "dest": dest,
                "name": info.get("name", ""),
                "last_seen": info.get("last_seen"),
            }
            for dest, info in self._peers.items()
        ]
        _floor = datetime.min.replace(tzinfo=UTC)
        peers.sort(key=lambda p: p["last_seen"] or _floor, reverse=True)
        return peers

    def classify_dest(self, dest_hex: str) -> str:
        """Best-effort type of a destination hash from heard announce aspects.

        Returns ``"node"`` for a NomadNet site (``nomadnetwork.node`` announce),
        ``"peer"`` for an LXMF messageable identity (``lxmf.delivery`` announce),
        or ``""`` when we have never heard the destination announce (a bare hash
        carries no aspect, so it is genuinely ambiguous until heard).

        Matching is prefix-tolerant in either direction because hashes are
        sometimes surfaced/stored short (12 chars) and sometimes full (32).
        """
        if not dest_hex:
            return ""
        d = dest_hex.strip().lower()
        for dest in self._nodes:
            if dest.startswith(d) or d.startswith(dest):
                return "node"
        for dest in self._peers:
            if dest.startswith(d) or d.startswith(dest):
                return "peer"
        return ""

    def interface_stats(self) -> dict | None:
        """Snapshot of RNS interfaces (incl. RNode telemetry from rnsd).

        Wraps :meth:`RNS.Reticulum.get_interface_stats`, the same RPC the
        ``rnstatus`` tool uses. When attached to a shared instance (rnsd),
        this travels over the local control socket and returns *rnsd's* view
        of its interfaces - so RNode RSSI/SNR/battery/frequency are visible
        even though those interfaces are owned by rnsd, not by this process.
        When running standalone the values come from our own RNS.Transport.

        Returns the raw dict (``{"interfaces": [...], "rxb": ..., "txb": ...,
        "rxs": ..., "txs": ..., "transport_uptime": ..., ...}``) or ``None``
        when RNS is unavailable / the call fails. Never raises.
        """
        if not self._running or not _HAVE_RNS or self._reticulum is None:
            return None
        try:
            return self._reticulum.get_interface_stats()
        except Exception:  # noqa: BLE001 - never let a telemetry probe crash the UI
            log.debug("get_interface_stats failed", exc_info=True)
            return None

    def is_reachable(self, msg: UnifiedMessage) -> bool:
        if not self._running or not _HAVE_RNS:
            return False
        # Shared group/broadcast channels are always sendable when RNS is up
        # (delivery is broadcast on the medium; no per-recipient path needed).
        if msg.address_type in (AddressType.GROUP, AddressType.BROADCAST):
            return True
        if msg.address_type is not AddressType.DIRECT or not msg.recipient:
            return False
        try:
            return RNS.Transport.has_path(bytes.fromhex(msg.recipient))
        except ValueError:
            return False

    # -- helpers --------------------------------------------------------------
    def _storage_dir(self) -> str:
        base = getattr(RNS.Reticulum, "storagepath", None) or os.path.expanduser(
            "~/.reticulum/storage"
        )
        return os.path.join(base, "radio_app")

    def _dispatch_to_loop(self, msg: UnifiedMessage) -> None:
        if self._loop is None:
            log.debug(
                "[reticulum] no loop captured yet; dropping inbound %s", msg.msg_id
            )
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._emit(msg), self._loop)
        except RuntimeError:
            log.exception("could not marshal inbound message onto the loop")
            return

        def _log_failure(f) -> None:
            exc = f.exception()
            if exc is not None:
                log.error("[reticulum] inbound dispatch failed: %r", exc)

        fut.add_done_callback(_log_failure)


# -- managed RNode interface -------------------------------------------------
_RNODE_TEMPLATE = """\
[reticulum]
  enable_transport = False
  share_instance = Yes
[logging]
  loglevel = 4
[interfaces]
  [[RNode LoRa Interface]]
    type = RNodeInterface
    interface_enabled = True
    port = {port}
    frequency = {frequency}
    bandwidth = {bandwidth}
    spreadingfactor = {spreadingfactor}
    codingrate = {codingrate}
    txpower = {txpower}
"""
_RNODE_DEFAULTS = {
    "port": "/dev/ttyUSB0",
    "frequency": 915000000,
    "bandwidth": 125000,
    "spreadingfactor": 8,
    "codingrate": 5,
    "txpower": 7,
}


def ensure_rnode_interface(configdir: str | None, rnode: dict) -> str | None:
    """Write a Reticulum config with an RNode interface if none exists yet.
    Never clobbers an existing config (the user may have hand-tuned it). Returns
    the path written, or None if a config already existed.
    """
    cfgdir = configdir or os.path.expanduser("~/.reticulum")
    os.makedirs(cfgdir, exist_ok=True)
    cfgpath = os.path.join(cfgdir, "config")
    if os.path.isfile(cfgpath):
        log.info("Reticulum config already present at %s; leaving it alone.", cfgpath)
        return None
    params = {
        **_RNODE_DEFAULTS,
        **{k: v for k, v in rnode.items() if v not in (None, "")},
    }
    with open(cfgpath, "w") as fh:
        fh.write(_RNODE_TEMPLATE.format(**params))
    log.info("Wrote managed RNode interface to %s", cfgpath)
    return cfgpath


class _AnnounceHandler:
    """Surfaces RNS announces (peer presence beacons) into the Monitor.

    RNS announces are how identities advertise themselves on the network; seeing
    them is essential for monitoring "who's on the air" over LoRa or the wider
    Reticulum mesh. We emit them as ``UnifiedMessage`` events tagged with
    ``metadata['kind'] = 'announce'`` so the router skips persistence / filtering
    (they're not conversations), but the UI's Monitor still shows them live.
    """

    # None = receive announces for ALL aspects (lxmf, nomadnet, ...).
    aspect_filter = None
    # Also tell us when paths are returned via path responses, not just first
    # announces (helps when bootstrapping a path to a peer).
    receive_path_responses = True

    def __init__(self, transport: ReticulumTransport) -> None:
        self._transport = transport

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity,
        app_data,
        *_extra,
        **_kwargs,  # RNS passes announce_packet_hash= and may add more kwargs
    ) -> None:
        # Best-effort decode of the (optional) app_data into a display label.
        label = _display_name_from_app_data(app_data)
        dest_hex = destination_hash.hex()
        short = dest_hex[:12]
        # Classify against the aspect-specific registries (populated by the
        # peer/node handlers that run before this one).
        if dest_hex in self._transport._nodes:
            aspect = "site"
        elif dest_hex in self._transport._peers:
            aspect = "peer"
        else:
            aspect = "other"
        log.info(
            "[reticulum] announce received from %s (%s) [%s]",
            short,
            label or "(no name)",
            aspect,
        )
        content = (
            f"announce: {label or '(no name)'}  <{short}>"
            if label
            else f"announce: <{short}>"
        )
        msg = UnifiedMessage(
            sender=dest_hex,
            content=content,
            address_type=AddressType.BROADCAST,
            transport=self._transport.name,
            metadata={
                "kind": "announce",
                "aspect": aspect,
                "rns_dest": dest_hex,
                "display_name": label,
            },
        )
        # Marshal back onto the asyncio loop the transport is running on.
        self._transport._dispatch_to_loop(msg)


class _PeerAnnounceHandler:
    """Records LXMF peer announces (messageable identities), aspect-filtered.

    Filtered to ``lxmf.delivery`` so it only sees peers you can message — as
    distinct from ``nomadnetwork.node`` sites that serve pages. Record-only:
    the generic :class:`_AnnounceHandler` does the Monitor emit and tags each
    announce ``peer``/``site``/``other`` using these registries.
    """

    aspect_filter = "lxmf.delivery"
    receive_path_responses = True

    def __init__(self, transport: ReticulumTransport) -> None:
        self._transport = transport

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity,
        app_data,
        *_extra,
        **_kwargs,
    ) -> None:
        name = _display_name_from_app_data(app_data)
        self._transport._peers[destination_hash.hex()] = {
            "name": name,
            "last_seen": datetime.now(UTC),
        }


class _NodeAnnounceHandler:
    """Records NomadNet node announces for the read-only page browser.

    Aspect-filtered to ``nomadnetwork.node`` so it only sees node beacons (not
    LXMF delivery announces). Builds a simple directory of reachable nodes the
    user can browse, keyed by destination hash.
    """

    aspect_filter = "nomadnetwork.node"
    receive_path_responses = True

    def __init__(self, transport: ReticulumTransport) -> None:
        self._transport = transport

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity,
        app_data,
        *_extra,
        **_kwargs,
    ) -> None:
        name = _display_name_from_app_data(app_data, use_lxmf=False)
        self._transport._nodes[destination_hash.hex()] = {
            "name": name,
            "last_seen": datetime.now(UTC),
        }
        log.info(
            "[reticulum] nomadnet node: %s <%s>",
            name or "(unnamed)",
            destination_hash.hex()[:12],
        )


