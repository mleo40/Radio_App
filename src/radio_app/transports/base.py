"""The transport plugin contract.

Every medium (Reticulum, JS8Call, Mercury, and anything added later) implements
:class:`Transport`. The router only ever talks to this interface, so adding a new
platform is a single new class — no changes to the core, the message model or the
UI. Subclasses self-register by setting a ``name`` class attribute.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from ..core.message import UnifiedMessage

# Inbound callback signature: called by a transport when it receives a message.
ReceiveCallback = Callable[[UnifiedMessage], Awaitable[None] | None]

# Populated automatically as Transport subclasses are imported.
TRANSPORT_REGISTRY: dict[str, type[Transport]] = {}


class ReachabilityStatus(str, Enum):
    """Result of a passive endpoint reachability probe (no transmission).

    This answers "can we reach this transport's control endpoint right now?"
    (e.g. rnsd shared instance, the JS8Call API socket, a MeshCore USB/TCP
    device) - distinct from :meth:`Transport.is_reachable`, which asks whether a
    specific *recipient* can be messaged. Drives the mode-selector health dot.
    """

    OK = "ok"            # endpoint reachable / link up
    DOWN = "down"        # endpoint not reachable
    NOT_APPLICABLE = "na"  # transport has no own endpoint (e.g. NomadNet on RNS)


async def probe_tcp(host: str, port: int, timeout: float = 2.0) -> ReachabilityStatus:
    """Best-effort TCP-connect reachability probe (opens then closes a socket).

    Shared by socket-based transports (JS8Call, Mercury, MeshCore-over-TCP). It
    transmits no application data - it only confirms the control port accepts a
    connection right now.
    """
    import asyncio

    try:
        fut = asyncio.open_connection(host, port)
        _reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except (OSError, TimeoutError):
        return ReachabilityStatus.DOWN
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 - close failures don't change reachability
        pass
    return ReachabilityStatus.OK



@dataclass(frozen=True)
class TransportCapabilities:
    """What a transport can do, so the router can pick the best one per task.

    Conservative defaults describe a slow, lossy, plaintext, broadcast-only HF
    link — the lowest common denominator. Capable transports override upward.
    """

    max_message_size: int = 64           # bytes per message
    supports_broadcast: bool = True
    supports_addressing: bool = False    # can target a specific recipient?
    supports_groups: bool = False        # native named-group support?
    supports_encryption: bool = False    # end-to-end?
    supports_delivery_confirmation: bool = False
    #: Opt in to the router's app-level chunking/reassembly for messages that
    #: exceed ``max_message_size``. Only meaningful for small-MTU text media
    #: (JS8Call, MeshCore); larger transports carry whole messages natively.
    supports_chunking: bool = False
    is_realtime: bool = False            # suitable for live keyboard chat?
    typical_latency_s: float = 30.0
    needs_internet: bool = False         # excluded when off-grid
    address_scheme: str = "label"        # "rns_hash" | "callsign" | "node_id"...

    # -- identity / regulatory ------------------------------------------------
    #: Whether the medium attaches the operator's real callsign/grid (amateur HF
    #: must identify on the air). When False (e.g. Reticulum) the transport is
    #: anonymous and identifying info MUST be stripped before sending.
    carries_operator_identity: bool = True
    #: Whether transmitting encrypted/obscured payloads is prohibited on this
    #: medium (true for amateur HF). The compliance guard enforces this.
    prohibits_encryption: bool = False


class Transport(abc.ABC):
    """Abstract base every transport adapter implements."""

    #: Unique short name, e.g. "reticulum". Setting it registers the subclass.
    name: str = ""

    #: What the UI should render for this transport's mode workspace. "chat" is
    #: a contacts + conversation surface; "browse" is a page browser (NomadNet).
    #: Future surfaces can be added without touching the core.
    surface: str = "chat"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "name", ""):
            TRANSPORT_REGISTRY[cls.name] = cls

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self._on_receive: ReceiveCallback | None = None
        self._running = False

    # -- lifecycle ------------------------------------------------------------

    @abc.abstractmethod
    async def start(self) -> None:
        """Connect / open the medium. Must set ``self._running`` when ready."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Disconnect and release resources."""

    # -- data path ------------------------------------------------------------

    @abc.abstractmethod
    async def send(self, msg: UnifiedMessage) -> bool:
        """Transmit ``msg``. Return True if handed off to the medium."""

    def on_receive(self, callback: ReceiveCallback) -> None:
        """Register the callback invoked with each inbound UnifiedMessage."""
        self._on_receive = callback

    # -- introspection --------------------------------------------------------

    def capabilities(self) -> TransportCapabilities:
        """Describe this transport. Override to advertise real capabilities."""
        return TransportCapabilities()

    def local_identity(self) -> str | None:
        """The sender identity to use on this transport for anonymous media.

        Anonymous transports (e.g. Reticulum) return a non-identifying address
        (such as an RNS identity hash). Identity-carrying transports (HF) return
        None, so the operator callsign is used instead.
        """
        return None

    @property
    def running(self) -> bool:
        return self._running

    async def check_reachable(self) -> ReachabilityStatus:
        """Passively probe whether this transport's endpoint is reachable.

        This must NOT transmit anything; it only confirms the control endpoint
        (rnsd, the JS8Call API socket, a MeshCore device, ...) can be reached.
        The default simply reflects whether the transport started successfully;
        transports with a distinct endpoint (sockets, serial ports) should
        override to actually test it. Returns :class:`ReachabilityStatus`.
        """
        return (
            ReachabilityStatus.OK if self._running else ReachabilityStatus.DOWN
        )

    def is_reachable(self, msg: UnifiedMessage) -> bool:
        """Best-effort: can this transport deliver ``msg`` right now?

        Default heuristic: reachable iff running and the transport can carry the
        message's address type. Transports with live path/last-heard knowledge
        (e.g. Reticulum, JS8Call) should override for accuracy.
        """
        if not self._running:
            return False
        caps = self.capabilities()
        if msg.address_type.value == "direct":
            return caps.supports_addressing
        if msg.address_type.value == "group":
            return caps.supports_groups or caps.supports_broadcast
        return caps.supports_broadcast

    # -- helper for subclasses ------------------------------------------------

    async def _emit(self, msg: UnifiedMessage) -> None:
        """Subclasses call this to push a received message into the router."""
        msg.transport = self.name
        if self._on_receive is None:
            return
        result = self._on_receive(msg)
        if result is not None:  # support both sync and async callbacks
            await result

