"""Winlink transport (store-and-forward email over radio, via Pat).

We do not implement the Winlink B2F protocol or drive any modem ourselves.
Instead we wrap a separately-installed **Pat** client
(https://github.com/la5nta/pat, MIT-licensed) over its HTTP API — the same
"talk to an external app over a socket" pattern used for JS8Call and Mercury.
Pat owns the radio session, the RMS gateway dialogue and (for RF) the modem +
rig control; this adapter only:

* posts outbound messages to Pat's outbox  (``POST /api/mailbox/out``),
* optionally triggers a connection session  (``GET /api/connect?url=...``),
* polls the inbox for new mail            (``GET /api/mailbox/in`` + ``/{mid}``),
* reports endpoint health                 (``GET /api/status``).

**Connection methods.** Pat reaches a gateway over a pluggable transport encoded
as the *scheme* of the connect URL (``telnet://``, ``ardop://``, ``varahf://``,
...). We model these as :class:`WinlinkConnectMethod` entries in
:data:`CONNECT_METHODS`. Telnet (internet, no modem) ships now; RF modems such as
Mercury (VARA-compatible TNC) and VARA/ARDOP are added by appending a method —
no other code changes. The actual modem is configured inside Pat, outside this
app, exactly like the rig is for JS8Call.

Dependency-light by design: only the standard library is used (``urllib``);
blocking HTTP calls are run in a thread so the asyncio loop is never blocked.
Pat itself is a separate program the operator installs — it is never bundled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from ..core.message import AddressType, DeliveryStatus, UnifiedMessage
from .base import ReachabilityStatus, Transport, TransportCapabilities, probe_tcp

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WinlinkConnectMethod:
    """One way Pat can reach a Winlink gateway.

    ``scheme`` is the connect-URL scheme Pat expects (e.g. ``telnet``,
    ``ardop``, ``varahf``). ``needs_internet`` and ``needs_modem`` are
    informational (drive capability reporting and future UI hints). ``label`` is
    a human description for menus. ``probe_port`` is the default local TCP port
    whose openness indicates the path is usable (the modem/TNC is running); the
    auto-fallback and the Health board use it to detect which paths are up.
    ``None`` means "can't cheaply probe — just attempt it" (telnet, serial
    Pactor).
    """

    scheme: str
    label: str
    needs_internet: bool = False
    needs_modem: bool = False
    probe_port: int | None = None


# Registry of supported connection methods. Telnet is the dependency-free path
# (good for setup/testing and internet-connected use). RF methods require the
# matching modem configured *inside Pat* (Mercury/VARA/ARDOP/Pactor/packet) — add
# one here to expose it; nothing else in this file needs to change.
#
# NOTE: Mercury and VARA HF are BOTH reached via the ``varahf`` scheme — Mercury
# is a VARA-compatible TNC, so whichever of them is running answers on the VARA
# port (default 8300). They therefore share the single ``varahf`` fallback slot.
CONNECT_METHODS: dict[str, WinlinkConnectMethod] = {
    "telnet": WinlinkConnectMethod(
        "telnet", "Telnet (internet, no radio)", needs_internet=True
    ),
    # --- RF methods (require the corresponding modem set up in Pat) ----------
    "ardop": WinlinkConnectMethod(
        "ardop", "ARDOP (open-source soundcard modem)",
        needs_modem=True, probe_port=8515,
    ),
    "varahf": WinlinkConnectMethod(
        "varahf", "VARA HF (incl. Mercury VARA-compatible TNC)",
        needs_modem=True, probe_port=8300,
    ),
    "varafm": WinlinkConnectMethod(
        "varafm", "VARA FM", needs_modem=True, probe_port=8300,
    ),
    "pactor": WinlinkConnectMethod(
        "pactor", "Pactor (SCS hardware TNC)", needs_modem=True
    ),
    "ax25": WinlinkConnectMethod(
        "ax25", "Packet / AX.25 (KISS TNC or Direwolf)", needs_modem=True
    ),
}

_DEFAULT_PAT_URL = "http://127.0.0.1:8080"
_DEFAULT_METHOD = "telnet"
# Sentinel ``connect`` value that walks ``connect_order`` instead of one method.
_AUTO = "auto"
# Default fallback order for ``connect = "auto"``: internet first (fast, free),
# then a VARA-compatible RF modem (Mercury or VARA), then ARDOP. The user never
# has to pick — the first reachable path that actually connects wins.
_DEFAULT_AUTO_ORDER = ["telnet", "varahf", "ardop"]
_DEFAULT_POLL_INTERVAL_S = 60
_HTTP_TIMEOUT_S = 10.0
_STATUS_TIMEOUT_S = 3.0
_PROBE_TIMEOUT_S = 2.0


class WinlinkTransport(Transport):
    """Wraps a user-installed Pat client over its HTTP API."""

    name = "winlink"
    surface = "chat"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._poll_task: asyncio.Task | None = None
        self._seen_mids: set[str] = set()
        self._primed = False  # first inbox sweep only records existing MIDs

    # -- config helpers -------------------------------------------------------

    @property
    def _pat_url(self) -> str:
        return str(self.config.get("pat_url", _DEFAULT_PAT_URL)).rstrip("/")

    @property
    def _callsign(self) -> str:
        return str(self.config.get("callsign", "")).strip()

    @property
    def _connect_setting(self) -> str:
        return str(self.config.get("connect", _DEFAULT_METHOD)).strip().lower()

    @property
    def is_auto(self) -> bool:
        """True when ``connect = "auto"`` (walk ``connect_order``)."""
        return self._connect_setting == _AUTO

    @property
    def _method(self) -> WinlinkConnectMethod:
        """The single configured method (in auto mode, the first in the order).

        Kept as the representative for labels/identity; the actual dialing in
        auto mode iterates :meth:`_connect_order`.
        """
        name = self._connect_setting
        if name == _AUTO:
            order = self._connect_order()
            name = order[0] if order else _DEFAULT_METHOD
        return CONNECT_METHODS.get(name, CONNECT_METHODS[_DEFAULT_METHOD])

    def _connect_order(self) -> list[str]:
        """Ordered method names to try in auto mode (known methods only)."""
        raw = self.config.get("connect_order")
        if isinstance(raw, list) and raw:
            names = [str(x).strip().lower() for x in raw]
        else:
            names = list(_DEFAULT_AUTO_ORDER)
        return [n for n in names if n in CONNECT_METHODS]

    def connect_summary(self) -> str:
        """Human description of how the next session will be dialed."""
        if self.is_auto:
            return "auto: " + " \u2192 ".join(self._connect_order())
        return self._method.scheme

    def _probe_host(self) -> str:
        host = str(self.config.get("probe_host", "127.0.0.1")).strip()
        return host or "127.0.0.1"

    def _probe_port_for(self, method: WinlinkConnectMethod) -> int | None:
        """Local TCP port to probe for ``method`` (config can override)."""
        if method.scheme in ("varahf", "varafm"):
            return int(self.config.get("vara_port", method.probe_port or 8300))
        if method.scheme == "ardop":
            return int(self.config.get("ardop_port", method.probe_port or 8515))
        return method.probe_port

    def _gateway_for(self, method_name: str) -> str:
        """Per-method gateway override (``<method>_gateway``), else ``gateway``."""
        specific = self.config.get(f"{method_name}_gateway")
        if specific is not None and str(specific).strip():
            return str(specific).strip()
        return str(self.config.get("gateway", "")).strip()

    def _connect_url_for(self, method_name: str) -> str:
        method = CONNECT_METHODS.get(method_name, CONNECT_METHODS[_DEFAULT_METHOD])
        gateway = self._gateway_for(method_name)
        return f"{method.scheme}://{gateway}" if gateway else f"{method.scheme}://"

    @property
    def _poll_interval(self) -> float:
        try:
            return max(
                5.0, float(self.config.get("poll_interval", _DEFAULT_POLL_INTERVAL_S))
            )
        except (TypeError, ValueError):
            return float(_DEFAULT_POLL_INTERVAL_S)

    @property
    def _auto_connect(self) -> bool:
        """Whether ``send()`` should immediately trigger a Pat session.

        When False (default), messages are queued in Pat's outbox and delivered
        on the next session the operator initiates (manually or via
        :meth:`connect_now`). When True, each send dials the configured gateway.
        """
        return bool(self.config.get("auto_connect", False))

    def build_connect_url(self) -> str:
        """Compose the Pat ``connect`` URL from config.

        A raw ``connect_url`` in config wins (full escape hatch). Otherwise the
        URL is ``<scheme>://<gateway>`` using the configured method; for telnet
        with no gateway, Pat's bare ``telnet://`` default CMS host is used.
        """
        raw = str(self.config.get("connect_url", "")).strip()
        if raw:
            return raw
        gateway = str(self.config.get("gateway", "")).strip()
        scheme = self._method.scheme
        return f"{scheme}://{gateway}" if gateway else f"{scheme}://"

    # -- capabilities ---------------------------------------------------------

    def capabilities(self) -> TransportCapabilities:
        # In auto mode the link only truly needs the internet if EVERY method in
        # the order needs it (i.e. telnet-only). With an RF fallback present it
        # can work off-grid, so report needs_internet=False.
        if self.is_auto:
            methods = [CONNECT_METHODS[n] for n in self._connect_order()]
            needs_internet = bool(methods) and all(m.needs_internet for m in methods)
        else:
            needs_internet = self._method.needs_internet
        return TransportCapabilities(
            max_message_size=120_000,        # email-class: bodies + attachments
            supports_broadcast=False,        # point-to-mailbox, not on-air bcast
            supports_addressing=True,        # CALLSIGN@winlink.org
            supports_groups=False,
            supports_encryption=False,       # Winlink content is not E2E encrypted
            supports_delivery_confirmation=False,  # store-and-forward; no live ACK
            is_realtime=False,
            typical_latency_s=300.0,         # depends on next session/forwarding
            needs_internet=needs_internet,
            address_scheme="email",          # callsign-based Winlink address
            carries_operator_identity=True,  # amateur: identify with callsign
            prohibits_encryption=True,        # amateur HF rules apply on RF
        )

    def local_identity(self) -> str | None:
        # Identity-carrying medium: use the operator callsign, not an anon id.
        return None

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        # The adapter is "up" as a client as soon as it starts; whether Pat is
        # actually reachable is reported separately by check_reachable().
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        log.info(
            "Winlink transport started (pat=%s, method=%s)",
            self._pat_url, self._method.scheme,
        )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    async def check_reachable(self) -> ReachabilityStatus:
        """Reachable iff Pat's HTTP API answers ``/api/status``."""
        try:
            await asyncio.to_thread(self._http_get, "/api/status", _STATUS_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - any failure means "down"
            return ReachabilityStatus.DOWN
        return ReachabilityStatus.OK

    # -- data path ------------------------------------------------------------

    async def send(self, msg: UnifiedMessage) -> bool:
        """Queue an outbound message in Pat's outbox (optionally dial a session).

        Only DIRECT messages are meaningful for Winlink (email to a callsign).
        """
        if msg.address_type is not AddressType.DIRECT or not msg.recipient:
            log.warning("Winlink only supports direct messages with a recipient.")
            return False
        if not self._running:
            return False

        subject = str(msg.metadata.get("subject") or self._derive_subject(msg.content))
        form = {
            "to": msg.recipient,
            "subject": subject,
            "body": msg.content,
            # Pat requires an RFC3339 date on the outbound form.
            "date": datetime.now(UTC).isoformat(),
        }
        cc = msg.metadata.get("cc")
        if cc:
            form["cc"] = cc if isinstance(cc, str) else ",".join(map(str, cc))

        try:
            await asyncio.to_thread(self._http_post_form, "/api/mailbox/out", form)
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink outbox POST failed: %s", exc)
            return False

        msg.status = DeliveryStatus.SENT  # queued for the next forwarding session

        if self._auto_connect:
            # Best-effort: dial the gateway now. Failure to connect does not
            # unqueue the message (it stays in Pat's outbox for a later try).
            try:
                await self.connect_now()
            except Exception as exc:  # noqa: BLE001
                log.warning("Winlink auto-connect failed: %s", exc)
        return True

    async def connect_now(self, connect_url: str | None = None) -> int:
        """Trigger a Pat session and return Pat's ``NumReceived``.

        Resolution order:
        * ``connect_url`` given (UI override, e.g. a picked RMS) -> dial exactly
          that.
        * ``connect = "auto"`` -> walk :meth:`_connect_order`, probing each
          path's endpoint and dialing the first that connects (the operator
          never picks telnet vs Mercury vs VARA — it just gets out).
        * otherwise -> dial the single configured method/gateway.

        Raises when nothing succeeds.
        """
        if connect_url:
            return await self._attempt_connect(connect_url)
        if self.is_auto:
            return await self._connect_auto()
        return await self._attempt_connect(self.build_connect_url())

    async def _connect_auto(self) -> int:
        """Try each method in order; first reachable + connecting one wins."""
        tried: list[str] = []
        for name in self._connect_order():
            method = CONNECT_METHODS[name]
            port = self._probe_port_for(method)
            if port is not None:
                status = await probe_tcp(self._probe_host(), port, _PROBE_TIMEOUT_S)
                if status is not ReachabilityStatus.OK:
                    log.info(
                        "Winlink auto: skip %s (endpoint %s:%s down)",
                        name, self._probe_host(), port,
                    )
                    continue
            tried.append(name)
            try:
                received = await self._attempt_connect(self._connect_url_for(name))
                log.info("Winlink auto: %s connected.", name)
                return received
            except Exception as exc:  # noqa: BLE001 - try the next method
                log.info("Winlink auto: %s failed (%s).", name, exc)
        raise RuntimeError(
            "no Winlink path succeeded "
            f"(attempted: {', '.join(tried) or 'none reachable'})"
        )

    async def _attempt_connect(self, url: str) -> int:
        """Dial one connect URL via Pat; raise on session failure (HTTP 5xx)."""
        log.info("Winlink connecting: %s", url)
        query = urllib.parse.urlencode({"url": url})
        body = await asyncio.to_thread(
            self._http_get, f"/api/connect?{query}", _HTTP_TIMEOUT_S
        )
        received = 0
        try:
            received = int(json.loads(body).get("NumReceived", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        # Pull anything new right away rather than waiting for the next tick.
        await self._sweep_inbox()
        return received

    async def path_status(self) -> list[dict]:
        """Probe each connection path's endpoint for the Health board.

        Returns one entry per method in the active set (the auto order, or the
        single configured method) with keys ``name``, ``scheme``, ``label``,
        ``reachable`` (True/False/None) and ``detail``. ``reachable`` is ``None``
        for paths that can't be cheaply probed (telnet rides Pat itself; serial
        Pactor). This is what powers the per-modem rows (e.g. a varahf/Mercury
        modem shown DOWN until it's running).
        """
        names = self._connect_order() if self.is_auto else [self._method.scheme]
        out: list[dict] = []
        for name in names:
            method = CONNECT_METHODS.get(name)
            if method is None:
                continue
            port = self._probe_port_for(method)
            reachable: bool | None
            if port is None:
                reachable = None
                detail = "via Pat" if method.scheme == "telnet" else "serial"
            else:
                status = await probe_tcp(
                    self._probe_host(), port, _PROBE_TIMEOUT_S
                )
                reachable = status is ReachabilityStatus.OK
                detail = f"{self._probe_host()}:{port}"
            out.append({
                "name": name,
                "scheme": method.scheme,
                "label": method.label,
                "reachable": reachable,
                "detail": detail,
            })
        return out

    async def list_gateways(
        self, mode: str | None = None, prefix: str | None = None
    ) -> list[dict]:
        """Return RMS gateways known to Pat (``GET /api/rmslist``).

        Optional ``mode`` (e.g. ``ardop``/``packet``) and callsign ``prefix``
        filters are passed through to Pat. Returns the decoded JSON list (each
        entry has at least ``callsign``); empty for telnet or when Pat has no
        cached list.
        """
        params: dict[str, str] = {}
        if mode:
            params["mode"] = mode
        if prefix:
            params["prefix"] = prefix
        path = "/api/rmslist"
        if params:
            path += "?" + urllib.parse.urlencode(params)
        body = await asyncio.to_thread(self._http_get, path, _HTTP_TIMEOUT_S)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    # -- inbound polling ------------------------------------------------------

    async def _poll_loop(self) -> None:
        # Prime once so pre-existing inbox messages aren't replayed as "new".
        try:
            await self._sweep_inbox()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("Winlink initial inbox sweep failed: %s", exc)
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._sweep_inbox()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.debug("Winlink inbox poll error: %s", exc)

    async def _sweep_inbox(self) -> None:
        """Fetch the inbox list and emit any messages we haven't seen yet."""
        try:
            listing = await asyncio.to_thread(
                self._http_get, "/api/mailbox/in", _HTTP_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Winlink inbox list failed: %s", exc)
            return
        try:
            entries = json.loads(listing) or []
        except json.JSONDecodeError:
            return

        first_sweep = not self._primed
        self._primed = True

        for entry in entries:
            mid = str(entry.get("MID", "")).strip()
            if not mid or mid in self._seen_mids:
                continue
            self._seen_mids.add(mid)
            if first_sweep:
                # Don't replay history accumulated before we started.
                continue
            msg = await self._fetch_message(mid, entry)
            if msg is not None:
                await self._emit(msg)

    async def _fetch_message(self, mid: str, entry: dict) -> UnifiedMessage | None:
        """Fetch a single inbox message (with body) and map to UnifiedMessage."""
        body_text = ""
        try:
            detail_raw = await asyncio.to_thread(
                self._http_get, f"/api/mailbox/in/{urllib.parse.quote(mid)}",
                _HTTP_TIMEOUT_S,
            )
            detail = json.loads(detail_raw)
            body_text = str(detail.get("Body", ""))
            entry = {**entry, **detail}  # detail carries the body + same fields
        except Exception as exc:  # noqa: BLE001
            log.debug("Winlink message fetch failed for %s: %s", mid, exc)

        sender = _addr_str(entry.get("From")) or "unknown"
        recipient = self._callsign or _first_addr(entry.get("To"))
        subject = str(entry.get("Subject", ""))
        files = entry.get("Files") or []
        attachments = [
            f.get("Name") for f in files if isinstance(f, dict) and f.get("Name")
        ]
        return UnifiedMessage(
            sender=sender,
            content=body_text,
            address_type=AddressType.DIRECT,
            recipient=recipient,
            status=DeliveryStatus.RECEIVED,
            transport=self.name,
            metadata={
                "subject": subject,
                "mid": mid,
                "attachments": attachments,
            },
        )

    # -- HTTP plumbing (stdlib only; called via asyncio.to_thread) ------------

    def _http_get(self, path: str, timeout: float) -> str:
        req = urllib.request.Request(self._pat_url + path, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace")

    def _http_post_form(self, path: str, fields: dict[str, str]) -> str:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            self._pat_url + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace")

    # -- misc -----------------------------------------------------------------

    @staticmethod
    def _derive_subject(content: str) -> str:
        """Use the first line (trimmed) as the subject when none is supplied."""
        first = (content or "").strip().splitlines()[0] if content.strip() else ""
        first = first.strip()
        if len(first) > 60:
            first = first[:57] + "..."
        return first or "(no subject)"


def _addr_str(value: object) -> str:
    """Normalise a Pat/fbb address (string or {Addr,Proto} dict) to a string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("Addr", "addr", "Address", "address"):
            if value.get(key):
                return str(value[key]).strip()
    return str(value).strip()


def _first_addr(value: object) -> str:
    """First address from a To/Cc field (list of addresses, or single)."""
    if isinstance(value, list):
        return _addr_str(value[0]) if value else ""
    return _addr_str(value)



