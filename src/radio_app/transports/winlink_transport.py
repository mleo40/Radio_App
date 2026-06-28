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
import email
import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .._compat import UTC
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
        # Correlate our outbound messages with Pat's outbox so we can upgrade a
        # message from "queued in Pat" (SENT) to DELIVERED once a session has
        # actually forwarded it (it disappears from the outbox). Maps Pat's
        # outbox MID -> (our msg_id, recipient).
        self._pending_out: dict[str, tuple[str, str]] = {}
        # RF modem process owned by this transport (e.g. Mercury spawned via
        # modem_cmd). None when not spawned by us (externally managed).
        self._modem_proc: asyncio.subprocess.Process | None = None

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
        return self._pat_connect_string(method.scheme, gateway)

    @staticmethod
    def _pat_connect_string(scheme: str, gateway: str) -> str:
        """Build the string handed to Pat's ``/api/connect?url=`` for a path.

        For **telnet with no gateway** this returns Pat's built-in connect
        *alias* ``"telnet"`` (not the bare URL ``telnet://``). Pat expands the
        alias to the full CMS URL **including the target callsign**
        (``telnet://{mycall}:CMSTelnet@cms.winlink.org:8772/wl2k``); a bare
        ``telnet://`` has no target and Pat rejects it with "Invalid or missing
        target callsign". RF schemes keep ``<scheme>://<gateway>`` — they
        genuinely need an RMS gateway callsign as the target.
        """
        gateway = (gateway or "").strip()
        if scheme == "telnet" and not gateway:
            return "telnet"
        return f"{scheme}://{gateway}" if gateway else f"{scheme}://"

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
        """Compose the Pat ``connect`` string from config.

        A raw ``connect_url`` in config wins (full escape hatch). Otherwise it is
        ``<scheme>://<gateway>`` using the configured method; for **telnet with
        no gateway** it is Pat's built-in ``telnet`` alias (which expands to the
        full CMS URL with a target callsign) — a bare ``telnet://`` is rejected
        by Pat as "Invalid or missing target callsign".
        """
        raw = str(self.config.get("connect_url", "")).strip()
        if raw:
            return raw
        gateway = str(self.config.get("gateway", "")).strip()
        return self._pat_connect_string(self._method.scheme, gateway)

    # -- capabilities ---------------------------------------------------------

    def capabilities(self) -> TransportCapabilities:
        # In auto mode the link only truly needs the internet if EVERY method in
        # the order needs it (i.e. telnet-only). With an RF fallback present it
        # can work off-grid, so report needs_internet=False.
        if self.is_auto:
            methods = [CONNECT_METHODS[n] for n in self._connect_order()]
            needs_internet = bool(methods) and all(m.needs_internet for m in methods)
            # Uses the shared radio if ANY configured path drives an RF modem
            # (telnet-only never touches the radio; an RF fallback can).
            uses_radio = any(m.needs_modem for m in methods)
        else:
            needs_internet = self._method.needs_internet
            uses_radio = self._method.needs_modem
        return TransportCapabilities(
            max_message_size=120_000,        # email-class: bodies + attachments
            supports_broadcast=False,        # point-to-mailbox, not on-air bcast
            supports_addressing=True,        # CALLSIGN@winlink.org
            supports_groups=False,
            supports_encryption=False,       # Winlink content is not E2E encrypted
            supports_delivery_confirmation=False,  # store-and-forward; no live ACK
            supports_attachments=True,        # multipart file uploads to Pat
            is_realtime=False,
            typical_latency_s=300.0,         # depends on next session/forwarding
            needs_internet=needs_internet,
            address_scheme="email",          # callsign-based Winlink address
            carries_operator_identity=True,  # amateur: identify with callsign
            prohibits_encryption=True,        # amateur HF rules apply on RF
            uses_shared_radio=uses_radio,     # only when an RF modem path is used
        )

    def local_identity(self) -> str | None:
        # Identity-carrying medium: use the operator callsign, not an anon id.
        return None

    def validate_config(self) -> list[str]:
        """Return human-readable warnings about the config (no network calls).

        Catches the misconfigurations that would otherwise only surface as a
        silent fallback or a failed dial: a malformed ``pat_url``, an unknown
        ``connect`` method, unknown ``connect_order`` entries, an empty callsign,
        or invalid modem ports. Empty list means the config looks usable.
        """
        warnings: list[str] = []
        parts = urllib.parse.urlsplit(self._pat_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            warnings.append(f"pat_url looks malformed: {self._pat_url!r}")

        known = ", ".join(sorted(CONNECT_METHODS))
        setting = self._connect_setting
        if setting != _AUTO and setting not in CONNECT_METHODS:
            warnings.append(
                f"connect = {setting!r} is not a known method (use {known} "
                "or 'auto')"
            )

        raw_order = self.config.get("connect_order")
        if isinstance(raw_order, list):
            unknown = [
                str(x) for x in raw_order
                if str(x).strip().lower() not in CONNECT_METHODS
            ]
            if unknown:
                warnings.append(
                    f"connect_order has unknown method(s): {', '.join(unknown)} "
                    f"(known: {known})"
                )
            if self.is_auto and not self._connect_order():
                warnings.append(
                    "connect = 'auto' but connect_order lists no known methods"
                )

        if not self._callsign:
            warnings.append("callsign is empty (set your Winlink callsign)")

        for key in ("vara_port", "ardop_port"):
            if key in self.config:
                try:
                    port = int(self.config[key])
                    if not 1 <= port <= 65535:
                        raise ValueError
                except (TypeError, ValueError):
                    warnings.append(
                        f"{key} is not a valid TCP port: {self.config[key]!r}"
                    )
        return warnings

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        # The adapter is "up" as a client as soon as it starts; whether Pat is
        # actually reachable is reported separately by check_reachable().
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        for warning in self.validate_config():
            log.warning("Winlink config: %s", warning)
        log.info(
            "Winlink transport started (pat=%s, method=%s)",
            self._pat_url, self._method.scheme,
        )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        await self._stop_modem()

    async def check_reachable(self) -> ReachabilityStatus:
        """Reachable iff Pat's HTTP API answers ``/api/status``."""
        try:
            await asyncio.to_thread(self._http_get, "/api/status", _STATUS_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - any failure means "down"
            return ReachabilityStatus.DOWN
        return ReachabilityStatus.OK

    # -- modem auto-launch (Mercury / VARA HF) --------------------------------

    async def _start_modem_if_needed(self, method_name: str) -> bool:
        """Spawn the configured RF modem if its control port is not yet open.

        Called before a varahf/varafm connect attempt. If ``modem_cmd`` is not
        set in config the call is a no-op and returns True (caller can still
        proceed; the modem may already be running externally). Only acts on
        methods whose ``needs_modem`` flag is True.

        Returns True when the modem port is open after this call, False when
        the modem could not be started (no command, binary missing, timeout).
        """
        import shlex

        method = CONNECT_METHODS.get(method_name)
        if not method or not method.needs_modem:
            return True

        port = self._probe_port_for(method)
        if port is None:
            return True  # no probe possible; proceed without check

        host = self._probe_host()

        # If port is already open (externally managed or already running), done.
        status = await probe_tcp(host, port, _PROBE_TIMEOUT_S)
        if status is ReachabilityStatus.OK:
            return True

        modem_cmd = str(self.config.get("modem_cmd", "") or "").strip()
        if not modem_cmd:
            return False  # not configured to auto-launch

        # If we already own a running modem proc, just wait for it.
        if self._modem_proc is None or self._modem_proc.returncode is not None:
            argv = shlex.split(modem_cmd)
            log.info("Winlink: spawning RF modem %r", argv)
            try:
                self._modem_proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                log.error(
                    "Winlink: modem command not found: %r. "
                    "Set [transports.winlink].modem_cmd in your config.",
                    modem_cmd,
                )
                return False
            except Exception:  # noqa: BLE001
                log.exception("Winlink: failed to spawn modem %r", modem_cmd)
                return False

        wait_s = max(1.0, float(self.config.get("modem_wait_s", 10) or 10))
        deadline = asyncio.get_event_loop().time() + wait_s
        while asyncio.get_event_loop().time() < deadline:
            status = await probe_tcp(host, port, _PROBE_TIMEOUT_S)
            if status is ReachabilityStatus.OK:
                log.info("Winlink: modem ready on %s:%d", host, port)
                return True
            await asyncio.sleep(0.5)

        log.error(
            "Winlink: modem %r did not open port %d within %.0fs",
            modem_cmd, port, wait_s,
        )
        return False

    async def _stop_modem(self) -> None:
        """Terminate the owned modem process if we spawned it."""
        proc = self._modem_proc
        self._modem_proc = None
        if proc is None or proc.returncode is not None:
            return
        log.info("Winlink: stopping owned modem process")
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("Winlink: modem did not exit cleanly; sending SIGKILL")
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # -- data path ------------------------------------------------------------

    async def send(self, msg: UnifiedMessage) -> bool:
        """Queue an outbound message in Pat's outbox (optionally dial a session).

        Only DIRECT messages are meaningful for Winlink (email to a callsign).
        Local file paths in ``metadata["attach"]`` are uploaded as Winlink
        attachments (multipart); their base names are recorded in
        ``metadata["attachments"]`` so the UI can show what was sent.
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

        files = self._read_attachments(msg.metadata.get("attach") or [])

        # Snapshot the outbox so we can identify the MID Pat assigns to this
        # message (its POST does not return one) and correlate delivery later.
        before = await self._outbox_mids()
        try:
            if files:
                await asyncio.to_thread(
                    self._http_post_multipart, "/api/mailbox/out", form, files
                )
            else:
                await asyncio.to_thread(
                    self._http_post_form, "/api/mailbox/out", form
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink outbox POST failed: %s", exc)
            return False

        if files:
            # Record what we attached for UI display (names, not the bytes).
            msg.metadata["attachments"] = [name for name, _, _ in files]
        await self._correlate_outbound(msg, before)

        msg.status = DeliveryStatus.SENT  # queued for the next forwarding session

        if self._auto_connect:
            # Best-effort: dial the gateway now. Failure to connect does not
            # unqueue the message (it stays in Pat's outbox for a later try).
            try:
                await self.connect_now()
            except Exception as exc:  # noqa: BLE001
                log.warning("Winlink auto-connect failed: %s", exc)
        return True

    def _read_attachments(
        self, paths: object
    ) -> list[tuple[str, bytes, str]]:
        """Read outbound attachment file paths into (name, bytes, content-type)."""
        out: list[tuple[str, bytes, str]] = []
        if not isinstance(paths, list):
            return out
        for p in paths:
            path = str(p)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                log.warning("Winlink: cannot read attachment %s: %s", path, exc)
                continue
            name = os.path.basename(path) or "attachment"
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            out.append((name, data, ctype))
        return out

    async def _correlate_outbound(
        self, msg: UnifiedMessage, before: set[str] | None
    ) -> None:
        """After posting, learn the new outbox MID so delivery can be confirmed."""
        if before is None:
            return
        after = await self._outbox_mids()
        if after is None:
            return
        new = after - before
        # Only correlate when exactly one message appeared (unambiguous).
        if len(new) == 1:
            self._pending_out[new.pop()] = (msg.msg_id, msg.recipient or "")

    # -- Winlink standard forms ----------------------------------------------
    async def list_forms(self) -> list[dict]:
        """List installed Winlink form templates (flattened catalog tree).

        Returns ``[{"name", "path", "folder"}]`` sorted by folder then name;
        ``path`` is the template path :meth:`compose_form` expects. Empty when
        no forms are installed (run :meth:`update_forms` to fetch them).
        """
        try:
            body = await asyncio.to_thread(
                self._http_get, "/api/formcatalog", _HTTP_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink form catalog fetch failed: %s", exc)
            return []
        try:
            root = json.loads(body)
        except (ValueError, TypeError):
            return []
        forms: list[dict] = []
        _flatten_form_folder(root, "", forms)
        forms.sort(key=lambda f: (f["folder"].lower(), f["name"].lower()))
        return forms

    async def update_forms(self) -> dict:
        """Download/install the latest Winlink standard form templates.

        Returns ``{"version", "action"}`` (action ``update``/``none``) or ``{}``
        on failure. Pat performs the download, so it needs internet access.
        """
        try:
            body = await asyncio.to_thread(
                self._http_post_form, "/api/formsUpdate", {}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink forms update failed: %s", exc)
            return {}
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return {}
        return {
            "version": str(data.get("newestVersion", "")),
            "action": str(data.get("action", "")),
        }

    async def get_form_template(self, template_path: str) -> str:
        """Fetch a template's processed text (for preview / prompt discovery)."""
        query = urllib.parse.urlencode({"template": template_path})
        try:
            return await asyncio.to_thread(
                self._http_get, f"/api/template?{query}", _HTTP_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink template fetch failed: %s", exc)
            return ""

    async def compose_form(
        self,
        template_path: str,
        responses: dict[str, str] | None = None,
        *,
        to: str | None = None,
        cc: str | None = None,
        subject: str | None = None,
        msg_id: str | None = None,
    ) -> dict | None:
        """Build a Winlink form and queue it in Pat's outbox.

        Drives Pat's browserless form flow: build the form server-side (which
        generates the ``RMS_Express_Form`` XML attachment, held under a one-time
        ``forminstance`` key), retrieve the computed to/cc/subject/body, then
        post to the outbox with the same key so Pat re-attaches the XML.

        ``responses`` maps the template's text prompts to answers (an empty map
        is valid — the form still builds with defaults). ``to``/``cc``/``subject``
        override the form's computed values. Pass ``msg_id`` to correlate the
        queued message for a later delivery receipt. Returns the built
        ``{"to","cc","subject","body"}``, or ``None`` on failure.
        """
        if not self._running:
            return None
        key = uuid.uuid4().hex
        cookie = {"Cookie": f"forminstance={key}"}
        qs = urllib.parse.urlencode({"template": template_path})
        # 1) Build the form (stores Message + XML attachment under the cookie).
        try:
            await asyncio.to_thread(
                self._http_post_json,
                f"/api/form?{qs}",
                {"responses": responses or {}},
                cookie,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink form build failed: %s", exc)
            return None
        # 2) Retrieve the computed message fields.
        try:
            built_raw = await asyncio.to_thread(
                self._http_get, "/api/form", _HTTP_TIMEOUT_S, cookie
            )
            built = json.loads(built_raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink form retrieve failed: %s", exc)
            return None
        out = {
            "to": to if to is not None else str(built.get("msg_to", "")),
            "cc": cc if cc is not None else str(built.get("msg_cc", "")),
            "subject": (
                subject if subject is not None
                else str(built.get("msg_subject", ""))
            ),
            "body": str(built.get("msg_body", "")),
        }
        # 3) Queue to the outbox with the same cookie (Pat re-attaches the XML).
        before = await self._outbox_mids() if msg_id else None
        form_fields = {
            "to": out["to"],
            "cc": out["cc"],
            "subject": out["subject"],
            "body": out["body"],
            "date": datetime.now(UTC).isoformat(),
        }
        try:
            await asyncio.to_thread(
                self._http_post_form, "/api/mailbox/out", form_fields, cookie
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink form outbox POST failed: %s", exc)
            return None
        if msg_id is not None and before is not None:
            after = await self._outbox_mids()
            if after is not None:
                new = after - before
                if len(new) == 1:
                    self._pending_out[new.pop()] = (msg_id, out["to"])
        return out


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
        # Single configured method — auto-launch modem if needed before dialing.
        await self._start_modem_if_needed(self._method.scheme)
        return await self._attempt_connect(self.build_connect_url())

    async def _connect_auto(self) -> int:
        """Try each method in order; first reachable + connecting one wins."""
        tried: list[str] = []
        errors: list[str] = []
        for name in self._connect_order():
            method = CONNECT_METHODS[name]
            # Try to auto-launch the modem before probing so the probe passes
            # even if the operator hasn't started it manually.
            if method.needs_modem:
                await self._start_modem_if_needed(name)
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
                errors.append(f"{name}: {exc}")
        # Surface Pat's actual error(s) so the failure is diagnosable (bad
        # password, no internet, CMS refused, modem down) instead of opaque.
        detail = "; ".join(errors) if errors else "none reachable"
        raise RuntimeError(
            f"no Winlink path succeeded (attempted: "
            f"{', '.join(tried) or 'none reachable'}) — {detail}"
        )

    async def _attempt_connect(self, url: str) -> int:
        """Dial one connect URL via Pat; raise on session failure (HTTP 5xx).

        On an HTTP error, Pat's response body usually explains *why* the session
        failed (e.g. "no command response from CMS", a secure-login problem, or a
        connection refusal); we include it in the raised error so the operator
        sees the real cause rather than a bare status code.
        """
        log.info("Winlink connecting: %s", url)
        query = urllib.parse.urlencode({"url": url})
        try:
            body = await asyncio.to_thread(
                self._http_get, f"/api/connect?{query}", _HTTP_TIMEOUT_S
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                detail = ""
            raise RuntimeError(
                f"Pat rejected the {url!r} session "
                f"(HTTP {exc.code}{': ' + detail if detail else ''})"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"could not reach Pat at {self._pat_url} ({exc.reason})"
            ) from exc
        received = 0
        try:
            received = int(json.loads(body).get("NumReceived", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        # Pull anything new right away rather than waiting for the next tick,
        # and confirm delivery of anything the session forwarded.
        await self._sweep_inbox()
        await self._reconcile_outbox()
        return received

    async def _outbox_mids(self) -> set[str] | None:
        """Set of MIDs currently queued in Pat's outbox (None on error)."""
        try:
            raw = await asyncio.to_thread(
                self._http_get, "/api/mailbox/out", _HTTP_TIMEOUT_S
            )
            data = json.loads(raw) or []
        except Exception:  # noqa: BLE001 - treat any failure as "unknown"
            return None
        if not isinstance(data, list):
            return None
        return {str(e.get("MID", "")).strip() for e in data if e.get("MID")}

    async def outbox_count(self) -> int | None:
        """How many messages are queued in Pat's outbox (None if unknown).

        Used by the UI to report how many messages a session will try to send,
        and (by comparing before/after) how many actually went out.
        """
        mids = await self._outbox_mids()
        return None if mids is None else len(mids)

    async def _reconcile_outbox(self) -> None:
        """Emit a delivery receipt for any tracked message that left the outbox.

        A message that was queued (tracked in :attr:`_pending_out`) but is no
        longer in Pat's outbox after a session was forwarded to the CMS/RMS, so
        we mark it DELIVERED via the standard delivery-receipt telemetry the UI
        already understands (the same path Reticulum LXMF receipts use).
        """
        if not self._pending_out:
            return
        current = await self._outbox_mids()
        if current is None:
            return
        forwarded = [mid for mid in self._pending_out if mid not in current]
        for mid in forwarded:
            ref_id, recipient = self._pending_out.pop(mid)
            await self._emit_delivery(ref_id, recipient)

    async def _emit_delivery(self, ref_id: str, recipient: str) -> None:
        """Emit a 'delivered' receipt telemetry event for ``ref_id``."""
        telem = UnifiedMessage(
            sender=self.name,
            content="",
            address_type=AddressType.DIRECT,
            transport=self.name,
            metadata={
                "kind": "delivery",
                "ref_msg_id": ref_id,
                "status": "delivered",
                "recipient": recipient,
            },
        )
        await self._emit(telem)

    async def fetch_attachment(self, mid: str, name: str) -> bytes:
        """Download one inbound attachment's raw bytes from Pat."""
        path = (
            f"/api/mailbox/in/{urllib.parse.quote(mid)}"
            f"/{urllib.parse.quote(name)}"
        )
        return await asyncio.to_thread(self._http_get_bytes, path, _HTTP_TIMEOUT_S)

    async def save_attachments(self, mid: str, dest_dir: str) -> list[str]:
        """Download all attachments of an inbox message into ``dest_dir``.

        Returns the list of saved local file paths.
        """
        try:
            detail_raw = await asyncio.to_thread(
                self._http_get,
                f"/api/mailbox/in/{urllib.parse.quote(mid)}",
                _HTTP_TIMEOUT_S,
            )
            detail = json.loads(detail_raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("Winlink: cannot list attachments for %s: %s", mid, exc)
            return []
        names = [
            f.get("Name")
            for f in (detail.get("Files") or [])
            if isinstance(f, dict) and f.get("Name")
        ]
        os.makedirs(dest_dir, exist_ok=True)
        saved: list[str] = []
        for name in names:
            try:
                data = await self.fetch_attachment(mid, name)
            except Exception as exc:  # noqa: BLE001
                log.warning("Winlink: attachment %s download failed: %s", name, exc)
                continue
            out_path = os.path.join(dest_dir, os.path.basename(name))
            try:
                with open(out_path, "wb") as fh:
                    fh.write(data)
            except OSError as exc:
                log.warning("Winlink: cannot write %s: %s", out_path, exc)
                continue
            saved.append(out_path)
        return saved

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

    async def stream_events(
        self,
        on_event: Callable[[dict], None],
        should_stop: Callable[[], bool],
        *,
        on_prompt: Callable[[dict], asyncio.Future | object] | None = None,
    ) -> None:
        """Stream Pat's live ``/ws`` events as decoded dicts to ``on_event``.

        Best-effort live session feedback (Status/Progress/Notification). Parses
        the host/port from ``pat_url`` and opens a minimal WebSocket; any failure
        is swallowed so the UI never breaks if Pat has no WebSocket.

        ``on_prompt`` (optional) is an async callable invoked when Pat asks for a
        **secure-login password** mid-session (it only does this when no password
        is set in Pat's own config). It receives the prompt dict and returns the
        password string (or ``None`` to decline); the answer is sent back over
        the same WebSocket as a ``prompt_response``. This is what enables
        per-session credentials without storing the password anywhere.
        """
        from . import winlink_ws

        parts = urllib.parse.urlsplit(self._pat_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        outgoing: asyncio.Queue[str] = asyncio.Queue()
        tasks: set[asyncio.Task] = set()

        def _on_text(text: str) -> None:
            try:
                event = json.loads(text)
            except (ValueError, json.JSONDecodeError):
                return
            if not isinstance(event, dict):
                return
            prompt = event.get("Prompt")
            if (
                on_prompt is not None
                and isinstance(prompt, dict)
                and str(prompt.get("kind")) == "password"
            ):
                # Answer asynchronously so the WS read loop is never blocked
                # while the operator types; the response is queued for sending.
                task = asyncio.create_task(
                    self._answer_password_prompt(prompt, on_prompt, outgoing)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                return
            on_event(event)

        try:
            await winlink_ws.stream(
                host, port, "/ws", _on_text, should_stop, outgoing=outgoing
            )
        except Exception as exc:  # noqa: BLE001 - feedback is optional
            log.debug("Winlink event stream ended: %s", exc)

    async def _answer_password_prompt(
        self,
        prompt: dict,
        on_prompt: Callable[[dict], object],
        outgoing: asyncio.Queue[str],
    ) -> None:
        """Resolve a password via ``on_prompt`` and queue Pat's prompt_response."""
        try:
            value = await on_prompt(prompt)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            log.debug("Winlink password prompt handler failed: %s", exc)
            value = None
        if not value:
            return
        outgoing.put_nowait(
            json.dumps(
                {"prompt_response": {"id": prompt.get("id", ""), "value": value}}
            )
        )

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
            body_text = decode_winlink_body(str(detail.get("Body", "")))
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

    def _http_get(self, path: str, timeout: float,
                  headers: dict[str, str] | None = None) -> str:
        req = urllib.request.Request(
            self._pat_url + path, method="GET", headers=headers or {}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace")

    def _http_get_bytes(self, path: str, timeout: float) -> bytes:
        req = urllib.request.Request(self._pat_url + path, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read()

    def _http_post_form(self, path: str, fields: dict[str, str],
                        headers: dict[str, str] | None = None) -> str:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            self._pat_url + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                **(headers or {}),
            },
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace")

    def _http_post_json(self, path: str, payload: dict,
                        headers: dict[str, str] | None = None) -> str:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._pat_url + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace")


    def _http_post_multipart(
        self,
        path: str,
        fields: dict[str, str],
        files: list[tuple[str, bytes, str]],
    ) -> str:
        """POST ``multipart/form-data`` with text fields and ``files`` parts.

        Pat's outbox handler reads attachments from the multipart ``files``
        field; everything else (to/subject/body/date) rides as normal fields.
        Built with the stdlib so no extra dependency is needed.
        """
        boundary = "----RadioApp" + uuid.uuid4().hex
        crlf = b"\r\n"
        body = bytearray()
        for key, value in fields.items():
            body += b"--" + boundary.encode() + crlf
            body += (
                f'Content-Disposition: form-data; name="{key}"'.encode() + crlf
            )
            body += crlf + str(value).encode("utf-8") + crlf
        for name, data, ctype in files:
            body += b"--" + boundary.encode() + crlf
            body += (
                'Content-Disposition: form-data; name="files"; '
                f'filename="{name}"'.encode()
            ) + crlf
            body += f"Content-Type: {ctype}".encode() + crlf + crlf
            body += data + crlf
        body += b"--" + boundary.encode() + b"--" + crlf
        req = urllib.request.Request(
            self._pat_url + path,
            data=bytes(body),
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            },
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


def detect_form_fields(template_text: str) -> list[str]:
    """Best-effort discovery of a form's prompt field names from its text.

    Winlink/Pat templates expose inputs in a few shapes; we scan for the common
    ones so the CLI/TUI can suggest fields to fill. Returns a de-duplicated,
    order-preserving list. Unknown layouts simply yield ``[]`` — the user can
    still supply any field the form asks for.
    """
    import re

    names: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"<(?:var|ask)\s+([A-Za-z0-9_]+)",            # <Var City> / <Ask Name>
        r'name\s*=\s*["\']([A-Za-z0-9_]+)["\']',       # HTML input name="city"
        r"\{([A-Za-z0-9_]+)\}",                         # {City} placeholders
    )
    for pat in patterns:
        for m in re.finditer(pat, template_text, flags=re.IGNORECASE):
            name = m.group(1)
            low = name.lower()
            if low not in seen:
                seen.add(low)
                names.append(name)
    return names


def _flatten_form_folder(node: object, prefix: str, out: list[dict]) -> None:
    """Flatten Pat's nested form-catalog tree into a flat list of forms.

    ``prefix`` is the accumulated folder path for ``node`` (the root's own name
    is omitted). Appends ``{"name","path","folder"}`` for each template found.
    """
    if not isinstance(node, dict):
        return
    for form in node.get("forms") or []:
        if not isinstance(form, dict):
            continue
        path = form.get("template_path")
        if not path:
            continue
        out.append({
            "name": str(form.get("name") or path),
            "path": str(path),
            "folder": prefix,
        })
    for sub in node.get("folders") or []:
        if not isinstance(sub, dict):
            continue
        sub_name = str(sub.get("name") or "")
        sub_prefix = f"{prefix}/{sub_name}".strip("/") if prefix else sub_name
        _flatten_form_folder(sub, sub_prefix, out)


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


def decode_winlink_body(raw: str) -> str:
    """Decode an inbound Winlink body that arrives as a raw MIME entity.

    Pat's ``Body`` is usually decoded plain text, but some messages (e.g. ones
    composed by Winlink Express with ``Content-Transfer-Encoding: base64`` or
    multipart bodies) come through as a raw MIME part — headers followed by an
    encoded payload — which is unreadable if shown verbatim. When the body looks
    like MIME we parse it with the stdlib ``email`` module and return the decoded
    text (base64/quoted-printable handled, ``text/plain`` preferred, HTML
    stripped as a fallback). Non-MIME bodies are returned unchanged.
    """
    if not raw or not _looks_like_mime(raw):
        return raw
    try:
        message = email.message_from_string(raw)
    except Exception:  # noqa: BLE001 - never let a parse error hide the mail
        return raw
    decoded = _extract_mime_text(message).strip()
    # A declared base64/utf-8 part that is actually garbage decodes to mojibake;
    # showing the original (at least partly legible) raw is better than that.
    if not decoded or _looks_garbled(decoded):
        return raw
    return decoded


def _looks_garbled(text: str) -> bool:
    """Heuristic: True when decoded text is mostly unreadable (mojibake).

    Triggers on the Unicode replacement char (from a failed decode) or when a
    large share of characters are control/non-printable bytes — the signature of
    base64 content that decoded to binary rather than real text.
    """
    if not text:
        return False
    if "\ufffd" in text:
        return True
    bad = sum(
        1 for ch in text
        if ch not in "\t\n\r" and (ord(ch) < 32 or ord(ch) == 127)
    )
    return bad > len(text) * 0.2


def _looks_like_mime(raw: str) -> bool:
    """True when ``raw`` begins with a MIME header block (Content-Type/-Encoding).

    Scans the leading header lines only: a real MIME entity starts with
    ``Key: value`` headers (or folded continuations) and includes a
    ``Content-Type`` or ``Content-Transfer-Encoding`` header. This keeps ordinary
    prose — even text that happens to mention "content-type" mid-paragraph — from
    being treated as MIME.
    """
    saw_content_header = False
    for line in raw.splitlines():
        if not line.strip():
            break  # blank line ends the header block
        low = line.lower()
        if low.startswith(("content-type:", "content-transfer-encoding:")):
            saw_content_header = True
        elif not (line[:1].isspace() or ":" in line):
            return False  # not a header line -> not a leading MIME header block
    return saw_content_header


def _extract_mime_text(message: email.message.Message) -> str:
    """Pull readable text out of a parsed MIME message.

    Prefers decoded ``text/plain`` parts; if none yield text, falls back to
    stripped ``text/html``.
    """
    plains: list[str] = []
    htmls: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plains.append(_part_text(part))
        elif ctype == "text/html":
            htmls.append(_part_text(part))
    text = "\n".join(p for p in plains if p).strip()
    if text:
        return text
    html = "\n".join(h for h in htmls if h).strip()
    return _strip_html(html) if html else ""


def _part_text(part: email.message.Message) -> str:
    """Decode a single non-multipart MIME part to text (handles base64/QP)."""
    payload = part.get_payload(decode=True)
    if payload is None:
        body = part.get_payload()
        return body if isinstance(body, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", "replace")


def _strip_html(html: str) -> str:
    """Crude HTML-to-text fallback: drop scripts/styles/tags, unescape entities."""
    import html as html_lib

    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()

