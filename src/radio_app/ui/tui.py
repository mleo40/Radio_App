"""Textual terminal UI (TUI) for Radio_App — a single pane of glass.

A persistent **mode selector** (top bar) is the primary control: each configured
transport is one operating **mode** with its own workspace, plus utility views —
**Watch**, **Health** and **Logs**. Selecting a mode re-skins the workspace and
binds sending to that transport. See DESIGN.md for the full model.

Surfaces:
* OPERATING MODE (interact): pick a transport; its contacts/conversations show
  and sending is bound to it. Capability detail (identity, encrypted/plaintext)
  appears in the status bar.
* WATCH (observe): a unified, read-only live stream of ALL messages — both
  received and the ones you send — from every transport, regardless of the
  active mode. Selecting an item opens that conversation and switches the active
  mode to its transport.
* HEALTH (verify): passive per-transport reachability probes (no transmission) —
  "can we reach rnsd / the JS8Call API / the modem socket right now?".
* LOGS (diagnose): a live, level-filterable view of the in-memory application
  log (follow/pause). A WARN/ERR badge in the status bar flags new problems and
  jumps here when clicked.
* CHATS (recall): the "All chats" archive — every conversation across all
  modes, newest first, read-only. A Mode button filters to a single transport;
  Enter on a row opens that conversation (switching to its mode).

Mode selector shows a health dot per mode: ● up · ○ down · · n/a · ◌ unknown.

Keys:  F3 = cycle mode   F4 = Fav-only (Stream + every mode)
       F5 = cycle Stream/Health/History/Favorites/Logs   Ctrl+R = refresh
       Ctrl+F = search history   Ctrl+C / Ctrl+Q / q = quit
Composer commands:  /to <callsign|@GROUP>,  /monitor,  /fav add|rm|list|only,
  /favorites,  /logs,  /loglevel <level>,  /search <text>,  /chats,
  /browse <hash>[:/page/x.mu],  /nodes,  /peers,  /help,  /quit
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from datetime import datetime, timedelta

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Select,
    Static,
    TextArea,
)

from .._compat import UTC
from ..app import App as CoreApp
from ..config import Config
from ..core.favorites import Favorite
from ..core.message import AddressType, DeliveryStatus, UnifiedMessage
from ..core.micron import parse_address, render_micron
from ..core.station import Station
from ..transports.base import ReachabilityStatus, Transport
from ..core.maidenhead import grid_to_latlon
from ..core.wx_internet import fetch_weather
from ..transports.js8call_transport import (
    JS8_BAND_DIAL_HZ,
    band_for_freq,
    dial_for_band,
)
from .about import ABOUT_MD

# Default cap for the in-memory Watch scrollback (rows). Overridable via
# [ui].watch_buffer_limit; see RadioTUI.__init__ / on_mount.
_WATCH_BUFFER_DEFAULT = 1000


# Commands that work in every mode (they drive the UI/state, not a transport).
# Shown by /help under "Everywhere" regardless of the active mode.
_UNIVERSAL_COMMAND_HELP = (
    "/to <callsign|@GROUP> [message], /monitor (Watch), "
    "/favorites (F6), /fav add|rm|list|only [<id> [label]], "
    "/mode (cycle, F3), /refresh, /logs, "
    "/loglevel <debug|info|warning|error>, /search <text> (Ctrl+F), "
    "/chats, "
    "/tmpl [list|<name>|add <n> <text>|del <n>], "
    "/bands [<band>|activity [<band>]], "
    "/bandscan <bands> <dwell_min> — JS8Call propagation probe, "
    "/sched [list|cancel <id>|band <band> <time> [daily]|+Nm|HH:MM [text]], "
    "/subs [add|rm @GROUP], "
    "/groups [@NAME|new|delete|add|rm|tag|untag], "
    "/contacts [<name>|new|delete|link|unlink|rename], "
    "/bridge [list], "
    "/filters [add|edit|del|mv] ..., "
    "/position [<grid>|clear], "
    "/roster [Nh], /name <friendly name>, /close [<id>], "
    "/start [transport], "
    "/net open <name> | ci [<call>] [note] | list | close | status | sessions, "
    "/wx [grid], "
    "/help, /quit"
)

_CALLSIGN_RE = __import__("re").compile(
    r"^[A-Z]{1,2}[0-9][A-Z]{1,3}$|^[A-Z]{1,2}[0-9][A-Z]{0,3}/[A-Z0-9]+$",
    __import__("re").IGNORECASE,
)


def _looks_like_callsign(token: str) -> bool:
    """True if *token* looks like an amateur callsign (e.g. KE0XYZ, W1AW)."""
    return bool(_CALLSIGN_RE.match(token.strip()))

# Mode-specific commands, keyed by transport name. /help shows only the active
# mode's group (plus the universal ones), so each panel lists what's usable
# there instead of the whole command surface. Each command is gated in
# _handle_command's handlers, so this map mirrors that gating.
_MODE_COMMAND_HELP: dict[str, tuple[str, ...]] = {
    "winlink": (
        "\u2709 Compose button \u2014 full email editor with multi-line body + templates",
        "/subject <text> \u2014 set the email subject for the next message",
        "type \\n in the body \u2014 inserts a line break (quick multi-line)",
        "/attach <path> \u2014 queue a file attachment (/attach clear empties)",
        "/save \u2014 save attachments from the open message",
        "/connect [gateway] \u2014 open a forwarding session (send + receive)",
        "/gateway <CALL> \u2014 set the RMS gateway; /gateways lists configured",
    ),
    "js8call": (
        "/freq [<MHz|Hz>] \u2014 show or set the dial frequency",
        "/band [<name>] \u2014 list bands or switch (e.g. /band 20m)",
        "/inbox \u2014 list JS8Call store-and-forward messages",
        "/relay <CALL> <text> \u2014 leave a store-and-forward message",
        "/sms <phone> <text> \u2014 send an APRS SMS",
        "/cmd [<CALL>] <SNR?|GRID?|...> \u2014 send a directed query",
    ),
    "reticulum": (
        "/whoami \u2014 show your Reticulum address",
        "/announce \u2014 re-announce your LXMF identity",
        "/path [<id>] \u2014 request a network path to a contact",
        "/peers \u2014 list messageable LXMF peers",
        "/nodes \u2014 list discovered NomadNet nodes",
        "/browse <hash>[:/page/x.mu] \u2014 open a NomadNet page",
        "/attach <path> \u2014 queue a file attachment (/attach clear empties)",
    ),
    "meshcore": (
        "/announce \u2014 broadcast a node advert",
        "/channel list|add <index> <#name> [secret] \u2014 manage channels",
    ),
}


# ---------------------------------------------------------------------------
# Winlink email templates
# ---------------------------------------------------------------------------

class _WLTemplate:
    """A named email template with optional subject/body placeholders."""

    def __init__(self, name: str, subject: str, body: str) -> None:
        self.name = name
        self.subject = subject
        self.body = body


def _substitute_wl_template(text: str, callsign: str, date_utc: str, time_utc: str) -> str:
    """Replace {callsign}, {date_utc}, {time_utc} placeholders in a template."""
    return (
        text
        .replace("{callsign}", callsign or "N0CALL")
        .replace("{date_utc}", date_utc)
        .replace("{time_utc}", time_utc)
    )


_WL_TEMPLATES: list[_WLTemplate] = [
    _WLTemplate("(blank)", "", ""),
    _WLTemplate(
        "ARRL Radiogram",
        "ARRL RADIOGRAM — {callsign}",
        (
            "ARRL RADIOGRAM\n\n"
            "Precedence: R (Routine)\n"
            "Handling Instructions: \n"
            "Station of Origin: {callsign}\n"
            "Check: \n"
            "Place of Origin: \n"
            "Time Filed: {time_utc} UTC\n"
            "Date: {date_utc}\n\n"
            "TO: \n"
            "STREET: \n"
            "CITY: \n"
            "STATE/ZIP: \n"
            "PHONE: \n\n"
            "MESSAGE:\n\n\n\n"
            "End of message. Please confirm receipt. 73 de {callsign}"
        ),
    ),
    _WLTemplate(
        "Health & Welfare",
        "Health & Welfare — {callsign}",
        (
            "HEALTH & WELFARE MESSAGE\n\n"
            "From: {callsign}\n"
            "Date/Time: {date_utc} {time_utc} UTC\n\n"
            "This message certifies the station below is safe and well.\n\n"
            "Station: \n"
            "Location: \n"
            "Grid Square: \n\n"
            "Status: All is well. No special needs at this time.\n\n"
            "Please relay to the addressee if possible.\n\n"
            "73 de {callsign}"
        ),
    ),
    _WLTemplate(
        "Activity Report",
        "Station Activity Report — {callsign} {date_utc}",
        (
            "STATION ACTIVITY REPORT\n\n"
            "Station: {callsign}\n"
            "Date/Time: {date_utc} {time_utc} UTC\n"
            "Location: \n"
            "Grid Square: \n\n"
            "Bands / Modes Active: \n"
            "Traffic Handled: \n"
            "Stations Worked: \n\n"
            "Status: Operational\n\n"
            "Comments:\n\n\n"
            "73 de {callsign}"
        ),
    ),
    _WLTemplate(
        "EmComm Spot Report",
        "EmComm Spot Report — {callsign} {time_utc}Z",
        (
            "EMCOMM SPOT REPORT\n\n"
            "From: {callsign}\n"
            "Date/Time: {date_utc} {time_utc} UTC\n"
            "Location: \n"
            "Grid Square: \n\n"
            "SITUATION:\n\n\n"
            "RESOURCES NEEDED:\n\n\n"
            "RESOURCES AVAILABLE:\n\n\n"
            "CASUALTIES: \n\n"
            "INFRASTRUCTURE STATUS:\n\n\n"
            "PRIORITY TRAFFIC:\n\n\n"
            "Next scheduled contact: \n\n"
            "73 de {callsign}"
        ),
    ),
]


def _cache_age(when: datetime | None) -> str:
    """Coarse human age (``3m``/``5h``/``2d``) for a cached NomadNet page."""
    if when is None:
        return "?"
    secs = max(0.0, (datetime.now(UTC) - when).total_seconds())
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs / 60:.0f}m"
    if secs < 172800:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


def _parse_freq_to_hz(text: str) -> int | None:
    """Parse a user-typed frequency into Hz.

    Accepts MHz (``14.078``) or raw Hz (``14078000``); a trailing ``hz``/``mhz``
    unit is tolerated. Values below 100000 are treated as MHz, the rest as Hz.
    Returns None on anything unparseable.
    """
    s = text.strip().lower().replace(",", "")
    for unit in ("mhz", "hz"):
        if s.endswith(unit):
            s = s[: -len(unit)].strip()
            break
    try:
        val = float(s)
    except ValueError:
        return None
    if val <= 0:
        return None
    return int(round(val * 1_000_000)) if val < 100_000 else int(val)


class Incoming(Message):
    """Posted to the UI thread when the router accepts an inbound message."""

    def __init__(self, msg: UnifiedMessage, action: str) -> None:
        super().__init__()
        self.msg = msg
        self.action = action


class SetupScreen(ModalScreen[dict | None]):
    """First-run modal: capture callsign + grid square.

    The radio is driven by the transport app (JS8Call), so there is no rig
    selection here. Values may be pre-filled from JS8Call when it is reachable.
    """

    CSS = """
    SetupScreen { align: center middle; }
    #setup-box {
        width: 64; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #setup-box Input { margin-bottom: 1; }
    #setup-buttons { height: auto; align-horizontal: right; }
    """

    def __init__(self, station: Station, source_note: str = "",
                 download_dir: str = "") -> None:
        super().__init__()
        self._station = station
        self._source_note = source_note
        self._download_dir = download_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-box"):
            intro = "[b]Station setup[/b]\nUsed on HF only; Reticulum stays anonymous."
            if self._source_note:
                intro += f"\n[dim]{self._source_note}[/dim]"
            yield Static(intro)
            yield Input(
                value=self._station.callsign,
                placeholder="Callsign (e.g. N0CALL)",
                id="s-call",
            )
            yield Input(
                value=self._station.grid_square,
                placeholder="Grid square (e.g. FN31pr)",
                id="s-grid",
            )
            yield Static(
                "Where to save downloaded files (attachments, etc.) — used by "
                "every mode:",
                id="s-download-label",
            )
            yield Input(
                value=self._download_dir,
                placeholder="Download folder (blank = default app data dir)",
                id="s-download",
            )
            yield Static("", id="setup-error")
            with Horizontal(id="setup-buttons"):
                yield Button("Skip", id="s-skip", variant="default")
                yield Button("Save", id="s-save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "s-skip":
            self.dismiss(None)
            return
        station = Station(
            callsign=self.query_one("#s-call", Input).value,
            grid_square=self.query_one("#s-grid", Input).value,
        )
        problems = station.validate()
        if problems:
            self.query_one("#setup-error", Static).update(
                "[red]" + "; ".join(problems) + "[/red]"
            )
            return
        self.dismiss(
            {
                "callsign": station.callsign,
                "grid_square": station.grid_square,
                "download_dir": self.query_one("#s-download", Input).value.strip(),
            }
        )


class ConfirmEncryptScreen(ModalScreen[bool]):
    """Explicit, unmistakable confirmation before encrypted HF transmission."""

    CSS = """
    ConfirmEncryptScreen { align: center middle; }
    #warn-box {
        width: 72; height: auto; padding: 1 2;
        border: thick $error; background: $surface;
    }
    #warn-buttons { height: auto; align-horizontal: right; }
    """

    def __init__(self, warning: str) -> None:
        super().__init__()
        self._warning = warning

    def compose(self) -> ComposeResult:
        with Vertical(id="warn-box"):
            yield Static(f"[b red]{self._warning}[/b red]")
            with Horizontal(id="warn-buttons"):
                yield Button("Cancel", id="c-no", variant="primary")
                yield Button("I ACCEPT - transmit", id="c-yes", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "c-yes")



class PasswordPromptScreen(ModalScreen[str | None]):
    """Masked, per-session password prompt (e.g. Winlink secure login).

    The value is returned to the caller via ``dismiss`` and is never written to
    disk or config — it lives only for the duration of the session that asked
    for it. Submitting an empty field (or Cancel/Esc) declines the prompt.
    """

    CSS = """
    PasswordPromptScreen { align: center middle; }
    #pw-box {
        width: 64; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #pw-msg { height: auto; }
    #pw-hint { height: auto; color: $text-muted; }
    #pw-input { height: 3; }
    #pw-buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message or "Enter password"

    def compose(self) -> ComposeResult:
        with Vertical(id="pw-box"):
            yield Static(f"[b]{self._message}[/b]", id="pw-msg")
            yield Static(
                "[dim]Used for this session only — never saved to disk.[/dim]",
                id="pw-hint",
            )
            yield Input(password=True, id="pw-input")
            with Horizontal(id="pw-buttons"):
                yield Button("Cancel", id="pw-cancel")
                yield Button("Send", id="pw-ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#pw-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pw-ok":
            self.dismiss(self.query_one("#pw-input", Input).value or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)



class BrowseScreen(ModalScreen[None]):
    """Read-only NomadNet page viewer with link navigation + history."""

    CSS = """
    BrowseScreen { align: center middle; }
    #browse-box {
        width: 90%; height: 90%; padding: 0 1;
        border: thick $primary; background: $surface;
    }
    #browse-addr { height: 1; color: $accent; }
    #browse-body { height: 1fr; border: solid $panel; padding: 0 1; }
    #browse-actions { height: 1; }
    #browse-status { width: 1fr; height: 1; color: $text-muted; }
    #browse-getlive { height: 1; min-width: 12; border: none; margin: 0 0 0 1; }
    #browse-input { height: 3; }
    """
    BINDINGS = [
        ("escape", "close", "Close"),
        ("ctrl+b", "back", "Back"),
        ("ctrl+r", "reload", "Reload"),
        ("ctrl+l", "get_live", "Get live"),
    ]

    def __init__(
        self,
        browser,
        dest: str,
        path: str = "/page/index.mu",
        fields: dict | None = None,
        prefer_cache: bool = False,
    ) -> None:
        super().__init__()
        self._browser = browser
        self._current = (dest, path, fields or {})
        self._history: list[tuple[str, str, dict]] = []
        self._links: list = []
        # When the stack is offline, serve cached snapshots directly instead of
        # waiting on a live fetch that is doomed to time out.
        self._prefer_cache = prefer_cache

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-box"):
            yield Static("", id="browse-addr")
            with VerticalScroll(id="browse-body"):
                yield Static("", id="browse-content", markup=True)
            with Horizontal(id="browse-actions"):
                yield Static("", id="browse-status")
                yield Button("\u21bb Get live", id="browse-getlive")
            yield Input(
                placeholder="link # to follow · <hash>:/page/x.mu · "
                "Ctrl+L live · Ctrl+B back · Ctrl+R reload · Esc close",
                id="browse-input",
            )

    def on_mount(self) -> None:
        self.query_one("#browse-input", Input).focus()
        self._load(*self._current, push=False)

    @work
    async def _load(
        self, dest: str, path: str, fields: dict, push: bool = True,
        force_live: bool = False,
    ) -> None:
        addr = self.query_one("#browse-addr", Static)
        status = self.query_one("#browse-status", Static)
        content = self.query_one("#browse-content", Static)
        loc = f"[b]{(dest or '?')[:16]}[/]:{path}"
        addr.update(f"[dim]\u2026 loading[/] {loc}")
        # Default browsing is cache-first: a page we've already seen is served
        # straight from the local cache (instant, zero traffic on the air), and
        # only an uncached page hits the network. The "Get live" button (Ctrl+L)
        # forces a fresh fetch when the operator actually wants the latest.
        # Offline mode (``_prefer_cache``) stays cache-only; dynamic pages
        # (field_data) can never be cached, so they always go live.
        prefer = self._prefer_cache and not force_live
        cache_first = not force_live and not prefer and not fields
        if force_live:
            status.update("loading (live)...")
        elif prefer:
            status.update("loading (cached)...")
        elif cache_first:
            status.update("loading (cache-first)...")
        else:
            status.update("loading...")
        res = await self._browser.fetch(
            dest, path, field_data=fields or None,
            prefer_cache=prefer, cache_first=cache_first,
        )
        getlive = self.query_one("#browse-getlive", Button)
        if not res.ok:
            addr.update(f"[b white on red] OFFLINE [/] {loc}")
            content.update(f"[red]Error:[/red] {res.error}")
            status.update("[red]failed[/] — no live link and no cached copy")
            getlive.display = False
            return
        # Canonicalise to the *resolved* full destination hash. The caller may
        # have passed a short prefix (node lists show truncated hashes; the
        # address bar accepts prefixes). If we kept that prefix as the page's
        # base, every relative link (`:/page/x.mu`) would inherit it and get
        # re-resolved against the live announce set on the next hop - which can
        # silently land on a *different* node that shares the prefix. Pinning to
        # the full hash keeps in-node links on the same node.
        dest = res.dest or dest
        loc = f"[b]{(dest or '?')[:16]}[/]:{path}"
        age = getattr(res, "fetched_at", None) if res.from_cache else None
        if res.from_cache:
            badge = (
                f"[b black on yellow] CACHED {_cache_age(age)} [/]"
                if age
                else "[b black on yellow] CACHED [/]"
            )
        else:
            badge = "[b black on green] LIVE [/]"
        addr.update(f"{badge} {loc}")
        page = render_micron(res.content, base_dest=dest)
        content.update(page.markup or "[dim](empty page)[/dim]")
        self._links = page.links
        if push and self._current != (dest, path, fields):
            self._history.append(self._current)
        self._current = (dest, path, fields)
        self.query_one("#browse-body", VerticalScroll).scroll_home(animate=False)
        nlinks = len(self._links)
        hint = " · type a number to follow" if nlinks else ""
        if res.from_cache:
            # Offer a one-press upgrade to the live page (only worthwhile online).
            getlive.display = self._browser.available
            stale = f" — fetched {_cache_age(age)} ago, may be stale" if age else ""
            live_hint = " · Ctrl+L for live" if self._browser.available else ""
            status.update(
                f"[yellow]\u25cf cached[/]{stale} · {nlinks} link(s){hint}{live_hint}"
            )
        else:
            getlive.display = False
            status.update(f"[green]\u25cf live[/] · {nlinks} link(s){hint}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse-getlive":
            event.stop()
            self.action_get_live()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.isdigit():
            self._follow(int(text))
            return
        low = text.lower()
        if low in ("b", "back"):
            self.action_back()
        elif low in ("r", "reload"):
            self.action_reload()
        elif low in ("l", "live"):
            self.action_get_live()
        elif low in ("q", "quit", "close"):
            self.dismiss(None)
        else:
            base = self._current[0]
            dest, path, fields = parse_address(text, base_dest=base)
            if not dest:
                self.query_one("#browse-status", Static).update("no node hash given")
                return
            self._load(dest, path, fields)

    def _follow(self, idx: int) -> None:
        if idx < 1 or idx > len(self._links):
            self.query_one("#browse-status", Static).update(f"no link #{idx}")
            return
        link = self._links[idx - 1]
        dest = link.resolve_dest(self._current[0])
        # If resolve_dest returned a short hex prefix (didn't match the current
        # node), consult the offline cache before going live. If the cache has a
        # unique entry for this prefix+path, its stored dest IS the full 32-char
        # hash — using it avoids "ambiguous prefix" errors when multiple live RNS
        # nodes happen to share the same leading hex digits.
        if (
            dest
            and len(dest) < 32
            and all(c in "0123456789abcdef" for c in dest.lower())
            and getattr(self._browser, "_cache", None) is not None
        ):
            hit = self._browser._cache.get(dest, link.path)
            if hit is not None:
                dest = hit.dest
        self._load(dest, link.path, link.fields)

    def action_back(self) -> None:
        if not self._history:
            self.query_one("#browse-status", Static).update("no history")
            return
        dest, path, fields = self._history.pop()
        self._load(dest, path, fields, push=False)

    def action_reload(self) -> None:
        self._load(*self._current, push=False)

    def action_get_live(self) -> None:
        """Force a live fetch of the current page, bypassing the cache."""
        self._load(*self._current, push=False, force_live=True)

    def action_close(self) -> None:
        self.dismiss(None)


class WinlinkFormsScreen(ModalScreen["str | None"]):
    """Picker for an installed Winlink form template.

    Lists the flattened form catalog (folder/name) and returns the selected
    template ``path`` (or ``None`` if cancelled). A small filter box narrows long
    catalogs by substring.
    """

    CSS = """
    WinlinkFormsScreen { align: center middle; }
    #wlf-box {
        width: 80; height: 80%; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #wlf-title { height: 1; }
    #wlf-filter { height: 3; margin-bottom: 1; }
    #wlf-list { height: 1fr; border: solid $panel; }
    #wlf-hint { height: 1; color: $text-muted; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, forms: list[dict]) -> None:
        super().__init__()
        self._forms = forms
        self._visible: list[dict] = list(forms)

    def compose(self) -> ComposeResult:
        with Vertical(id="wlf-box"):
            yield Static(
                f"[b]Winlink forms[/b] ({len(self._forms)} installed)",
                id="wlf-title",
            )
            yield Input(placeholder="filter (type to narrow)…", id="wlf-filter")
            yield ListView(id="wlf-list")
            yield Static(
                "Enter/tap a form to fill it · Esc to cancel", id="wlf-hint"
            )

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#wlf-filter", Input).focus()

    def _rebuild(self) -> None:
        lst = self.query_one("#wlf-list", ListView)
        lst.clear()
        for f in self._visible:
            folder = f.get("folder") or ""
            name = f.get("name") or f.get("path") or "?"
            label = f"{folder}/{name}" if folder else name
            lst.append(ListItem(Label(label)))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._visible = self.filter_forms(self._forms, event.value)
        self._rebuild()

    @staticmethod
    def filter_forms(forms: list[dict], term: str) -> list[dict]:
        """Forms whose name/folder/path contains ``term`` (case-insensitive)."""
        term = term.strip().lower()
        if not term:
            return list(forms)
        return [
            f
            for f in forms
            if term in (f.get("name") or "").lower()
            or term in (f.get("folder") or "").lower()
            or term in (f.get("path") or "").lower()
        ]

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the filter selects the only/first remaining match.
        if self._visible:
            self.dismiss(self._visible[0]["path"])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._visible):
            self.dismiss(self._visible[idx]["path"])

    def action_close(self) -> None:
        self.dismiss(None)


class WinlinkComposeFormScreen(ModalScreen["dict | None"]):
    """Fill in a Winlink form: per-field inputs + To/Cc/Subject overrides.

    Returns ``{"template", "responses", "to", "cc", "subject"}`` on submit (empty
    overrides are sent as ``None`` so the form's computed values win), or ``None``
    if cancelled. The actual build/queue (``compose_form``) is done by the app so
    this screen stays a pure data collector.
    """

    CSS = """
    WinlinkComposeFormScreen { align: center middle; }
    #wcf-box {
        width: 84; height: 90%; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #wcf-title { height: auto; margin-bottom: 1; }
    #wcf-fields { height: 1fr; }
    #wcf-fields Input { margin-bottom: 1; }
    #wcf-fields Static { color: $text-muted; }
    #wcf-error { height: auto; color: $error; }
    #wcf-buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, template_path: str, field_names: list[str]) -> None:
        super().__init__()
        self._template = template_path
        self._fields = field_names

    def compose(self) -> ComposeResult:
        with Vertical(id="wcf-box"):
            yield Static(
                f"[b]Compose form[/b]\n[dim]{self._template}[/dim]", id="wcf-title"
            )
            with VerticalScroll(id="wcf-fields"):
                yield Static("[b]Message[/b] (override the form's defaults)")
                yield Input(placeholder="To (address) — blank uses form default",
                            id="wcf-to")
                yield Input(placeholder="Cc — optional", id="wcf-cc")
                yield Input(placeholder="Subject — blank uses form default",
                            id="wcf-subject")
                if self._fields:
                    yield Static("[b]Fields[/b]")
                    for name in self._fields:
                        yield Input(placeholder=name, id=f"wcf-f-{name}")
                else:
                    yield Static(
                        "(No prompt fields detected — submit to build with the "
                        "form's defaults, or add fields the form asks for.)"
                    )
            yield Static("", id="wcf-error")
            with Horizontal(id="wcf-buttons"):
                yield Button("Cancel", id="wcf-cancel")
                yield Button("Queue to outbox", id="wcf-submit", variant="primary")

    def on_mount(self) -> None:
        try:
            self.query_one("#wcf-to", Input).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wcf-cancel":
            self.dismiss(None)
            return
        if event.button.id != "wcf-submit":
            return
        self.dismiss(self._build_result())

    def _build_result(self) -> dict:
        """Collect field responses + To/Cc/Subject overrides into a result dict.

        Empty fields are dropped (so the form's defaults stand); empty overrides
        become ``None`` so :meth:`compose_form` uses the form's computed values.
        """
        responses: dict[str, str] = {}
        for name in self._fields:
            val = self.query_one(f"#wcf-f-{name}", Input).value.strip()
            if val:
                responses[name] = val
        to = self.query_one("#wcf-to", Input).value.strip()
        cc = self.query_one("#wcf-cc", Input).value.strip()
        subject = self.query_one("#wcf-subject", Input).value.strip()
        return {
            "template": self._template,
            "responses": responses,
            "to": to or None,
            "cc": cc or None,
            "subject": subject or None,
        }

    def action_close(self) -> None:
        self.dismiss(None)


class WinlinkEmailComposeScreen(ModalScreen["dict | None"]):
    """Full-screen modal for composing a Winlink email.

    Replaces the single-line + ``\\n`` workaround with a proper multi-line
    body editor (TextArea) plus To/Cc/Subject fields and optional templates.

    Returns ``{"to", "cc", "subject", "body", "attachments"}`` on submit,
    ``{"action": "ics_forms"}`` when the user picks ICS Forms from the
    template picker, or ``None`` on cancel.
    """

    CSS = """
    WinlinkEmailComposeScreen { align: center middle; }
    #wecf-box {
        width: 84; height: 90%; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #wecf-title { height: auto; margin-bottom: 1; }
    #wecf-fields { height: 1fr; }
    #wecf-fields Input { margin-bottom: 1; }
    #wecf-fields Label { color: $text-muted; }
    #wecf-fields Select { margin-bottom: 1; }
    #wecf-body { height: 10; margin-bottom: 1; }
    #wecf-attach-row { height: auto; }
    #wecf-attach-label { width: 1fr; color: $text-muted; }
    #wecf-error { height: auto; color: $error; }
    #wecf-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    """
    BINDINGS = [("escape", "close", "Close")]

    _ICS_SENTINEL = "__ics_forms__"

    def __init__(
        self,
        subject: str = "",
        attachments: list[str] | None = None,
        callsign: str = "",
        date_utc: str = "",
        time_utc: str = "",
    ) -> None:
        super().__init__()
        self._init_subject = subject
        self._attachments: list[str] = list(attachments or [])
        self._callsign = callsign
        self._date_utc = date_utc
        self._time_utc = time_utc
        self._last_template_body = ""

    def compose(self) -> ComposeResult:
        options: list[tuple[str, str]] = [
            (t.name, str(i)) for i, t in enumerate(_WL_TEMPLATES)
        ]
        options.append(("ICS Forms →", self._ICS_SENTINEL))
        with Vertical(id="wecf-box"):
            yield Static("[b]✉ Compose Winlink Email[/b]", id="wecf-title")
            with VerticalScroll(id="wecf-fields"):
                yield Select(
                    options,
                    prompt="— template (optional) —",
                    id="wecf-template",
                    allow_blank=True,
                )
                yield Label("To:")
                yield Input(
                    id="wecf-to",
                    placeholder="W1AW  (callsign or email address)",
                )
                yield Label("Cc:")
                yield Input(id="wecf-cc", placeholder="optional")
                yield Label("Subject:")
                yield Input(id="wecf-subject", value=self._init_subject)
                yield Label("Body:")
                yield TextArea(id="wecf-body")
                yield Label("Attachments:")
                with Horizontal(id="wecf-attach-row"):
                    yield Static(self._attach_summary(), id="wecf-attach-label")
                    yield Button("Clear", id="wecf-attach-clear", classes="modebtn")
                yield Input(
                    id="wecf-attach-path",
                    placeholder="/path/to/attachment  (Enter to add)",
                )
            yield Static("", id="wecf-error")
            with Horizontal(id="wecf-buttons"):
                yield Button("Cancel", id="wecf-cancel")
                yield Button(
                    "Queue to outbox", id="wecf-queue", variant="primary"
                )

    def on_mount(self) -> None:
        self.query_one("#wecf-to", Input).focus()

    # -- template picker ------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        val = event.value
        if val is Select.BLANK:
            return
        if val == self._ICS_SENTINEL:
            self.dismiss({"action": "ics_forms"})
            return
        tmpl = _WL_TEMPLATES[int(val)]
        body_widget = self.query_one("#wecf-body", TextArea)
        current_body = body_widget.text
        if not current_body.strip() or current_body == self._last_template_body:
            body = _substitute_wl_template(
                tmpl.body, self._callsign, self._date_utc, self._time_utc
            )
            body_widget.load_text(body)
            self._last_template_body = body
        subj_widget = self.query_one("#wecf-subject", Input)
        if not subj_widget.value.strip() and tmpl.subject:
            subj_widget.value = _substitute_wl_template(
                tmpl.subject, self._callsign, self._date_utc, self._time_utc
            )

    # -- attachments ----------------------------------------------------------

    def _attach_summary(self) -> str:
        if not self._attachments:
            return "[dim]\U0001f4ce[/dim] None"
        names = ", ".join(os.path.basename(p) for p in self._attachments)
        return f"[dim]\U0001f4ce[/dim] {len(self._attachments)}: {names}"

    def _queue_attachment(self, path: str) -> None:
        path = path.strip()
        if not path:
            return
        if not os.path.exists(path):
            self.query_one("#wecf-error", Static).update(
                f"File not found: {path}"
            )
            return
        self._attachments.append(path)
        self.query_one("#wecf-attach-label", Static).update(
            self._attach_summary()
        )
        self.query_one("#wecf-error", Static).update("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "wecf-attach-path":
            self._queue_attachment(event.input.value)
            event.input.value = ""

    # -- buttons --------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "wecf-cancel":
            self.dismiss(None)
        elif bid == "wecf-attach-clear":
            self._attachments = []
            self.query_one("#wecf-attach-label", Static).update(
                self._attach_summary()
            )
        elif bid == "wecf-queue":
            to = self.query_one("#wecf-to", Input).value.strip()
            if not to:
                self.query_one("#wecf-error", Static).update(
                    "To: address is required."
                )
                return
            attach_input = self.query_one("#wecf-attach-path", Input).value
            if attach_input.strip():
                self._queue_attachment(attach_input)
                if not os.path.exists(attach_input.strip()):
                    return
            self.dismiss(self._build_result())

    def _build_result(self) -> dict:
        return {
            "to": self.query_one("#wecf-to", Input).value.strip(),
            "cc": self.query_one("#wecf-cc", Input).value.strip() or None,
            "subject": self.query_one("#wecf-subject", Input).value.strip(),
            "body": self.query_one("#wecf-body", TextArea).text,
            "attachments": list(self._attachments),
        }

    def action_close(self) -> None:
        self.dismiss(None)


class WinlinkWXSubscribeScreen(ModalScreen[bool]):
    """Instructions for subscribing to NWS bulletins via Winlink.

    Dismisses with True if the user wants to open the compose modal to send
    the subscription request, False otherwise.
    """

    CSS = """
    WinlinkWXSubscribeScreen { align: center middle; }
    #wxsub-box {
        width: 76; height: auto; padding: 1 2;
        border: thick $accent; background: $surface;
    }
    #wxsub-title  { height: auto; margin-bottom: 1; }
    #wxsub-body   { height: auto; color: $text-muted; margin-bottom: 1; }
    #wxsub-hint   { height: auto; color: $text-muted; margin-bottom: 1; }
    #wxsub-btns   { height: auto; align-horizontal: right; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, grid: str = "") -> None:
        super().__init__()
        self._grid = grid.upper()[:6] if grid else ""

    def compose(self) -> ComposeResult:
        grid_hint = f" for grid {self._grid}" if self._grid else ""
        with Vertical(id="wxsub-box"):
            yield Static("[b]⛅ Subscribe to NWS Bulletins via Winlink[/b]", id="wxsub-title")
            yield Static(
                f"NWS distributes official weather bulletins to licensed amateur stations "
                f"via Winlink. To receive forecasts{grid_hint}, send a subscription request "
                f"to the NWS Winlink gateway:\n\n"
                f"  [b]To:[/b] NWS\n"
                f"  [b]Subject:[/b] SUBSCRIBE{' ' + self._grid[:4] if self._grid else ''}\n"
                f"  [b]Body:[/b] (leave blank)\n\n"
                f"Bulletins will arrive in your Winlink inbox during any connect session. "
                f"They are automatically tagged as weather data in this app.",
                id="wxsub-body",
            )
            yield Static(
                "[dim]Press 'Open Compose' to pre-fill the request, or Esc to cancel.[/dim]",
                id="wxsub-hint",
            )
            with Horizontal(id="wxsub-btns"):
                yield Button("Cancel", id="wxsub-cancel")
                yield Button("Open Compose →", id="wxsub-open", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wxsub-open":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class BandScanResultScreen(ModalScreen[bool]):
    """Show band-scan results and offer to switch to the best-performing band.

    Dismisses with True to switch, False to stay (caller restores the
    pre-scan band on decline).
    """

    CSS = """
    BandScanResultScreen { align: center middle; }
    #bscan-box {
        width: 60; height: auto; padding: 1 2;
        border: thick $accent; background: $surface;
    }
    #bscan-title { height: auto; margin-bottom: 1; }
    #bscan-body  { height: auto; color: $text-muted; margin-bottom: 1; }
    #bscan-btns  { height: auto; align-horizontal: right; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, results: list, best_band: str) -> None:
        super().__init__()
        self._results = results
        self._best = best_band

    def compose(self) -> ComposeResult:
        lines = []
        for r in self._results:
            snr = f", avg SNR {r.avg_snr:+.0f} dB" if r.avg_snr is not None else ""
            marker = " ← best" if r.band == self._best else ""
            lines.append(f"{r.band}: {r.heard_count} heard{snr}{marker}")
        with Vertical(id="bscan-box"):
            yield Static("[b]📡 Band Scan Results[/b]", id="bscan-title")
            yield Static("\n".join(lines), id="bscan-body")
            with Horizontal(id="bscan-btns"):
                yield Button("Stay", id="bscan-no")
                yield Button(
                    f"Switch to {self._best} →", id="bscan-yes", variant="primary"
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "bscan-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class WXSetupScreen(ModalScreen[dict | None]):
    """Weather setup: manage grid squares and passive radio source subscriptions.

    Grid squares are shown in a ListView (arrow keys to navigate, Remove to
    delete the highlighted entry).  The add field forces uppercase as you type.
    MeshCore and Reticulum checkboxes stay open until the user explicitly saves.

    Returns ``{"meshcore": bool, "reticulum": bool, "grids": str}`` on save,
    or ``None`` on cancel.
    """

    CSS = """
    WXSetupScreen { align: center middle; }
    #wxsetup-box {
        width: 72; height: auto; padding: 1 2;
        border: thick $accent; background: $surface;
    }
    #wxsetup-title      { height: auto; margin-bottom: 1; }
    #wxsetup-grids-l    { height: auto; }
    #wxsetup-grid-list  { height: 5; margin-bottom: 0; }
    #wxsetup-grid-rm    { height: 3; width: auto; margin-bottom: 1; }
    #wxsetup-add-row    { height: 3; margin-bottom: 1; }
    #wxsetup-grid-input { width: 1fr; }
    #wxsetup-grid-add   { width: 7; min-width: 7; }
    #wxsetup-sources-l  { height: auto; margin-top: 1; margin-bottom: 1; }
    #wxsetup-btns       { height: auto; align-horizontal: right; margin-top: 1; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        has_meshcore: bool = False,
        has_reticulum: bool = False,
        current_grids: str = "",
    ) -> None:
        super().__init__()
        self._has_mc = has_meshcore
        self._has_rns = has_reticulum
        self._grids: list[str] = [
            g.strip().upper()
            for g in current_grids.split(",")
            if g.strip()
        ]

    def compose(self) -> ComposeResult:
        mc_note = "" if self._has_mc else " [dim](when MeshCore connects)[/dim]"
        rns_note = "" if self._has_rns else " [dim](when Reticulum connects)[/dim]"
        with Vertical(id="wxsetup-box"):
            yield Static("[b]⛅ Weather Setup[/b]", id="wxsetup-title")
            yield Static(
                "Grid squares (↑↓ to select, Remove to delete):",
                id="wxsetup-grids-l",
            )
            yield ListView(id="wxsetup-grid-list")
            yield Button("Remove selected", id="wxsetup-grid-rm", variant="warning")
            with Horizontal(id="wxsetup-add-row"):
                yield Input(placeholder="FN42", id="wxsetup-grid-input", max_length=6)
                yield Button("+ Add", id="wxsetup-grid-add", variant="default")
            yield Static(
                "[b]Passive radio sources[/b]",
                id="wxsetup-sources-l",
            )
            yield Checkbox(
                f"MeshCore #weather channel{mc_note}",
                value=self._has_mc,
                id="wxsetup-mc-cb",
            )
            yield Checkbox(
                f"Reticulum #weather / #nws_alerts groups{rns_note}",
                value=self._has_rns,
                id="wxsetup-rns-cb",
            )
            with Horizontal(id="wxsetup-btns"):
                yield Button("Cancel", id="wxsetup-skip")
                yield Button("Save", id="wxsetup-both", variant="primary")

    def on_mount(self) -> None:
        lst = self.query_one("#wxsetup-grid-list", ListView)
        for grid in self._grids:
            lst.append(ListItem(Label(grid)))
        self.query_one("#wxsetup-grid-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "wxsetup-grid-input":
            upper = event.value.upper()
            if upper != event.value:
                event.input.value = upper

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "wxsetup-grid-input":
            self._do_add_grid()

    def _do_add_grid(self) -> None:
        inp = self.query_one("#wxsetup-grid-input", Input)
        grid = inp.value.strip().upper()
        if grid and grid not in self._grids:
            self._grids.append(grid)
            self.query_one("#wxsetup-grid-list", ListView).append(
                ListItem(Label(grid))
            )
        inp.value = ""
        inp.focus()

    def _build_result(self) -> dict:
        mc = self.query_one("#wxsetup-mc-cb", Checkbox).value
        rns = self.query_one("#wxsetup-rns-cb", Checkbox).value
        return {"meshcore": mc, "reticulum": rns, "grids": ",".join(self._grids)}

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "wxsetup-skip":
            self.dismiss(None)
        elif bid == "wxsetup-both":
            self.dismiss(self._build_result())
        elif bid == "wxsetup-grid-add":
            self._do_add_grid()
            event.stop()
        elif bid == "wxsetup-grid-rm":
            lst = self.query_one("#wxsetup-grid-list", ListView)
            idx = lst.index
            if idx is not None and 0 <= idx < len(self._grids):
                del self._grids[idx]
                if lst.highlighted_child is not None:
                    lst.highlighted_child.remove()
            event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen["dict | None"]):
    """Four-tab settings modal: Favorites, Weather, Backup, Database."""

    CSS = """
    SettingsScreen { align: center middle; }
    #settings-box {
        width: 76; height: auto; max-height: 95vh;
        padding: 1 2; border: thick $accent; background: $surface;
    }
    #settings-title     { height: auto; margin-bottom: 1; }
    #settings-tabs      { height: 3; }
    #settings-tabs Button {
        height: 1; border: none; padding: 0 2; margin: 0 1 0 0;
    }
    #settings-tabs Button.-active { text-style: bold reverse; }
    #settings-content   { height: auto; }
    /* Favorites pane */
    #stab-fav-pane      { height: auto; }
    #stab-fav-list      { height: 8; margin-bottom: 0; }
    #stab-fav-rm        { height: 3; width: auto; margin-bottom: 1; }
    #stab-fav-kinds     { height: 3; margin-bottom: 0; }
    #stab-fav-kinds Button { height: 1; min-width: 8; border: none; padding: 0 1; margin: 0 1 0 0; }
    #stab-fav-kinds Button.-active { text-style: bold reverse; }
    #stab-fav-add-row   { height: 3; margin-top: 1; }
    #stab-fav-id        { width: 1fr; }
    #stab-fav-label     { width: 20; }
    #stab-fav-add       { width: 7; min-width: 7; }
    /* Weather pane */
    #stab-wx-pane       { height: auto; }
    #stab-wx-list       { height: 5; margin-bottom: 0; }
    #stab-wx-rm         { height: 3; width: auto; margin-bottom: 1; }
    #stab-wx-add-row    { height: 3; margin-bottom: 1; }
    #stab-wx-grid-input { width: 1fr; }
    #stab-wx-grid-add   { width: 7; min-width: 7; }
    #stab-wx-sources-l  { height: auto; margin-top: 1; }
    /* Backup pane */
    #stab-bak-pane      { height: auto; }
    #stab-bak-status    { height: auto; margin-bottom: 1; color: $text-muted; }
    #stab-bak-btns      { height: 3; margin-bottom: 1; }
    #stab-bak-restore-l { height: auto; margin-bottom: 0; }
    #stab-bak-restore-row { height: 3; margin-bottom: 1; }
    #stab-bak-path      { width: 1fr; }
    #stab-bak-do        { width: 10; min-width: 10; }
    #stab-bak-log       { height: auto; color: $text-muted; }
    /* Database pane */
    #stab-db-pane       { height: auto; }
    #stab-db-stats      { height: auto; margin-bottom: 1; color: $text-muted; }
    #stab-db-vac-row    { height: 3; margin-bottom: 1; }
    #stab-db-vacuum     { width: auto; }
    #stab-db-prune-l    { height: auto; margin-bottom: 0; }
    #stab-db-prune-row  { height: 3; margin-bottom: 1; }
    #stab-db-days       { width: 8; }
    #stab-db-do         { width: 10; min-width: 10; }
    #stab-db-log        { height: auto; color: $text-muted; }
    /* Footer */
    #settings-footer    { height: auto; align-horizontal: right; margin-top: 1; }
    """

    BINDINGS = [("escape", "close_settings", "Close")]

    def __init__(self, core) -> None:
        super().__init__()
        self._core = core
        self._add_kind: str = "callsign"
        self._wx_grids: list[str] = [
            g.strip().upper()
            for g in str(core.config.ui.get("wx_grids", "") or "").split(",")
            if g.strip()
        ]
        self._has_mc = any(t.name == "meshcore" for t in core.transports)
        self._has_rns = any(t.name == "reticulum" for t in core.transports)

    def compose(self) -> ComposeResult:
        mc_note = "" if self._has_mc else " [dim](when MC connects)[/dim]"
        rns_note = "" if self._has_rns else " [dim](when RNS connects)[/dim]"
        with Vertical(id="settings-box"):
            yield Static("[b]⚙ Settings[/b]", id="settings-title")
            with Horizontal(id="settings-tabs"):
                yield Button("Favorites", id="stab-fav", classes="stab")
                yield Button("Weather", id="stab-wx", classes="stab")
                yield Button("Backup", id="stab-bak", classes="stab")
                yield Button("Database", id="stab-db", classes="stab")
            with ContentSwitcher(initial="stab-fav-pane", id="settings-content"):
                with Vertical(id="stab-fav-pane"):
                    yield Static("Saved contacts (arrow keys to select):")
                    yield ListView(id="stab-fav-list")
                    yield Button("Remove selected", id="stab-fav-rm", variant="warning")
                    yield Static("Type:")
                    with Horizontal(id="stab-fav-kinds"):
                        yield Button("User", id="stab-kind-user", classes="stab-kind")
                        yield Button("Group", id="stab-kind-group", classes="stab-kind")
                        yield Button("Room", id="stab-kind-room", classes="stab-kind")
                    with Horizontal(id="stab-fav-add-row"):
                        yield Input(
                            placeholder="ID (callsign / @group / #room)",
                            id="stab-fav-id",
                        )
                        yield Input(
                            placeholder="Label (opt.)",
                            id="stab-fav-label",
                            max_length=40,
                        )
                        yield Button("+ Add", id="stab-fav-add", variant="default")
                with Vertical(id="stab-wx-pane"):
                    yield Static("Grid squares (arrow keys to select):", id="stab-wx-l")
                    yield ListView(id="stab-wx-list")
                    yield Button("Remove selected", id="stab-wx-rm", variant="warning")
                    with Horizontal(id="stab-wx-add-row"):
                        yield Input(
                            placeholder="FN42",
                            id="stab-wx-grid-input",
                            max_length=6,
                        )
                        yield Button("+ Add", id="stab-wx-grid-add", variant="default")
                    yield Static(
                        "\n[b]Passive radio sources[/b]", id="stab-wx-sources-l"
                    )
                    yield Checkbox(
                        f"MeshCore #weather channel{mc_note}",
                        id="stab-wx-mc",
                    )
                    yield Checkbox(
                        f"Reticulum #weather / #nws_alerts{rns_note}",
                        id="stab-wx-rns",
                    )
                with Vertical(id="stab-bak-pane"):
                    yield Static("", id="stab-bak-status")
                    with Horizontal(id="stab-bak-btns"):
                        yield Button("Backup Now", id="stab-bak-backup", variant="primary")
                    yield Static("Restore from archive:", id="stab-bak-restore-l")
                    with Horizontal(id="stab-bak-restore-row"):
                        yield Input(
                            placeholder="/path/to/radio_app-backup-*.tar.gz",
                            id="stab-bak-path",
                        )
                        yield Button("Restore…", id="stab-bak-do", variant="warning")
                    yield Static("", id="stab-bak-log")
                with Vertical(id="stab-db-pane"):
                    yield Static("", id="stab-db-stats")
                    with Horizontal(id="stab-db-vac-row"):
                        yield Button("Vacuum Now", id="stab-db-vacuum", variant="primary")
                    yield Static("Prune messages older than:", id="stab-db-prune-l")
                    with Horizontal(id="stab-db-prune-row"):
                        yield Input(placeholder="90", id="stab-db-days", max_length=5)
                        yield Static(" days  ", id="stab-db-days-l")
                        yield Button("Prune…", id="stab-db-do", variant="warning")
                    yield Static("", id="stab-db-log")
            with Horizontal(id="settings-footer"):
                yield Button("Close", id="settings-close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#stab-fav", Button).add_class("-active")
        self.query_one("#stab-kind-user", Button).add_class("-active")
        self._refresh_fav_list()
        wx_lst = self.query_one("#stab-wx-list", ListView)
        for grid in self._wx_grids:
            wx_lst.append(ListItem(Label(grid)))
        self._refresh_backup_status()
        self._refresh_db_stats()

    def _refresh_fav_list(self) -> None:
        lst = self.query_one("#stab-fav-list", ListView)
        lst.clear()
        for fav in self._core.favorites.all():
            k = getattr(fav, "kind", "") or ""
            fid = fav.id or ""
            if k == "node":
                badge = "[dim]Node[/dim]"
            elif k == "mc_channel" or fid.startswith("#"):
                badge = "[dim]Room[/dim]"
            elif k == "group" or fid.startswith("@"):
                badge = "[dim]Grp [/dim]"
            elif k == "mc_peer":
                badge = "[dim]MC  [/dim]"
            else:
                badge = "[dim]User[/dim]"
            label_str = f'  "{fav.label}"' if fav.label else ""
            lst.append(ListItem(Label(f"{badge}  {fid}{label_str}")))

    def _refresh_backup_status(self) -> None:
        cfg_dir = self._core.config.path.parent
        backups = sorted(cfg_dir.glob("radio_app-backup-*.tar.gz"))
        status = self.query_one("#stab-bak-status", Static)
        if backups:
            latest = backups[-1]
            size_kb = latest.stat().st_size // 1024
            status.update(f"Last backup: {latest.name} ({size_kb} KB)")
        else:
            status.update("No backup found in config directory.")

    def _refresh_db_stats(self) -> None:
        try:
            s = self._core.store.stats()
            msgs = s.get("messages", 0)
            threads = s.get("threads", 0)
            size_kb = (s.get("size_bytes") or 0) // 1024
            size_str = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb} KB"
            self.query_one("#stab-db-stats", Static).update(
                f"{msgs:,} messages · {threads:,} threads · {size_str}"
            )
        except Exception:  # noqa: BLE001
            pass

    @work
    async def _do_vacuum(self) -> None:
        try:
            freed = self._core.store.vacuum()
            freed_kb = freed // 1024
            msg = f"✓ Vacuum done — freed {freed_kb} KB." if freed_kb else "✓ Vacuum done — nothing to reclaim."
            self._set_db_log(msg)
            self._refresh_db_stats()
        except Exception as exc:  # noqa: BLE001
            self._set_db_log(f"✗ Vacuum failed: {exc}")

    @work
    async def _do_prune(self, days: int) -> None:
        try:
            removed = self._core.store.purge_older_than(days)
            msg = f"✓ Pruned {removed} message(s) older than {days} days."
            self._set_db_log(msg)
            self._refresh_db_stats()
        except Exception as exc:  # noqa: BLE001
            self._set_db_log(f"✗ Prune failed: {exc}")

    def _trigger_prune(self) -> None:
        raw = self.query_one("#stab-db-days", Input).value.strip()
        if not raw.isdigit() or int(raw) <= 0:
            self._set_db_log("Enter a positive number of days.")
            return
        self._do_prune(int(raw))

    def _set_db_log(self, msg: str) -> None:
        try:
            self.query_one("#stab-db-log", Static).update(msg)
        except Exception:  # noqa: BLE001
            pass

    def _set_active_tab(self, tab_id: str) -> None:
        for tid in ("stab-fav", "stab-wx", "stab-bak", "stab-db"):
            try:
                self.query_one(f"#{tid}", Button).set_class(tid == tab_id, "-active")
            except Exception:  # noqa: BLE001
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in ("stab-fav-id", "stab-wx-grid-input"):
            upper = event.value.upper()
            if upper != event.value:
                event.input.value = upper

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("stab-fav-id", "stab-fav-label"):
            self._do_add_fav()
            event.stop()
        elif event.input.id == "stab-wx-grid-input":
            self._do_add_wx_grid()
            event.stop()
        elif event.input.id == "stab-db-days":
            self._trigger_prune()
            event.stop()

    def _do_add_fav(self) -> None:
        fid = self.query_one("#stab-fav-id", Input).value.strip()
        label = self.query_one("#stab-fav-label", Input).value.strip()
        if not fid:
            return
        if self._add_kind == "group" and not fid.startswith("@"):
            fid = f"@{fid}"
        elif self._add_kind == "mc_channel" and not fid.startswith("#"):
            fid = f"#{fid}"
        self._core.favorites.add(fid, label, kind=self._add_kind)
        self._core.favorites.save(self._core.config)
        self.query_one("#stab-fav-id", Input).value = ""
        self.query_one("#stab-fav-label", Input).value = ""
        self._refresh_fav_list()

    def _do_add_wx_grid(self) -> None:
        inp = self.query_one("#stab-wx-grid-input", Input)
        grid = inp.value.strip().upper()
        if grid and grid not in self._wx_grids:
            self._wx_grids.append(grid)
            self.query_one("#stab-wx-list", ListView).append(ListItem(Label(grid)))
        inp.value = ""
        inp.focus()

    def _build_wx_result(self) -> dict:
        mc = self.query_one("#stab-wx-mc", Checkbox).value
        rns = self.query_one("#stab-wx-rns", Checkbox).value
        return {"meshcore": mc, "reticulum": rns, "grids": ",".join(self._wx_grids)}

    @work(thread=True)
    def _do_backup(self) -> None:
        from ..core import backup as bk
        try:
            result = bk.create_backup(
                self._core.config.path,
                self._core.config.database_path(),
            )
            size_kb = result.size_bytes // 1024
            self.app.call_from_thread(
                self._set_bak_log,
                f"✓ Backup written: {result.path.name} ({size_kb} KB)",
            )
            self.app.call_from_thread(self._refresh_backup_status)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(
                self._set_bak_log, f"✗ Backup failed: {exc}"
            )

    @work(thread=True)
    def _do_restore(self, path_str: str) -> None:
        from pathlib import Path as _Path
        from ..core import backup as bk
        archive = _Path(path_str).expanduser()
        if not archive.exists():
            self.app.call_from_thread(
                self._set_bak_log, f"File not found: {path_str}"
            )
            return
        try:
            result = bk.restore_backup(
                archive,
                self._core.config.path,
                self._core.config.database_path(),
            )
            msg = (
                f"✓ Restored — config: {result.config_restored}, "
                f"db: {result.db_restored}. Restart the app to apply."
            )
            self.app.call_from_thread(self._set_bak_log, msg)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(
                self._set_bak_log, f"✗ Restore failed: {exc}"
            )

    def _set_bak_log(self, msg: str) -> None:
        try:
            self.query_one("#stab-bak-log", Static).update(msg)
        except Exception:  # noqa: BLE001
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        # Tab switching
        if bid == "stab-fav":
            self.query_one("#settings-content", ContentSwitcher).current = "stab-fav-pane"
            self._set_active_tab("stab-fav")
            event.stop()
        elif bid == "stab-wx":
            self.query_one("#settings-content", ContentSwitcher).current = "stab-wx-pane"
            self._set_active_tab("stab-wx")
            event.stop()
        elif bid == "stab-bak":
            self.query_one("#settings-content", ContentSwitcher).current = "stab-bak-pane"
            self._set_active_tab("stab-bak")
            event.stop()
        elif bid == "stab-db":
            self.query_one("#settings-content", ContentSwitcher).current = "stab-db-pane"
            self._set_active_tab("stab-db")
            event.stop()
        # Kind selector
        elif bid in ("stab-kind-user", "stab-kind-group", "stab-kind-room"):
            kind_map = {
                "stab-kind-user": "callsign",
                "stab-kind-group": "group",
                "stab-kind-room": "mc_channel",
            }
            self._add_kind = kind_map[bid]
            for k in kind_map:
                try:
                    self.query_one(f"#{k}", Button).set_class(k == bid, "-active")
                except Exception:  # noqa: BLE001
                    pass
            event.stop()
        # Favorites actions
        elif bid == "stab-fav-add":
            self._do_add_fav()
            event.stop()
        elif bid == "stab-fav-rm":
            lst = self.query_one("#stab-fav-list", ListView)
            idx = lst.index
            if idx is not None:
                favs = self._core.favorites.all()
                if 0 <= idx < len(favs):
                    self._core.favorites.remove(favs[idx].id)
                    self._core.favorites.save(self._core.config)
                if lst.highlighted_child is not None:
                    lst.highlighted_child.remove()
            event.stop()
        # WX grid actions
        elif bid == "stab-wx-grid-add":
            self._do_add_wx_grid()
            event.stop()
        elif bid == "stab-wx-rm":
            lst = self.query_one("#stab-wx-list", ListView)
            idx = lst.index
            if idx is not None and 0 <= idx < len(self._wx_grids):
                del self._wx_grids[idx]
                if lst.highlighted_child is not None:
                    lst.highlighted_child.remove()
            event.stop()
        # Backup actions
        elif bid == "stab-bak-backup":
            self._do_backup()
            event.stop()
        elif bid == "stab-bak-do":
            path_str = self.query_one("#stab-bak-path", Input).value.strip()
            if path_str:
                self._do_restore(path_str)
            else:
                self._set_bak_log("Enter a backup archive path first.")
            event.stop()
        # Database actions
        elif bid == "stab-db-vacuum":
            self._do_vacuum()
            event.stop()
        elif bid == "stab-db-do":
            self._trigger_prune()
            event.stop()
        # Close
        elif bid == "settings-close":
            self.action_close_settings()
            event.stop()

    def action_close_settings(self) -> None:
        self.dismiss({"wx": self._build_wx_result()})


class AboutScreen(ModalScreen[None]):
    """Hidden "about" easter egg: a scrollable technical overview of the app.

    Undocumented: opened by the Ctrl+G chord (suppressed from the footer) or by
    typing the magic word ``xyzzy`` into the composer. 73!
    """

    CSS = """
    AboutScreen { align: center middle; }
    #about-box {
        width: 86%; height: 90%; padding: 1 2;
        border: thick $accent; background: $surface;
    }
    #about-body { height: 1fr; }
    #about-hint { height: 1; color: $text-muted; text-align: center; }
    """
    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("enter", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="about-box"):
            with VerticalScroll(id="about-body"):
                yield Markdown(ABOUT_MD)
            yield Static("Esc / q to close · 73", id="about-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class LaunchCmdScreen(ModalScreen):
    """One-shot prompt to customise the launch command for a transport's backing app.

    Shown the first time the operator runs /start for a transport that has no
    ``launch_cmd`` in config.  Pressing Enter with an empty field accepts the
    default.  The result is the command string chosen (possibly the default), or
    None if the operator pressed Escape / Cancel.
    """

    CSS = """
    LaunchCmdScreen { align: center middle; }
    #lc-box {
        width: 72; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #lc-title { height: auto; }
    #lc-hint  { height: auto; color: $text-muted; }
    #lc-input { height: 3; }
    #lc-buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, transport_name: str, default_cmd: str) -> None:
        super().__init__()
        self._transport_name = transport_name
        self._default_cmd = default_cmd

    def compose(self) -> ComposeResult:
        with Vertical(id="lc-box"):
            yield Static(
                f"[b]Launch command for {self._transport_name}[/b]", id="lc-title"
            )
            yield Static(
                f"[dim]Default: {self._default_cmd!r}. "
                "Press Enter to accept, or type a custom path/flags.[/dim]",
                id="lc-hint",
            )
            yield Input(placeholder=self._default_cmd, id="lc-input")
            with Horizontal(id="lc-buttons"):
                yield Button("Cancel", id="lc-cancel")
                yield Button("Use this command", id="lc-ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#lc-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or self._default_cmd)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "lc-ok":
            val = self.query_one("#lc-input", Input).value.strip()
            self.dismiss(val or self._default_cmd)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RadioTUI(App):
    """The Textual application."""

    ENABLE_COMMAND_PALETTE = False

    # Transports whose per-message size cap is advisory rather than a hard
    # protocol limit. JS8Call has no documented character cap — it auto-frames
    # long text into successive transmissions — so we warn instead of blocking.
    _SOFT_LIMIT_TRANSPORTS = frozenset({"js8call"})

    CSS = """
    #modebar { height: 3; background: $boost; padding: 0 1; }
    #modebar Button {
        height: 1; min-width: 6; margin: 0; border: none;
        padding: 0 1;
    }
    #modebar Button.-active { text-style: bold reverse; }
    #modebar Button.-down { color: $text-muted; opacity: 40%; }
    #modebar #modebar-spacer { width: 1fr; height: 1; }
    #modebar #input-ind { width: auto; height: 1; color: $text-muted; padding: 0 1; }
    .-touch #modebar { height: 5; }
    .-touch #modebar Button { height: 3; min-width: 12; }
    #main { height: 1fr; }
    #active-view { height: 1fr; }
    #active-banner { height: auto; color: $warning; padding: 0 1; }
    #brand-banner { height: auto; color: $text-muted; padding: 0 1; display: none; }
    #mesh-bar { height: 1; padding: 0 1; display: none; }
    #mesh-bar Button { height: 1; min-width: 6; border: none; margin: 0 1 0 0; }
    #mesh-bar-label { width: auto; color: $accent; }
    #mesh-spacer { width: 1fr; }
    .-touch #mesh-bar { height: 3; }
    .-touch #mesh-bar Button { height: 3; min-width: 12; }
    #js8-bar { height: 1; padding: 0 1; display: none; }
    #js8-bar Button { height: 1; min-width: 5; border: none; margin: 0 1 0 0; }
    #js8-bar-label { width: auto; color: $accent; }
    #js8-spacer { width: 1fr; }
    .-touch #js8-bar { height: 3; }
    .-touch #js8-bar Button { height: 3; min-width: 8; }
    #js8-query-bar { height: 1; padding: 0 1; display: none; }
    #js8-query-bar Button { height: 1; min-width: 8; border: none; margin: 0 1 0 0; }
    #js8-query-label { width: auto; color: $accent; }
    #js8-query-spacer { width: 1fr; }
    .-touch #js8-query-bar { height: 3; }
    .-touch #js8-query-bar Button { height: 3; min-width: 10; }
    #winlink-bar { height: 1; padding: 0 1; display: none; }
    #winlink-bar Button { height: 1; min-width: 6; border: none; margin: 0 1 0 0; }
    #winlink-bar-label { width: auto; color: $accent; }
    #winlink-spacer { width: 1fr; }
    .-touch #winlink-bar { height: 3; }
    .-touch #winlink-bar Button { height: 3; min-width: 12; }
    #active-body { height: 1fr; }
    #threads { width: 32; border-right: solid $panel; }
    #right { width: 1fr; }
    #messages { height: 1fr; padding: 0 1; }
    #monitor-view { height: 1fr; }
    #monitor-ticker { height: 1; color: $accent; padding: 0 1; }
    #watch-bar { height: 1; }
    #monitor-help { height: 1; width: auto; color: $text-muted; padding: 0 1; }
    #watch-spacer { width: 1fr; height: 1; }
    #watch-bar Button { height: 1; min-width: 6; border: none; margin: 0 1 0 0; }
    #monitor { height: 1fr; }
    #nomadnet-view { height: 1fr; }
    #nomad-help { height: 1; color: $text-muted; padding: 0 1; }
    #nomad-bar { height: 1; padding: 0 1; }
    #nomad-bar Button { height: 1; min-width: 6; border: none; margin: 0 1 0 0; }
    #nomad-spacer { width: 1fr; height: 1; }
    #nomad-nodes { height: 1fr; }
    #health-view { height: 1fr; }
    #health-help { height: 1; color: $text-muted; padding: 0 1; }
    #health-body { height: 1fr; }
    #health-log { height: 1fr; width: 50%; padding: 0 1; }
    #health-sys-log { height: 1fr; width: 50%; padding: 0 1; border-left: tall $panel; }
    #logs-view { height: 1fr; }
    #logs-bar { height: 1; }
    #logs-help { height: 1; width: auto; color: $text-muted; padding: 0 1; }
    #logs-spacer { width: 1fr; height: 1; }
    #logs-bar Button { height: 1; min-width: 6; border: none; margin: 0 1 0 0; }
    #logs-log { height: 1fr; padding: 0 1; }
    #favorites-view { height: 1fr; }
    #fav-help { height: auto; color: $text-muted; padding: 0 1; }
    #fav-bar { height: 1; padding: 0 1; }
    #fav-spacer { width: 1fr; }
    #favorites-list { height: 1fr; }
    #search-view { height: 1fr; }
    #search-input { height: 3; margin: 0 1; }
    #search-help { height: 1; color: $text-muted; padding: 0 1; }
    #search-results { height: 1fr; }
    #archive-view { height: 1fr; }
    #archive-bar { height: 1; }
    #archive-help { height: 1; width: auto; color: $text-muted; padding: 0 1; }
    #archive-spacer { width: 1fr; height: 1; }
    #archive-bar Button { height: 1; min-width: 6; border: none; margin: 0 1 0 0; }
    #archive-list { height: 1fr; }
    #net-view { height: 1fr; }
    #net-bar { height: 1; }
    #net-status { height: 1; width: auto; color: $text-muted; padding: 0 1; }
    #net-spacer { width: 1fr; height: 1; }
    #net-bar Button { height: 1; min-width: 8; border: none; margin: 0 1 0 0; }
    #net-log { height: 1fr; padding: 0 1; }
    #weather-view { height: 1fr; }
    #wx-bar { height: 1; }
    #wx-grid-label { height: 1; width: auto; color: $accent; padding: 0 1 0 0; }
    #wx-grid-prev { height: 1; min-width: 3; border: none; margin: 0; }
    #wx-grid-next { height: 1; min-width: 3; border: none; margin: 0 1 0 0; }
    #wx-grid { width: 8; border: none; height: 1; }
    #wx-spacer { width: 1fr; height: 1; }
    #wx-bar Button { height: 1; min-width: 6; border: none; margin: 0 1 0 0; }
    #wx-log { height: 1fr; padding: 0 1; }
    #statusbar { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    #composer { height: 3; }
    """
    # Quit is wired in two ways so it always works:
    #   - Ctrl+C / Ctrl+Q are priority bindings, so they fire even when an
    #     Input widget (composer or NomadNet address bar) currently has focus.
    #     Without priority, Input swallows the key and quit silently fails in
    #     chat/NomadNet modes.
    #   - 'q' is a normal binding for the read-only views (Watch / Health),
    #     where the composer is disabled and a single keystroke is faster.
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("q", "quit", "Quit"),
        ("f3", "choose_mode", "Next mode"),
        ("f4", "toggle_fav_only", "Fav-only"),
        ("f5", "cycle_utility", "Stream/Health…"),
        Binding("ctrl+l", "logs", "Logs", show=False, priority=True),
        ("f", "toggle_nomad_favorite", "Save node"),
        ("s", "sync_nomad", "Sync favs"),
        ("g", "cycle_watch_group", "Group filter"),
        ("i", "identity", "My address"),
        ("ctrl+n", "announce", "Announce"),
        Binding("ctrl+p", "settings", "Settings", show=True, priority=True),
        Binding("ctrl+w", "close_chat", "Close chat", priority=True),
        ("delete", "remove_favorite", "Remove fav"),
        Binding("ctrl+d", "remove_favorite", "Remove fav", priority=True),
        ("ctrl+r", "refresh", "Refresh"),
        # Full-text history search palette. Priority so it fires even while the
        # composer (or another Input) has focus.
        Binding("ctrl+f", "search", "Search", priority=True),
        Binding("escape", "deselect_chat", "All messages", show=False, priority=True),
        Binding("escape", "close_search", "Close search", show=False, priority=True),
        # Hidden easter egg: technical "about" overview. show=False keeps it out
        # of the footer; priority lets it fire even while the composer is focused.
        Binding("ctrl+g", "about", "About", show=False, priority=True),
    ]

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        """Gate context-specific bindings (and hide them from the footer).

        The favorites-only filter (F4) applies to the Watch feed *and* to each
        operating mode: a chat mode's conversation list, plus the NomadNet node
        list. It is enabled/shown on those and hidden on the read-only
        Health/Favorites surfaces. Returning ``False`` both disables the key and
        removes it from the footer; ``True`` is the default for every other
        action.
        """
        if action == "toggle_fav_only":
            return self.view in ("monitor", "active", "nomadnet")
        # Reticulum-only tools: only meaningful in the Reticulum chat mode.
        if action == "identity":
            return self.view == "active" and self.active_transport == "reticulum"
        # Announce is available in both Reticulum (LXMF announce) and MeshCore
        # (node advert) modes.
        if action == "announce":
            return self.view == "active" and self.active_transport in (
                "reticulum",
                "meshcore",
            )
        # Close-chat only applies when a conversation is open in a chat mode.
        if action == "close_chat":
            return self.view == "active" and self.current_target is not None
        # Sync favorite NomadNet pages: only on the NomadNet surface.
        if action == "sync_nomad":
            return self.view == "nomadnet"
        # Cycle the Watch stream group filter: only on the Watch surface.
        if action == "cycle_watch_group":
            return self.view == "monitor"
        # Escape deselects the open chat (returns to full feed, keeps history).
        # Don't steal Escape from modal screens — let them handle it themselves.
        if action == "deselect_chat":
            if len(self.screen_stack) > 1:
                return False
            return self.view == "active" and self.current_target is not None
        # Escape only closes the search palette while it's open (otherwise let
        # the key pass through to focused widgets).
        if action == "close_search":
            return self.view == "search"
        return True

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__()
        self._config_path = config_path
        self.core: CoreApp | None = None
        self.active_transport: str | None = None
        self.current_target: str | None = None
        self.view: str = "active"
        self._thread_keys: list[str] = []
        # Conversations the operator has opened in each mode, kept so the left
        # pane stays stable when you leave a mode and come back — even for
        # conversations with no stored messages yet. transport -> ordered keys.
        self._opened_threads: dict[str, list[str]] = {}
        self._monitor_entries: list[tuple[str, str]] = []
        # Bounded in-memory scrollback of non-announce Watch messages so the list
        # can be rebuilt when a filter is toggled. The Watch feed is NOT stored
        # history (that lives in the database) — capping it keeps a long session
        # on a busy band from growing memory without limit. The maxlen is set
        # from [ui].watch_buffer_limit once config loads (on_mount); the default
        # here covers the pre-config window and tests.
        self._monitor_msgs: deque[UnifiedMessage] = deque(
            maxlen=_WATCH_BUFFER_DEFAULT
        )
        # Effective row cap for the master buffer AND the rendered list (0 =
        # unbounded). Set from [ui].watch_buffer_limit in on_mount.
        self._watch_buffer_limit = _WATCH_BUFFER_DEFAULT
        self._monitor_fav_only = False
        # Watch stream group filter: when set to a group name, the feed is
        # restricted to that group's cross-mode traffic. Mutually exclusive with
        # the favorites-only filter (selecting one clears the other).
        self._monitor_group_filter: str | None = None
        # Per-mode favorites-only filter: when on, the active mode's thread list
        # is restricted to conversations with favorite peers (F4 in chat modes).
        self._active_fav_only = False
        self._encrypt_approved = False
        # Documented per-message size cap (bytes) for the active mode's
        # transport; drives the composer guard + live counter. 0 = no limit.
        self._compose_limit: int = 0
        self._theme_ready = False
        # Per-transport announce telemetry. Announces are intentionally NOT
        # rendered as Monitor rows (too noisy), but we keep a rolling count and
        # last-heard timestamp so the status bar can prove the transport is
        # live and hearing the network.
        self._announce_stats: dict[str, dict] = {}
        # Per-transport traffic telemetry (kind='traffic'). Like announces these
        # are not rendered as Monitor rows; we keep a rolling count + last-heard
        # timestamp so the status bar can prove a transport is actively hearing
        # the band (e.g. JS8 RX frames) even when frames carry no text.
        self._traffic_stats: dict[str, dict] = {}
        # Running count of delivered (non-telemetry) messages per transport,
        # for the Health board's "traffic volume" indicator.
        self._msg_counts: dict[str, int] = {}
        # Bounded history of recent FAVORITE peer announces (any transport)
        # that powers the Monitor view's "Favorites" ticker. Unknown peers are
        # not tracked here - the bell only rings for marked favorites.
        self._fav_recent: deque[tuple[datetime, str, str]] = deque(maxlen=8)
        # Latest reachability status per transport name, feeding the mode
        # selector's health dots. Updated by a passive timer probe (no TX).
        self._health: dict[str, ReachabilityStatus] = {}
        # Latest device telemetry per transport that exposes it (e.g. MeshCore
        # battery + radio params), shown on the Health board. Updated by the
        # same passive probe timer as the reachability dots.
        self._device_telemetry: dict[str, dict] = {}
        # Time-consensus state: best reading from GPS → local NTP → internet NTP →
        # system. Updated in _refresh_health (thread); read by _render_system_health.
        self._time_reading = None  # TimeReading | None
        self._time_queried: bool = False
        self._last_time_check: float = -999.0
        # HF propagation conditions fetched once at launch via _fetch_propagation().
        self._solar_data = None   # PropagationData | None
        self._solar_fetched: bool = False
        # Cached GPS/config position for the Health board display.
        self._position = None
        # Last input method seen ("key" or "pointer"), shown as a glyph and used
        # to offer a touch-friendly (larger) layout.
        self._input_mode = "key"
        self._touch_layout = False
        # Watch surface: when paused, new rows are buffered, not rendered live.
        self._watch_paused = False
        # Watch surface: sort the feed by mode (transport) instead of time.
        self._watch_sort_by_mode = False
        # Discovery feed for Health mode: identity -> {label, transport, ts}.
        # NomadNet node list row indices -> node dict.
        self._nomad_nodes: list[dict] = []
        # Favorites view row indices -> favorite id ("" for section headers).
        self._fav_keys: list[str] = []
        # Outbound delivery state per message id (e.g. Reticulum LXMF receipts):
        # msg_id -> "delivered" | "failed". Used to annotate sent messages with a
        # delivery indicator in the conversation log.
        self._delivery_status: dict[str, str] = {}
        # Pending subject line for the next Winlink message (set via the Subject
        # button or /subject). Cleared after a Winlink send consumes it.
        self._winlink_subject: str = ""
        # Pending outbound attachment file paths for the next message (set via
        # /attach). Works on any transport whose capabilities advertise
        # supports_attachments (Winlink email, Reticulum LXMF). Cleared after a
        # send consumes them.
        self._attach_queue: list[str] = []
        # Latest per-path probe for the Winlink transport (telnet/varahf/ardop
        # endpoint up/down), shown under its line on the Health board.
        self._winlink_paths: list[dict] = []
        # Search palette: row index -> (thread_key, transport) for opening a hit,
        # plus the surface to restore when the palette is closed with Esc.
        self._search_hits: list[tuple[str, str]] = []
        self._search_prev_view: str = "active"
        # "All chats" archive: row index -> (thread_key, transport) for opening a
        # conversation, plus an optional transport (mode) filter cycled by the
        # Mode button (None = every mode).
        self._archive_rows: list[tuple[str, str]] = []
        self._archive_mode_filter: str | None = None
        # Logs surface state. ``_logs_min_level`` is the severity floor the live
        # log feed renders at (cycled by the Level button / set by /loglevel).
        # ``_logs_paused`` freezes the live feed so the operator can scroll back
        # without new lines pushing the view.
        self._logs_min_level = logging.INFO
        self._logs_paused = False
        # Per-mode command history. Keyed by active_transport name when in a
        # transport mode, or by view name otherwise (e.g. "nomadnet").
        # Up/Down in the composer navigates backwards/forwards within the
        # history for the current mode. Cap set from [ui].command_history_limit
        # in on_mount.
        self._cmd_history: dict[str, deque] = {}
        self._cmd_hist_pos: dict[str, int] = {}   # -1 = not browsing
        self._cmd_hist_draft: dict[str, str] = {} # saved partial input while browsing
        self._cmd_hist_limit: int = 100
        # Weather surface: saved grid squares for the picker (loaded from config
        # on first _show_weather() call) and the active index.
        self._wx_grids: list[str] = []
        self._wx_grid_idx: int = 0

    # -- layout ---------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(id="modebar")
        with ContentSwitcher(initial="active-view", id="main"):
            with Vertical(id="active-view"):
                yield Static("", id="active-banner")
                yield Static("", id="brand-banner")
                with Horizontal(id="mesh-bar"):
                    yield Static("MeshCore", id="mesh-bar-label")
                    yield Static("", id="mesh-spacer")
                    yield Button(
                        "\u26a1 Start", id="mesh-start", classes="modebtn"
                    )
                    yield Button(
                        "\u2605 Favorite", id="mesh-fav", classes="modebtn"
                    )
                    yield Button(
                        "\U0001f4e3 Announce", id="mesh-announce", classes="modebtn"
                    )
                    yield Button(
                        "\U0001f30a Flood", id="mesh-announce-flood", classes="modebtn"
                    )
                with Horizontal(id="js8-bar"):
                    yield Static("JS8Call", id="js8-bar-label")
                    yield Static("", id="js8-spacer")
                    yield Button("\u26a1 Start", id="js8-start", classes="modebtn")
                    for _band in ("80m", "40m", "30m", "20m", "17m", "15m", "10m"):
                        yield Button(
                            _band, id=f"js8-band-{_band}", classes="modebtn"
                        )
                    yield Button("\u21bb", id="js8-freq-refresh", classes="modebtn")
                    yield Button(
                        "\U0001f4e3 CQ", id="js8-cq", classes="modebtn"
                    )
                    yield Button(
                        "\U0001f493 HB", id="js8-hb", classes="modebtn"
                    )
                    yield Button(
                        "\u2709 SMS", id="js8-sms", classes="modebtn"
                    )
                    yield Button(
                        "\U0001f4cd Beacon", id="js8-beacon", classes="modebtn"
                    )
                with Horizontal(id="winlink-bar"):
                    yield Static("Winlink", id="winlink-bar-label")
                    yield Static("", id="winlink-spacer")
                    yield Button("\u26a1 Start", id="winlink-start", classes="modebtn")
                    yield Button(
                        "\u270e Subject", id="winlink-subject", classes="modebtn"
                    )
                    yield Button(
                        "\u2709 Compose", id="winlink-compose", classes="modebtn"
                    )
                    yield Button(
                        "\U0001f4cb Forms", id="winlink-forms", classes="modebtn"
                    )
                    yield Button(
                        "\U0001f4e1 Connect", id="winlink-connect", classes="modebtn"
                    )
                    yield Button(
                        "\U0001f4e5 Outbox", id="winlink-outbox", classes="modebtn"
                    )
                    yield Button(
                        "\u2630 Gateways", id="winlink-gateways", classes="modebtn"
                    )
                with Horizontal(id="active-body"):
                    yield ListView(id="threads")
                    with Vertical(id="right"):
                        yield RichLog(
                            id="messages", wrap=True, markup=True, highlight=False
                        )
                with Horizontal(id="js8-query-bar"):
                    yield Static("Quick query:", id="js8-query-label")
                    yield Static("", id="js8-query-spacer")
                    for _q in ("SNR", "HEARING", "STATUS", "INFO"):
                        yield Button(
                            f"{_q}?", id=f"js8-query-{_q}", classes="modebtn"
                        )
            with Vertical(id="monitor-view"):
                yield Static(
                    "Favorites: (none added — '/fav add <id>' to track)",
                    id="monitor-ticker",
                )
                with Horizontal(id="watch-bar"):
                    yield Static(
                        "Watch - all transports (read-only). "
                        "Enter opens an item; [F4] favorites only.",
                        id="monitor-help",
                    )
                    yield Static("", id="watch-spacer")
                    yield Button("\u25cb Group", id="watch-group", classes="modebtn")
                    yield Button("⏸ Pause", id="watch-pause", classes="modebtn")
                    yield Button("\u21c5 By mode", id="watch-sort", classes="modebtn")
                    yield Button("✖ Clear", id="watch-clear", classes="modebtn")
                yield ListView(id="monitor")
            with Vertical(id="nomadnet-view"):
                yield Static(
                    "NomadNet pages (read-only) — Enter/tap a node to browse, "
                    "or type an address below. [f] save/unsave · [s] sync favs · "
                    "[F4] favorites.",
                    id="nomad-help",
                )
                with Horizontal(id="nomad-bar"):
                    yield Static("", id="nomad-spacer")
                    yield Button(
                        "\u21bb Sync favs", id="nomad-sync", classes="modebtn"
                    )
                    yield Button(
                        "\u2605 Save/Unsave", id="nomad-fav", classes="modebtn"
                    )
                yield ListView(id="nomad-nodes")
            with Vertical(id="health-view"):
                yield Static(
                    "Health - transport reachability (passive; no transmit). "
                    "[F5] re-check · [Ctrl+R] refresh",
                    id="health-help",
                )
                with Horizontal(id="health-body"):
                    yield RichLog(
                        id="health-log", wrap=True, markup=True, highlight=False,
                        auto_scroll=False,
                    )
                    yield RichLog(
                        id="health-sys-log", wrap=True, markup=True, highlight=False,
                        auto_scroll=False,
                    )
            with Vertical(id="logs-view"):
                with Horizontal(id="logs-bar"):
                    yield Static(
                        "Logs - live application log (in-memory). "
                        "[F5] cycle · '/loglevel <level>' to filter.",
                        id="logs-help",
                    )
                    yield Static("", id="logs-spacer")
                    yield Button("\u23f8 Pause", id="logs-pause", classes="modebtn")
                    yield Button("\u2191 Level", id="logs-level", classes="modebtn")
                    yield Button("\u2716 Clear", id="logs-clear", classes="modebtn")
                yield RichLog(
                    id="logs-log", wrap=True, markup=True, highlight=False
                )
            with Vertical(id="favorites-view"):
                yield Static(
                    "Favorites - saved NomadNet servers, callsigns, JS8Call "
                    "groups, MeshCore channels/contacts & hashes. Add (works "
                    "offline): type "
                    "'[node|peer|call|group|channel|contact] <id> [label]' below "
                    "+ Enter — e.g. 'node a1b2... HomeNode', '@EMS net' for a "
                    "JS8Call group, 'channel ops Ops net', or 'contact a1b2c3... "
                    "Bob'. Enter on a row opens it. Delete: select a row, then "
                    "Remove (or Ctrl+D, or '/fav rm <id>').",
                    id="fav-help",
                )
                with Horizontal(id="fav-bar"):
                    yield Static("", id="fav-spacer")
                    yield Button(
                        "\u2913 Import JS8 groups", id="fav-import-groups",
                        classes="modebtn",
                    )
                    yield Button("\u2716 Remove", id="fav-remove", classes="modebtn")
                yield ListView(id="favorites-list")
            with Vertical(id="search-view"):
                yield Static(
                    "Search history — type to find messages across every mode "
                    "(full-text). Enter on a result opens that conversation. "
                    "[Esc] closes.",
                    id="search-help",
                )
                yield Input(
                    placeholder="Search messages…  (e.g. 'net control', 'brid')",
                    id="search-input",
                )
                yield ListView(id="search-results")
            with Vertical(id="archive-view"):
                with Horizontal(id="archive-bar"):
                    yield Static(
                        "All chats — every conversation across all modes "
                        "(read-only). Enter opens one.",
                        id="archive-help",
                    )
                    yield Static("", id="archive-spacer")
                    yield Button("\u25cb Mode", id="archive-mode", classes="modebtn")
                    yield Button(
                        "\u21bb Refresh", id="archive-refresh", classes="modebtn"
                    )
                yield ListView(id="archive-list")
            with Vertical(id="net-view"):
                with Horizontal(id="net-bar"):
                    yield Static("", id="net-status")
                    yield Static("", id="net-spacer")
                    yield Button("◎ Open", id="net-open", classes="modebtn")
                    yield Button("✓ Check-in", id="net-ci-btn", classes="modebtn")
                    yield Button("✗ Close", id="net-close-btn", classes="modebtn")
                yield RichLog(
                    id="net-log", wrap=True, markup=True, highlight=False
                )
            with Vertical(id="weather-view"):
                with Horizontal(id="wx-bar"):
                    yield Static("⛅", id="wx-grid-label")
                    yield Button("◀", id="wx-grid-prev", classes="modebtn")
                    yield Input(value="", id="wx-grid", placeholder="FN31")
                    yield Button("▶", id="wx-grid-next", classes="modebtn")
                    yield Static("", id="wx-spacer")
                    yield Button("All", id="wx-filter-all", classes="modebtn")
                    yield Button("Inet", id="wx-filter-net", classes="modebtn")
                    yield Button("JS8", id="wx-filter-js8", classes="modebtn")
                    yield Button("WL", id="wx-filter-wl", classes="modebtn")
                    yield Button("RNS", id="wx-filter-rns", classes="modebtn")
                    yield Button("MC", id="wx-filter-mc", classes="modebtn")
                    yield Button("🌐 Fetch", id="wx-online", classes="modebtn")
                    yield Button("📡 JS8", id="wx-query", classes="modebtn")
                    yield Button("📧 Subscribe", id="wx-subscribe", classes="modebtn")
                    yield Button("⚙ Setup", id="wx-setup", classes="modebtn")
                yield RichLog(
                    id="wx-log", wrap=True, markup=True, highlight=False
                )
        yield Static("", id="statusbar")
        yield Input(placeholder="Type a message or /help ...", id="composer")
        yield Footer()

    # -- lifecycle ------------------------------------------------------------
    async def on_mount(self) -> None:
        cfg = Config.load(self._config_path)
        # File-only logging (Textual owns the terminal). The log path is shown
        # in the welcome message so users know where to `tail -f` it.
        from ..logging_setup import configure_logging

        log_path = configure_logging(cfg, stderr=False)
        self.core = CoreApp(cfg)
        # Size the Watch in-memory scrollback from config (0 = unbounded). Safe
        # to recreate here: no messages have been buffered before mount.
        try:
            limit = int(cfg.ui.get("watch_buffer_limit", _WATCH_BUFFER_DEFAULT))
        except (TypeError, ValueError):
            limit = _WATCH_BUFFER_DEFAULT
        limit = max(0, limit)
        self._watch_buffer_limit = limit
        self._monitor_msgs = deque(maxlen=limit if limit > 0 else None)
        try:
            self._cmd_hist_limit = max(
                0, int(cfg.ui.get("command_history_limit", 100))
            )
        except (TypeError, ValueError):
            self._cmd_hist_limit = 100
        # These don't need transports started, so wire them up immediately.
        self.core.router.add_ui_callback(self._on_router_message)
        self.core.compliance.set_confirm(lambda _w: self._encrypt_approved)
        branding = cfg.branding
        app_title = str(branding.get("app_title", "") or "").strip()
        self.title = app_title if app_title else "Radio_App"
        brand_banner = str(branding.get("banner", "") or "").strip()
        if brand_banner:
            bb = self.query_one("#brand-banner", Static)
            bb.update(brand_banner)
            bb.display = True
        self._build_mode_selector()
        self._update_modebar()
        self._update_status()
        self._update_monitor_help()
        self.query_one("#composer", Input).focus()
        self._log_system("Welcome. F3 cycles modes; F2 opens Watch.")
        if log_path is not None:
            self._log_system(f"Logs: tail -f {log_path}")
        # Restore the saved theme/palette, then allow future changes to persist.
        saved_theme = cfg.ui.get("theme", "")
        if saved_theme and saved_theme in self.available_themes:
            self.theme = saved_theme
        self._theme_ready = True
        # Keep the "ago" text in the Monitor ticker fresh even when no new
        # announces arrive.
        self.set_interval(2.0, self._refresh_monitor_ticker)
        # Passively probe each transport's reachability for the health dots.
        self.set_interval(5.0, self._refresh_health)
        self.call_after_refresh(self._refresh_health)
        self.set_interval(30.0, self._check_scheduled)
        # Keep the live Logs feed flowing and the status-bar WARN/ERR badge
        # current even when the operator is on another surface.
        self.set_interval(1.0, self._refresh_logs)
        self.set_interval(2.0, self._update_status)
        self.call_after_refresh(self._initial_flow)
        # Start transports in the background so the UI is interactive immediately
        # — a slow/unreachable transport (e.g. an offline JS8Call host whose TCP
        # connect sits in a multi-second timeout) no longer delays first paint.
        self._start_core()
        # Fetch HF propagation conditions once; updates health panel when done.
        self._fetch_propagation()

    @work(thread=True)
    def _fetch_propagation(self) -> None:
        """Fetch HF propagation conditions once at launch in a thread worker."""
        from ..core.propagation import fetch_sync
        try:
            data = fetch_sync(timeout=10.0)
        except Exception:
            data = None
        self._solar_data = data
        self._solar_fetched = True
        self.call_from_thread(self._refresh_health)

    @work
    async def _start_core(self) -> None:
        """Bring transports online without blocking the initial UI paint."""
        if self.core is None:
            return
        await self.core.start()
        # Reflect any transports that have now come up.
        self._update_status()
        self._update_modebar()
        self.call_after_refresh(self._refresh_health)

    def watch_theme(self, theme: str) -> None:
        """Persist the theme/palette selection to the single config file."""
        if not self._theme_ready or self.core is None:
            return
        try:
            self.core.config.set("ui", "theme", theme)
            self.core.config.save()
        except Exception:  # noqa: BLE001 - never let persistence break the UI
            pass

    @work
    async def _initial_flow(self) -> None:
        if self.core is None:
            return
        if not self.core.station.is_configured:
            await self._do_setup()
        # Open the configured landing surface (or auto-select the first mode).
        # F3 then cycles between modes.
        if self.active_transport is None and self.view == "active":
            self._open_home_view()

    def _open_home_view(self) -> None:
        """Open the startup landing surface.

        Honors ``[ui].home`` in the config: a transport name (e.g. ``meshcore``)
        opens that chat mode; ``nomadnet``/``watch``/``health``/``favorites`` open
        the matching utility surface. Empty or unrecognized falls back to the
        first configured transport (the historical default).
        """
        if self.core is None:
            return
        home = str(self.core.config.ui.get("home", "") or "").strip().lower()
        names = [t.name for t in self.core.transports]
        if home in ("health",):
            self.action_health()
            return
        if home in ("watch", "monitor"):
            self._show_watch()
            return
        if home in ("favorites", "favourites", "favs"):
            self._show_favorites()
            return
        if home in ("nomadnet", "nomad"):
            self._show_nomadnet()
            return
        if home and home in names:
            self._select_mode(home)
            return
        if home:
            self._log_system(
                f"[ui].home = {home!r} is not available; "
                "starting on the first mode."
            )
        # Default: first configured transport (preserves prior behavior).
        first = next((t.name for t in self.core.transports), None)
        if first is not None:
            self._select_mode(first)
        else:
            self._log_system(
                "No transports configured. Enable one in your config."
            )

    async def _do_setup(self) -> None:
        if self.core is None:
            return
        # If JS8Call is enabled, ask it for callsign/grid so the user need not
        # retype what JS8Call already knows (they can still override).
        station = self.core.station
        source_note = ""
        js8 = self.core.config.transports.get("js8call", {})
        if js8.get("enabled") and not station.callsign:
            from ..core.js8call_query import query_station

            info = await asyncio.to_thread(
                query_station,
                js8.get("host", "127.0.0.1"),
                int(js8.get("port", 2442)),
            )
            if info.any_found:
                station = Station(
                    callsign=info.callsign or station.callsign,
                    grid_square=info.grid or station.grid_square,
                )
                source_note = "Pre-filled from JS8Call - edit if needed."
        current_dl = str(self.core.config.storage.get("download_dir", "") or "")
        result = await self.push_screen_wait(
            SetupScreen(station, source_note, current_dl)
        )
        if not result:
            self._log_system("Station not set. HF transports need a callsign.")
            return
        cfg = self.core.config
        # The download location lives in [storage], not [station]; pull it out
        # before the rest of the result is written as station identity.
        download_dir = result.pop("download_dir", "")
        for key, value in result.items():
            cfg.set("station", key, value)
        if download_dir:
            cfg.set("storage", "download_dir", download_dir)
        cfg.save()
        self.core.station = Station(**result)
        self.core.router._station = self.core.station
        # Push the chosen download location to every running transport so all
        # modes save downloads there from now on.
        self.core.apply_download_dir()
        self._log_system(f"Station saved: {self.core.station.callsign}")
        if download_dir:
            self._log_system(
                f"Downloads will be saved to {self.core.config.download_dir()}"
            )


    async def on_unmount(self) -> None:
        if self.core is not None:
            await self.core.stop()

    # -- router bridge --------------------------------------------------------
    def _on_router_message(self, msg: UnifiedMessage, action) -> None:
        self.post_message(Incoming(msg, getattr(action, "value", str(action))))

    def on_incoming(self, event: Incoming) -> None:
        msg = event.msg
        # Announces are presence beacons - useful as a "transport is alive"
        # signal but far too chatty to render inline. Aggregate them into a
        # rolling counter shown in the status bar, then stop.
        if msg.metadata.get("kind") == "announce":
            self._record_announce(msg)
            self._update_status()
            if self.view == "health":
                self._render_health()
            return
        # Traffic beacons are a pure "transport is hearing the band" signal -
        # tally them for the status bar (like announces) and stop.
        if msg.metadata.get("kind") == "traffic":
            self._record_traffic(msg)
            self._update_status()
            if self.view == "health":
                self._render_health()
            return
        # Weather bulletins: append to WX feed when the view is active.
        if msg.metadata.get("kind") == "weather_bulletin":
            if self.view == "weather":
                self._render_weather()
            return
        # Delivery receipts (e.g. LXMF) annotate a previously-sent message with a
        # delivered/failed indicator; they are not conversations.
        if msg.metadata.get("kind") == "delivery":
            self._record_delivery(msg)
            return
        # Real (non-telemetry) messages: tally per transport for the Health
        # traffic-volume readout.
        if msg.transport:
            self._msg_counts[msg.transport] = (
                self._msg_counts.get(msg.transport, 0) + 1
            )
            if self.view == "health":
                self._render_health()
        # The Monitor always receives everything else, regardless of active mode.
        self._append_monitor(msg)
        # Watch traffic bumps a favorite's "last seen": anything we hear on the
        # air counts (broadcasts, traffic to others, direct messages), so the
        # timestamp reflects when the station was last *heard*, not just when it
        # messaged us.
        self._note_favorite_sighting(
            msg.metadata.get("rns_dest") or msg.sender or "",
            msg.metadata.get("display_name") or "",
            msg.timestamp,
            msg.transport or "?",
        )
        if event.action == "drop":
            return  # filtered out of active views/alerts (still in the Monitor)
        # The active view only updates for the active mode. With a conversation
        # open we render just that thread; with none selected the mode shows an
        # all-messages firehose, so any new message for the mode is appended.
        if msg.transport == self.active_transport:
            self._refresh_threads()
            if self.view == "active" and (
                self.current_target is None
                or msg.thread_key == self.current_target
            ):
                self._render_message(msg)
        if event.action == "notify":
            self.bell()

    def _record_announce(self, msg: UnifiedMessage) -> None:
        transport = msg.transport or "?"
        stats = self._announce_stats.setdefault(
            transport, {"count": 0, "last_ts": None, "last_peer": ""}
        )
        stats["count"] += 1
        stats["last_ts"] = msg.timestamp
        peer_label = (
            msg.metadata.get("display_name")
            or (msg.metadata.get("rns_dest", msg.sender) or "")[:12]
        )
        stats["last_peer"] = peer_label

        # (The old "recently heard -> add contact" discovery list lived here.
        # It was removed in favour of an RNS interface-stats readout on the
        # Health board, which is the actual rnsd/RNode health signal.)

        # An announce is also a sighting: bump "last seen" / fire back-online.
        rns_dest = msg.metadata.get("rns_dest") or msg.sender or ""
        display_name = msg.metadata.get("display_name") or ""
        self._note_favorite_sighting(
            rns_dest, display_name, msg.timestamp, transport, peer_label
        )
        # Self-healing classification: the announce aspect is the only reliable
        # peer-vs-site signal, so when we hear one for a saved contact, persist
        # the correct kind onto it (node = NomadNet site, peer = LXMF identity).
        aspect = msg.metadata.get("aspect")
        if aspect in ("site", "peer"):
            self._learn_favorite_kind(
                rns_dest, "node" if aspect == "site" else "peer"
            )

    def _learn_favorite_kind(self, ident: str, kind: str) -> None:
        """Persist an authoritatively-known kind onto a matching favorite.

        Reticulum can't tell a NomadNet site from an LXMF peer by hash alone, but
        an announce's aspect can. When such an announce matches a saved contact,
        we record ``node``/``peer`` on it so it's opened (browse vs. message) and
        grouped correctly from then on - without the user having to label it.
        """
        if self.core is None or not ident or kind not in ("node", "peer"):
            return
        fav = self.core.favorites.match(ident)
        # Never override an explicit callsign/group classification.
        if fav is None or fav.kind == kind or fav.kind in ("group", "callsign"):
            return
        fav.kind = kind
        try:
            self.core.favorites.save(self.core.config)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"could not update contact type: {exc}")
            return
        if self.view == "favorites":
            self._render_favorites()
        elif self.view == "nomadnet":
            self._refresh_nomad_nodes()

    def _note_favorite_sighting(
        self,
        ident: str,
        display_name: str,
        when: datetime,
        transport: str,
        peer_label: str = "",
    ) -> None:
        """Record that ``ident`` was heard and update favorite "last seen".

        Shared by the announce path *and* ordinary Watch traffic, so a callsign's
        "last seen" reflects **anything we hear on the air** (broadcasts, traffic
        to others, direct messages) - not only messages directed to us. The
        favorites decision seam (case-insensitive match + back-online logic)
        lives in core.favorites; here we just drive the ticker/alert/refresh.
        """
        if self.core is None or not ident:
            return
        sighting = self.core.favorites.note_sighting(
            ident, display_name=display_name, when=when
        )
        if sighting is None:
            return
        ticker_label = sighting.favorite.display or peer_label or ident[:12]
        # Deque dedupe: collapse consecutive sightings of the same favorite.
        if self._fav_recent and self._fav_recent[-1][1] == ticker_label:
            self._fav_recent[-1] = (when, ticker_label, transport)
        else:
            self._fav_recent.append((when, ticker_label, transport))
        self._refresh_monitor_ticker()
        if sighting.is_back_online:
            self.bell()
            self._log_system(
                f"[b yellow]\u2605 favorite online:[/b yellow] "
                f"{ticker_label} via {transport}"
            )
        # Keep the Favorites view's "last seen" column fresh while it's open.
        if self.view == "favorites":
            self._render_favorites()

    def _record_traffic(self, msg: UnifiedMessage) -> None:
        """Tally a traffic-heartbeat telemetry event for the status bar."""
        transport = msg.transport or "?"
        stats = self._traffic_stats.setdefault(
            transport, {"count": 0, "last_ts": None, "last_event": ""}
        )
        stats["count"] += 1
        stats["last_ts"] = msg.timestamp
        stats["last_event"] = msg.metadata.get("event", "")

    def _record_delivery(self, msg: UnifiedMessage) -> None:
        """Record an outbound delivery receipt and reflect it in the UI.

        Stores the status keyed by the original message id so the conversation
        log can show a ✓ (delivered) / ✗ (failed) marker, re-renders the open
        thread if it's the affected conversation, and logs a system line.
        """
        ref_id = msg.metadata.get("ref_msg_id")
        status = msg.metadata.get("status", "")
        if not ref_id or status not in ("delivered", "failed"):
            return
        self._delivery_status[ref_id] = status
        # Persist the receipt so it survives restarts and is verifiable later
        # (the store otherwise keeps the message as "sent" forever).
        if self.core is not None:
            persisted = (
                DeliveryStatus.DELIVERED
                if status == "delivered"
                else DeliveryStatus.FAILED
            )
            try:
                self.core.store.update_status(ref_id, persisted)
            except Exception:  # noqa: BLE001 - never let a receipt crash the UI
                pass
        recipient = msg.metadata.get("recipient") or ""
        if status == "delivered":
            self._log_system(f"\u2713 delivered to {self._short(recipient)}")
        else:
            self._log_system(f"\u2717 delivery failed to {self._short(recipient)}")
        # If the affected conversation is open, re-render so the inline marker
        # next to the sent message updates.
        if (
            self.view == "active"
            and self.current_target
            and recipient
            and self.current_target == recipient
        ):
            self._load_thread(self.current_target)

    # -- actions --------------------------------------------------------------
    def action_refresh(self) -> None:
        if self.view == "archive":
            self._render_archive()
        else:
            self._refresh_threads()
        self._update_status()

    @work
    async def action_settings(self) -> None:
        """Open the Settings modal (Favorites / Weather / Backup)."""
        if self.core is None:
            return
        if isinstance(self.screen, SettingsScreen):
            return
        result = await self.push_screen_wait(SettingsScreen(self.core))
        if result is None:
            return
        wx_result = result.get("wx")
        if wx_result is not None:
            await self._apply_wx_result(wx_result)

    def action_about(self) -> None:
        """Hidden easter egg: show the technical 'about' overview.

        Reachable via the undocumented Ctrl+G chord or the ``xyzzy`` magic word.
        Guarded so a second press while it's open doesn't stack screens.
        """
        if isinstance(self.screen, AboutScreen):
            return
        self.push_screen(AboutScreen())

    def action_copy_address(self, value: str = "") -> None:
        """Copy a value (e.g. your Reticulum address) to the clipboard.

        Triggered by clicking the underlined id in the status bar. Uses the
        terminal clipboard (OSC 52), which also works over SSH when the terminal
        supports it.
        """
        if not value:
            return
        try:
            self.copy_to_clipboard(value)
        except Exception:  # noqa: BLE001
            self._log_system(f"copy unavailable here — address: {value}")
            return
        self._log_system(f"\U0001f4cb copied to clipboard: {value}")

    def action_reply_to(self, ident: str = "", transport: str = "") -> None:
        """Open a direct conversation with a sender (clicked username).

        Triggered by clicking a sender's name in the message log. This is most
        useful in a shared thread — e.g. a MeshCore channel or a JS8 @group —
        where it lets you peel off and address that specific person directly
        instead of the whole channel. Switches to the sender's transport when
        known, opens (or starts) the 1:1 thread, and focuses the composer.
        """
        if self.core is None or not ident:
            return
        # Switch to the sender's mode when it's a different, configured
        # transport. Done first because changing mode resets current_target.
        if transport and transport != self.active_transport and transport in [
            t.name for t in self.core.transports
        ]:
            self._select_mode(transport)
        self.current_target = ident
        self._refresh_threads()
        self._load_thread(ident)
        self._update_status()
        try:
            self.query_one("#composer", Input).focus()
        except Exception:  # noqa: BLE001 - composer may not be mounted in tests
            pass
        self._log_system(
            f"replying to {self._display_id(ident)} — type a message and press Enter"
        )

    def action_deselect_chat(self) -> None:
        """Return to the full-feed view without deleting the thread (Escape)."""
        self.current_target = None
        if self.view == "active" and self.active_transport:
            self._show_all_messages()
        self._update_composer_placeholder()

    def action_close_chat(self) -> None:
        """Close (delete) the open conversation, removing it from the list."""
        self._close_chat(self.current_target)

    def _close_chat(self, thread_key: str | None) -> None:
        """Delete a conversation's messages so it drops out of the thread list.

        Threads are derived purely from stored messages, so closing a chat means
        deleting its messages. If the closed thread is the open one, the
        conversation pane is cleared. (Configured @groups keep their chip - you
        are still subscribed - but their history is removed.)
        """
        if self.core is None:
            return
        if not thread_key:
            self._log_system(
                "Open a conversation first, then /close (or /close <id>)."
            )
            return
        removed = self.core.store.delete_thread(thread_key)
        label = self._display_id(thread_key)
        # Forget it as an opened conversation so it doesn't reappear in the pane.
        for opened in self._opened_threads.values():
            if thread_key in opened:
                opened.remove(thread_key)
        if self.current_target == thread_key:
            self.current_target = None
            # Back to the mode's all-messages firehose (in the active chat view).
            if self.view == "active" and self.active_transport:
                self._show_all_messages()
            else:
                self.query_one("#messages", RichLog).clear()
        self._refresh_threads()
        self._update_status()
        if removed:
            self._log_system(
                f"closed conversation with {label} ({removed} messages removed)."
            )
        else:
            self._log_system(f"no stored messages to close for {label}.")

    # -- Reticulum-specific tools ---------------------------------------------
    def _reticulum_transport(self) -> Transport | None:
        """The live ReticulumTransport instance, or None when not configured."""
        if self.core is None:
            return None
        return next(
            (t for t in self.core.transports if t.name == "reticulum"), None
        )

    def _meshcore_transport(self) -> Transport | None:
        """The live MeshCoreTransport instance, or None when not configured."""
        if self.core is None:
            return None
        return next(
            (t for t in self.core.transports if t.name == "meshcore"), None
        )

    def _meshcore_channel_index(self, name: str) -> int | None:
        """Map a MeshCore channel favorite (by name) to its channel index.

        Names are matched case-insensitively against the transport's known
        channels (config + device), ignoring a leading '#'. 'public' / '' maps
        to the public channel (index 0). Returns None when the channel isn't
        known to this device.
        """
        t = self._meshcore_transport()
        if t is None or not hasattr(t, "channels"):
            return None
        target = (name or "").lstrip("#").strip().lower()
        if target in ("", "public"):
            return 0
        for ch in t.channels():
            cname = (ch.get("name") or "").lstrip("#").strip().lower()
            if cname == target:
                return ch["index"]
        return None

    def _meshcore_channel_name(self, index: int) -> str:
        """Friendly name for a MeshCore channel index ('public' for 0), or ''."""
        t = self._meshcore_transport()
        if t is None or not hasattr(t, "channels"):
            return ""
        for ch in t.channels():
            if ch["index"] == index:
                return (ch.get("name") or "").lstrip("#").strip()
        return ""

    @work
    async def _meshcore_announce(self, flood: bool = False) -> None:
        """Broadcast a MeshCore advert (announce) from the Mesh panel.

        Zero-hop by default (heard by immediate neighbours); ``flood=True``
        propagates across the mesh. Runs as a worker because the companion
        command is async.
        """
        t = self._meshcore_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("MeshCore is not running; cannot announce.")
            return
        ok = await t.send_advert(flood=flood)
        kind = "flood advert" if flood else "advert"
        self._log_system(
            f"\U0001f4e3 sent MeshCore {kind}."
            if ok
            else f"MeshCore {kind} failed (see logs)."
        )

    async def _handle_channel_command(self, arg: str) -> None:
        """Manage MeshCore channels from the panel: /channel list | add | rm.

        ``/channel add <index> <#name> [secret]`` creates a channel on the
        companion. A **hashtag channel** (``#name``) needs no secret — MeshCore
        derives its key from the name, so anyone who knows ``#name`` can join.
        The name is also saved to the config so it shows as ``#name`` and
        persists across restarts. ``/channel rm <index>`` drops the saved name.
        """
        t = self._meshcore_transport()
        if t is None or self.active_transport != "meshcore":
            self._log_system(
                "Switch to the MeshCore mode first (press F3) to manage channels."
            )
            return
        parts = arg.split()
        action = parts[0].lower() if parts else "list"

        if action in ("", "list", "ls"):
            self._log_system("MeshCore channels (open with /to @<index>):")
            for c in t.channels():
                name = c["name"] or ("public" if c["index"] == 0 else "")
                shown = f"#{name}" if name else "(unnamed)"
                self._log_system(f"  @{c['index']}  {shown}")
            self._log_system(
                "add one with: /channel add <index> <#name> [secret] "
                "(a #name needs no secret)"
            )
            return

        if action in ("rm", "remove", "del"):
            if len(parts) < 2 or not parts[1].isdigit():
                self._log_system("usage: /channel rm <index>")
                return
            index = int(parts[1])
            t.name_channel(index, "")
            self._persist_meshcore_channels(t)
            self._refresh_threads()
            self._log_system(f"removed saved name for channel @{index}.")
            return

        if action == "add":
            rest = parts[1:]
            if len(rest) < 2 or not rest[0].isdigit():
                self._log_system(
                    "usage: /channel add <index> <#name> [secret]  "
                    "e.g. /channel add 2 #ops"
                )
                return
            index = int(rest[0])
            if not 0 <= index < getattr(t, "MAX_CHANNELS", 8):
                self._log_system(
                    f"channel index must be 0-{getattr(t, 'MAX_CHANNELS', 8) - 1}."
                )
                return
            name = rest[1]
            secret = rest[2] if len(rest) > 2 else None
            # Save the display name (+ secret) and persist it to the single
            # config file so the channel can be recreated after a restart.
            t.name_channel(index, name, secret)
            self._persist_meshcore_channels(t)
            # Create it on the device when connected (hashtag channels need no
            # secret; the firmware derives the key from the name).
            applied = False
            if t.running:
                applied = await t.create_channel(index, name, secret)
            # Reflect immediately: open the new channel.
            self.current_target = f"@{index}"
            self._refresh_threads()
            self._load_thread(self.current_target)
            self._update_status()
            shown = f"#{name.lstrip('#')}"
            if not t.running:
                tail = "saved locally — connect the device to create it"
            elif applied:
                tail = "created on the device"
            else:
                tail = "device rejected it (see logs); saved locally"
            self._log_system(f"channel @{index} {shown}: {tail}.")
            return

        self._log_system("usage: /channel list | add <index> <#name> [secret] | rm")

    def _persist_meshcore_channels(self, transport: Transport) -> None:
        """Write the MeshCore transport's named channels to the config file."""
        if self.core is None:
            return
        transports = dict(self.core.config.transports)
        mc = dict(transports.get("meshcore", {}))
        mc["channels"] = transport.config_channels()
        self.core.config.set("transports", "meshcore", mc)
        try:
            self.core.config.save()
        except Exception as exc:  # noqa: BLE001 - never let persistence crash the UI
            self._log_system(f"could not save channel to config: {exc}")

    def action_identity(self) -> None:
        """Show our own (anonymous) Reticulum address so it can be shared.

        Reticulum identities are anonymous hashes, so peers can only reach us if
        we hand them this address. It's printed into the conversation log where
        it can be selected/copied from the terminal.
        """
        ret = self._reticulum_transport()
        if ret is None:
            self._log_system("Reticulum is not configured.")
            return
        addr = ret.local_identity() or "(unknown)"
        name = getattr(ret, "local_display_name", lambda: "")() or "(anonymous)"
        if not ret.running:
            self._log_system("Reticulum is not running; address unavailable.")
            return
        self._log_system(f"\U0001f5dd your Reticulum address: {addr}")
        self._log_system(f"   public display name: {name}")

    def action_announce(self) -> None:
        """Re-announce our identity so peers can find/reach us now.

        Bound to the active mode: in MeshCore mode it broadcasts a node advert;
        in Reticulum mode it re-announces our LXMF identity.
        """
        if self.active_transport == "meshcore":
            self._meshcore_announce(flood=False)
            return
        ret = self._reticulum_transport()
        if ret is None or not ret.running:
            self._log_system("Reticulum is not running; cannot announce.")
            return
        ok = ret.announce_now()
        self._log_system(
            "\U0001f4e3 announced your Reticulum identity."
            if ok
            else "announce failed (see logs)."
        )

    def action_find_path(self) -> None:
        """Request/store a network path to the open Reticulum contact.

        Reticulum resolves and caches the path itself, so a subsequent message
        to a known contact can be delivered even if they weren't heard recently.
        """
        ret = self._reticulum_transport()
        if ret is None or not ret.running:
            self._log_system("Reticulum is not running; cannot find a path.")
            return
        target = self.current_target
        if not target or target.startswith("@"):
            self._log_system("Open a direct Reticulum conversation first.")
            return
        if ret.has_path(target):
            self._log_system(f"\u2714 path already known to {self._short(target)}.")
            return
        if ret.request_path(target):
            self._log_system(
                f"\U0001f50d requested a path to {self._short(target)}; "
                "it will resolve as the network responds."
            )
        else:
            self._log_system(f"could not request a path to {target} (invalid id?).")


    def action_toggle_fav_only(self) -> None:
        """Toggle the favorites-only filter for the current surface (F4).

        Active on the Watch feed (filters the all-transport stream), in any chat
        mode (restricts that mode's conversation list to favorites), and in the
        NomadNet mode (restricts the node list to saved/favorite nodes). Hidden
        and inert on read-only surfaces (see :meth:`check_action`).
        """
        if self.view == "monitor":
            self._toggle_fav_only()
        elif self.view in ("active", "nomadnet"):
            self._toggle_active_fav_only()

    def _toggle_active_fav_only(self) -> None:
        self._active_fav_only = not self._active_fav_only
        if self.view == "nomadnet":
            self._refresh_nomad_nodes()
            self._update_nomad_help()
        else:
            self._refresh_threads()
            # The banner above the thread list already shows the filter state,
            # so we don't also write a system line into the conversation.
            self._update_active_banner()
        self._update_status()

    def _toggle_fav_only(self) -> None:
        self._monitor_fav_only = not self._monitor_fav_only
        # Favorites and the group filter are mutually exclusive.
        if self._monitor_fav_only:
            self._monitor_group_filter = None
        self._rebuild_monitor()
        self._update_monitor_help()
        self._update_watch_filter_buttons()
        state = "ON" if self._monitor_fav_only else "OFF"
        self._log_system(f"Monitor favorites-only filter: {state}")
        self._update_status()

    def _cycle_watch_group(self) -> None:
        """Cycle the Watch stream group filter: off -> group1 -> ... -> off.

        Selecting a group clears the favorites filter (the two are mutually
        exclusive). With no groups configured this is a no-op with a hint.
        """
        if self.core is None:
            return
        names = [g.name for g in self.core.groups.all()]
        if not names:
            self._log_system(
                "No groups configured. Add one with "
                "'radioapp group <name> add <transport:id>'."
            )
            return
        current = self._monitor_group_filter
        if current is None:
            nxt: str | None = names[0]
        else:
            try:
                idx = names.index(current)
            except ValueError:
                idx = -1
            nxt = names[idx + 1] if idx + 1 < len(names) else None
        self._monitor_group_filter = nxt
        if nxt is not None:
            self._monitor_fav_only = False
        self._rebuild_monitor()
        self._update_monitor_help()
        self._update_watch_filter_buttons()
        label = f"@{nxt}" if nxt else "OFF"
        self._log_system(f"Monitor group filter: {label}")
        self._update_status()

    def action_cycle_watch_group(self) -> None:
        """Cycle the Watch group filter (key 'g', Watch view only)."""
        if self.view == "monitor":
            self._cycle_watch_group()

    def _update_watch_filter_buttons(self) -> None:
        """Reflect the active group filter on the Watch bar's Group button."""
        try:
            btn = self.query_one("#watch-group", Button)
        except Exception:  # noqa: BLE001 - button may not be mounted (tests)
            return
        if self._monitor_group_filter:
            btn.label = f"\u25c9 @{self._monitor_group_filter}"
        else:
            btn.label = "\u25cb Group"

    def action_choose_mode(self) -> None:
        """Cycle to the next mode (no menu): each transport, then NomadNet."""
        if self.core is None:
            return
        keys = self._mode_keys()
        if not keys:
            self._log_system("No modes available. Enable a transport in your config.")
            return
        current = self._current_mode_key()
        if current in keys:
            nxt = keys[(keys.index(current) + 1) % len(keys)]
        else:
            nxt = keys[0]
        self._activate_mode_key(nxt)

    # Canonical display order for mode chips and F3 cycling.
    _MODE_ORDER = ["meshcore", "reticulum", "nomadnet", "js8call", "winlink", "wsjt_x"]

    # Abbreviated labels shown in the mode-selector bar.
    _MODE_SHORT_LABELS: dict[str, str] = {
        "meshcore":  "MC",
        "reticulum": "RNS",
        "nomadnet":  "Nomad",
        "js8call":   "JS8",
        "winlink":   "WL",
        "wsjt_x":    "WSJTX",
    }

    def _mode_keys(self) -> list[str]:
        """Ordered selectable operating modes, matching the selector chips."""
        if not self.core:
            return []
        transport_names = {t.name for t in self.core.transports}
        ordered = [n for n in self._MODE_ORDER if n == "nomadnet" or n in transport_names]
        ordered += [t.name for t in self.core.transports if t.name not in self._MODE_ORDER]
        return ordered

    def _current_mode_key(self) -> str | None:
        if self.view == "nomadnet":
            return "nomadnet"
        return self.active_transport

    def _activate_mode_key(self, key: str) -> None:
        if key == "nomadnet":
            self._show_nomadnet()
        else:
            self._select_mode(key)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Mode selector clicks (touch or mouse): pick a mode / Watch / Health."""
        self._note_input("pointer")
        bid = event.button.id or ""
        if bid == "mode-nomadnet":
            self._show_nomadnet()
        elif bid.startswith("mode-"):
            self._select_mode(bid[len("mode-"):])
        elif bid == "view-watch":
            self._show_watch()
        elif bid == "view-health":
            self.action_health()
        elif bid == "view-logs":
            self.action_logs()
        elif bid == "view-archive":
            self.action_archive()
        elif bid == "view-net":
            self._show_net()
        elif bid == "view-weather":
            self._show_weather()
        elif bid == "wx-grid-prev":
            self._wx_cycle_grid(-1)
        elif bid == "wx-grid-next":
            self._wx_cycle_grid(1)
        elif bid == "wx-filter-all":
            self._render_weather(transport_filter=None)
        elif bid == "wx-filter-net":
            self._render_weather(transport_filter="internet")
        elif bid == "wx-filter-js8":
            self._render_weather(transport_filter="js8call")
        elif bid == "wx-filter-wl":
            self._render_weather(transport_filter="winlink")
        elif bid == "wx-filter-rns":
            self._render_weather(transport_filter="reticulum")
        elif bid == "wx-filter-mc":
            self._render_weather(transport_filter="meshcore")
        elif bid == "wx-online":
            self._wx_fetch_internet()
        elif bid == "wx-query":
            self._wx_js8_query()
        elif bid == "wx-subscribe":
            self._wx_subscribe_guide()
        elif bid == "wx-setup":
            self._wx_run_setup()
        elif bid == "archive-mode":
            self._cycle_archive_filter()
        elif bid == "archive-refresh":
            self._render_archive()
            self._update_status()
        elif bid == "logs-pause":
            self._toggle_logs_pause()
        elif bid == "logs-level":
            self._cycle_log_level()
        elif bid == "logs-clear":
            ring = self._log_ring()
            if ring is not None:
                ring.clear()
            self._render_logs()
            self._update_status()
        elif bid == "watch-pause":
            self._toggle_watch_pause()
        elif bid == "watch-sort":
            self._toggle_watch_sort()
        elif bid == "watch-group":
            self._cycle_watch_group()
        elif bid == "watch-clear":
            self._clear_watch()
        elif bid == "nomad-fav":
            self.action_toggle_nomad_favorite()
        elif bid == "nomad-sync":
            self.action_sync_nomad()
        elif bid in ("mesh-start", "js8-start", "winlink-start"):
            transport_name = bid.split("-")[0]
            if transport_name == "mesh":
                transport_name = "meshcore"
            self._handle_start_command(transport_name)
        elif bid == "mesh-fav":
            self._favorite_current_conversation()
        elif bid == "mesh-announce":
            self._meshcore_announce(flood=False)
        elif bid == "mesh-announce-flood":
            self._meshcore_announce(flood=True)
        elif bid.startswith("js8-band-"):
            self._js8_switch_band(bid[len("js8-band-"):])
        elif bid == "js8-freq-refresh":
            self._js8_refresh_freq()
        elif bid == "js8-cq":
            self._js8_send_cq()
        elif bid == "js8-hb":
            self._js8_send_hb()
        elif bid == "js8-sms":
            self._js8_sms_prompt()
        elif bid == "js8-beacon":
            self._js8_send_beacon()
        elif bid.startswith("js8-query-"):
            self._js8_send_query(bid[len("js8-query-"):])
        elif bid == "winlink-subject":
            self._winlink_subject_prompt()
        elif bid == "winlink-compose":
            self._winlink_email_compose()
        elif bid == "winlink-forms":
            self._winlink_open_forms()
        elif bid == "winlink-connect":
            self._winlink_connect()
        elif bid == "winlink-outbox":
            self._winlink_show_outbox()
        elif bid == "winlink-gateways":
            self._winlink_list_gateways()
        elif bid == "fav-remove":
            self.action_remove_favorite()
        elif bid == "fav-import-groups":
            self._import_js8_groups()
        elif bid == "net-open":
            self._net_open_prompt()
        elif bid == "net-ci-btn":
            self._net_ci_self()
        elif bid == "net-close-btn":
            self._handle_net_command("close")

    def _select_mode(self, name: str) -> None:
        """Switch the active operating mode to a transport and show its surface."""
        if self.core is None:
            return
        self.active_transport = name
        self._update_radio_claim()
        self._show_active()
        self._apply_mode()
        self.query_one("#composer", Input).focus()

    def _show_active(self) -> None:
        """Show the active (mode-bound chat) surface."""
        self.view = "active"
        self.query_one("#main", ContentSwitcher).current = "active-view"
        self._enable_composer(True)
        self._update_active_banner()
        self._update_modebar()
        self._update_status()

    def _update_active_banner(self) -> None:
        """Show a clear 'favorites only' banner above the chat thread list.

        (Your Reticulum identity/display name lives in the bottom status bar's
        ``id:`` section, not here.)
        """
        try:
            banner = self.query_one("#active-banner", Static)
        except Exception:  # noqa: BLE001
            return
        if self._active_fav_only:
            mode = self.active_transport or "mode"
            banner.update(
                f"[b yellow]\u2605 showing FAVORITES only[/b yellow] in {mode} "
                "[dim]— press F4 to show everyone[/dim]"
            )
            banner.display = True
        else:
            banner.update("")
            banner.display = False

    def _show_watch(self) -> None:
        """Show the Watch (all-transport, read-only) surface."""
        self.view = "monitor"
        self.query_one("#main", ContentSwitcher).current = "monitor-view"
        self._enable_composer(False)
        self._refresh_monitor_ticker()
        self._update_monitor_help()
        self._update_watch_filter_buttons()
        self._update_modebar()
        self._update_status()

    def action_cycle_utility(self) -> None:
        """Cycle the utility surfaces with F5: Stream -> Health -> History
        -> Favorites -> Logs.

        From an operating (chat/NomadNet) mode, F5 jumps into the cycle at
        Stream, then advances Stream -> Health -> History -> Favorites -> Logs
        -> Stream.
        """
        order = ["monitor", "health", "archive", "favorites", "net", "weather", "logs"]
        if self.view in order:
            nxt = order[(order.index(self.view) + 1) % len(order)]
        else:
            nxt = "monitor"
        if nxt == "monitor":
            self._show_watch()
        elif nxt == "health":
            self._show_health()
        elif nxt == "logs":
            self._show_logs()
        elif nxt == "archive":
            self._show_archive()
        elif nxt == "net":
            self._show_net()
        elif nxt == "weather":
            self._show_weather()
        else:
            self._show_favorites()

    def action_health(self) -> None:
        """Open the Health surface directly (used by the Health chip)."""
        self._show_health()

    def _show_health(self) -> None:
        """Show the Health (reachability) surface."""
        self.view = "health"
        self.query_one("#main", ContentSwitcher).current = "health-view"
        self._enable_composer(False)
        self._render_health()
        self._refresh_health()
        self._update_modebar()
        self._update_status()

    # -- logs surface ---------------------------------------------------------

    # Severity floors the Logs surface can cycle through (with the Level button).
    _LOG_LEVELS = (
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
    )

    def action_logs(self) -> None:
        """Open the Logs surface directly (used by the Logs chip / status badge)."""
        self._show_logs()

    def _show_logs(self) -> None:
        """Show the Logs surface: the live, in-memory application log feed.

        Viewing the logs is treated as "acknowledging" any WARN/ERR that has
        accumulated, so the status-bar badge resets when the surface is opened.
        """
        self.view = "logs"
        self.query_one("#main", ContentSwitcher).current = "logs-view"
        self._enable_composer(False)
        ring = self._log_ring()
        if ring is not None:
            ring.reset_peak()
        self._render_logs()
        self._update_logs_buttons()
        self._update_modebar()
        self._update_status()

    def _log_ring(self):
        """Return the process-wide in-memory log handler, or None if absent."""
        from ..logging_setup import get_ring_handler

        return get_ring_handler()

    def _log_level_colour(self, level_no: int) -> str:
        if level_no >= logging.ERROR:
            return "red"
        if level_no >= logging.WARNING:
            return "yellow"
        if level_no >= logging.INFO:
            return "green"
        return "dim"

    def _render_logs(self) -> None:
        """Render the retained log records at/above the current severity floor."""
        try:
            log = self.query_one("#logs-log", RichLog)
        except Exception:  # noqa: BLE001 - surface not mounted yet
            return
        log.clear()
        ring = self._log_ring()
        if ring is None:
            log.write("[dim]In-memory logging is not active.[/dim]")
            return
        records = ring.snapshot(self._logs_min_level)
        if not records:
            level_name = logging.getLevelName(self._logs_min_level)
            log.write(
                f"[dim]No log records at {level_name} or above yet.[/dim]"
            )
            return
        for r in records:
            ts = datetime.fromtimestamp(r.created).strftime("%H:%M:%S")
            colour = self._log_level_colour(r.level_no)
            log.write(
                f"[dim]{ts}[/dim] [{colour}]{r.level_name:<7}[/{colour}] "
                f"[dim]{r.name}:[/dim] {r.message}"
            )

    def _refresh_logs(self) -> None:
        """Timer hook: redraw the Logs feed when visible and not paused.

        Re-rendering the whole bounded buffer is cheap and side-steps tracking a
        per-record cursor; pausing simply skips the redraw so the operator can
        scroll back without the view jumping.
        """
        if self.view != "logs" or self._logs_paused:
            return
        self._render_logs()

    def _update_logs_buttons(self) -> None:
        """Sync the Logs toolbar labels with the pause + level-filter state."""
        try:
            pause = self.query_one("#logs-pause", Button)
            level = self.query_one("#logs-level", Button)
        except Exception:  # noqa: BLE001 - surface not mounted yet
            return
        pause.label = "\u25b6 Follow" if self._logs_paused else "\u23f8 Pause"
        level.label = f"\u2191 {logging.getLevelName(self._logs_min_level)}"

    def _cycle_log_level(self) -> None:
        """Advance the Logs severity floor (DEBUG -> INFO -> WARNING -> ERROR)."""
        idx = (
            self._LOG_LEVELS.index(self._logs_min_level)
            if self._logs_min_level in self._LOG_LEVELS
            else 1
        )
        self._logs_min_level = self._LOG_LEVELS[
            (idx + 1) % len(self._LOG_LEVELS)
        ]
        self._update_logs_buttons()
        self._render_logs()
        self._update_status()

    def _toggle_logs_pause(self) -> None:
        self._logs_paused = not self._logs_paused
        self._update_logs_buttons()
        if not self._logs_paused:
            self._render_logs()

    def _set_log_level(self, arg: str) -> None:
        """Handle ``/loglevel [<level>]``: set the Logs severity floor.

        With no argument, reports the current floor. Accepts DEBUG/INFO/WARNING
        (or WARN)/ERROR, case-insensitively.
        """
        arg = arg.strip().upper()
        if not arg:
            self._log_system(
                "Log filter: "
                f"{logging.getLevelName(self._logs_min_level)}. "
                "Usage: /loglevel <debug|info|warning|error>"
            )
            return
        alias = {"WARN": "WARNING", "ERR": "ERROR"}
        arg = alias.get(arg, arg)
        level = logging.getLevelName(arg)
        if not isinstance(level, int):
            self._log_system(
                f"unknown level: {arg} (use debug|info|warning|error)"
            )
            return
        self._logs_min_level = level
        if self.view == "logs":
            self._update_logs_buttons()
            self._render_logs()
        else:
            self._show_logs()
        self._log_system(f"Log filter set to {arg}.")
        self._update_status()

    def _transport_identity(self, t) -> str:
        """Human description of the on-air identity a transport uses.

        Callsign-carrying media (HF: js8call/winlink) identify with a
        callsign — from the transport's own config if set, else the station
        callsign. Anonymous media (Reticulum/MeshCore) expose a non-identifying
        address via ``local_identity()``. Mirrors ``radioapp status``.
        """
        caps = t.capabilities()
        if caps.carries_operator_identity:
            own = ""
            cfg = getattr(t, "config", None)
            if isinstance(cfg, dict):
                own = str(cfg.get("callsign", "") or "").strip()
            callsign = own or (
                self.core.station.callsign if self.core is not None else ""
            )
            if callsign:
                return f"[dim]id:[/dim] callsign [b]{callsign}[/b]"
            return "[dim]id:[/dim] callsign [yellow](unset — run setup)[/yellow]"
        anon = None
        getter = getattr(t, "local_identity", None)
        if callable(getter):
            try:
                anon = getter()
            except Exception:  # noqa: BLE001
                anon = None
        display = ""
        display_getter = getattr(t, "local_display_name", None)
        if callable(display_getter):
            try:
                display = display_getter() or ""
            except Exception:  # noqa: BLE001
                display = ""
        if display:
            suffix = f" [dim]({anon})[/dim]" if anon else ""
            return f"[dim]id:[/dim] [b]{display}[/b]{suffix}"
        return f"[dim]id:[/dim] anonymous{f' ({anon})' if anon else ''}"

    def _transport_endpoint(self, t) -> str | None:
        """The control endpoint (host:port / URL) a transport dials, from config.

        Shown on the Health board even when the transport is **down**, so the
        operator can verify *where* the app is trying to connect (e.g. the
        JS8Call TCP API host, or Pat's HTTP URL) without digging in the config.
        Returns ``None`` for transports without a simple IP endpoint (Reticulum
        rides rnsd's local RPC socket; MeshCore may be USB-serial).
        """
        cfg = getattr(t, "config", None)
        if not isinstance(cfg, dict):
            return None
        if t.name == "js8call":
            host = str(cfg.get("host", "127.0.0.1") or "127.0.0.1")
            port = cfg.get("port", 2442)
            return f"[dim]endpoint:[/dim] {host}:{port} [dim](JS8Call TCP API)[/dim]"
        if t.name == "winlink":
            url = str(cfg.get("pat_url", "http://127.0.0.1:8080") or "")
            return f"[dim]endpoint:[/dim] {url} [dim](Pat HTTP API)[/dim]"
        return None

    # -- radio interlock (one HF radio, one transmitter) ----------------------

    def _continuous_radio_modes(self) -> set[str]:
        """Radio transports that hold the radio whenever their mode is active.

        JS8Call/Mercury occupy the sound card + CAT continuously while in use, so
        they claim the radio on mode entry. Winlink is excluded: it only keys the
        radio during an RF *session*, so it claims per-session, not on mode entry.
        """
        out: set[str] = set()
        if self.core is None:
            return out
        for t in self.core.transports:
            try:
                if t.capabilities().uses_shared_radio and t.name != "winlink":
                    out.add(t.name)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _update_radio_claim(self) -> None:
        """Sync the radio interlock token with the active mode (handoff on switch).

        Entering a continuous radio mode (JS8Call/Mercury) claims the radio;
        leaving it for a non-radio or Winlink mode releases that claim so a later
        Winlink RF session can take the radio. Warns when more than one radio
        transport is running and could key up over each other.
        """
        if self.core is None:
            return
        il = self.core.radio_interlock
        name = self.active_transport or ""
        continuous = self._continuous_radio_modes()
        if name in continuous:
            prev = il.transfer(name)
            if prev and prev != name and prev in continuous:
                self._log_system(f"\U0001f4fb radio: handed to {name} (was {prev}).")
        else:
            # Moving to Winlink / a non-radio mode frees a continuous holder.
            holder = il.holder
            if holder in continuous:
                il.release(holder)
        self._warn_radio_contention(name)

    def _running_radio_contenders(self, exclude: str = "") -> list[str]:
        """Running transports that share the one HF radio (minus ``exclude``)."""
        if self.core is None:
            return []
        out: list[str] = []
        for t in self.core.transports:
            if t.name == exclude or not getattr(t, "running", False):
                continue
            try:
                if t.capabilities().uses_shared_radio:
                    out.append(t.name)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _warn_radio_contention(self, name: str) -> None:
        """Warn when another radio transport could fight ``name`` for the radio."""
        if self.core is None or not self.core.radio_interlock.is_contender(name):
            return
        others = self._running_radio_contenders(exclude=name)
        if others:
            verb = "is" if len(others) == 1 else "are"
            self._log_system(
                f"\u26a0 radio: {', '.join(others)} {verb} also running and "
                f"share the one radio with {name}. Only one can transmit at a "
                "time — keep the others idle (or use Winlink telnet)."
            )

    def _render_radio_interlock(self, log: RichLog) -> None:
        """Render the radio-interlock status (who owns the shared HF radio)."""
        if self.core is None:
            return
        il = self.core.radio_interlock
        contenders = [
            t.name for t in self.core.transports
            if t.capabilities().uses_shared_radio
        ]
        if len(contenders) < 2:
            return  # nothing contends — no interlock to show
        log.write("")
        log.write("[b]Radio interlock[/b] (one HF radio — one transmitter)")
        log.write(f"  owner: [b]{il.holder or '(free)'}[/b]")
        log.write(f"  shares the radio: {', '.join(contenders)}")
        running = self._running_radio_contenders()
        if len(running) > 1:
            log.write(
                f"  [yellow]\u26a0 {', '.join(running)} are all running — keep all "
                "but one idle so they don't key over each other.[/yellow]"
            )

    def _render_health(self) -> None:
        """Render the reachability board from the latest probe results.

        For each reachable transport we also show a running traffic volume:
        announces heard + traffic-heartbeat events + delivered messages, with
        the time of the most recent event. This is the single 'is the network
        actually flowing?' indicator (previously duplicated in the status bar).

        For the Reticulum transport we additionally query rnsd over its RPC
        socket (the same call ``rnstatus`` uses) and render per-interface
        telemetry - including RNode RSSI/SNR/battery/frequency when an RNode
        is attached to rnsd. That is the real "RNode is healthy" signal.
        """
        if self.core is None:
            return
        log = self.query_one("#health-log", RichLog)
        log.clear()
        log.scroll_home(animate=False)
        log.write("[b]Transport reachability[/b] (passive endpoint probe)")
        if not self.core.transports:
            log.write("[dim]No transports configured. Enable one in your config.[/dim]")
            return
        for t in self.core.transports:
            status = self._health.get(t.name)
            dot = self._health_dot(t.name)
            if status is ReachabilityStatus.OK:
                word = "[green]reachable[/green]"
            elif status is ReachabilityStatus.DOWN:
                word = "[red]unreachable[/red]"
            elif status is ReachabilityStatus.NOT_APPLICABLE:
                word = "[dim]n/a[/dim]"
            else:
                word = "[dim]probing…[/dim]"
            vol = (
                self._format_traffic_volume(t.name)
                if status is ReachabilityStatus.OK
                else ""
            )
            log.write(f" {dot} [b]{t.name}[/b] {word}{vol}")
            # Show the actual on-air identity this transport uses (callsign for
            # HF media, an anonymous address for Reticulum/MeshCore) so it's
            # obvious which callsign goes out — matching `radioapp status`.
            log.write(f"    {self._transport_identity(t)}")
            # Show the configured control endpoint (host:port / URL) even when
            # the transport is down, so the operator can confirm *where* we dial.
            endpoint = self._transport_endpoint(t)
            if endpoint:
                log.write(f"    {endpoint}")
            if (
                t.name == "reticulum"
                and status is ReachabilityStatus.OK
                and hasattr(t, "interface_stats")
            ):
                self._render_rns_interfaces(log, t.interface_stats())
            # Winlink: show each connection path's modem/endpoint status, so the
            # operator can see (for example) that the varahf/Mercury modem is
            # down even while Pat (telnet) is reachable.
            if t.name == "winlink":
                self._render_winlink_paths(log)
            # JS8Call: show the rig operating state (dial/band/offset/speed and
            # the selected callsign) that JS8Call learns from the radio via CAT.
            if (
                t.name == "js8call"
                and status is ReachabilityStatus.OK
                and hasattr(t, "radio_status_snapshot")
            ):
                self._render_js8_status(log, t.radio_status_snapshot())
                if self.core is not None:
                    bstats = self.core.store.band_stats(transport=t.name)
                    if bstats:
                        parts = " ".join(f"{b} {c}" for b, c in bstats.items())
                        log.write(f"    [dim]band log: {parts}[/dim]")
            # MeshCore (and any transport exposing device_telemetry) shows its
            # device health: battery + radio parameters.
            if (
                status is ReachabilityStatus.OK
                and hasattr(t, "device_telemetry")
            ):
                self._render_device_telemetry(
                    log, self._device_telemetry.get(t.name)
                )
        self._render_radio_interlock(log)
        log.write("[dim]Press F5 to re-check now.[/dim]")
        self._render_system_health()

    def _render_system_health(self) -> None:
        """Render host system health into the right Health pane.

        Covers: UTC clock + time-source offset, position/grid, CPU, memory,
        temperature, power/battery, disk, and database stats. All metrics are
        best-effort — anything unknown is simply omitted.
        """
        from ..core.syshealth import collect, format_bytes, format_duration

        if self.core is None:
            return
        try:
            log = self.query_one("#health-sys-log", RichLog)
        except Exception:  # noqa: BLE001 - widget may not be mounted yet
            return
        log.clear()
        log.scroll_home(animate=False)
        db_path = self.core.config.database_path()
        health = collect(str(db_path))
        log.write("[b]System[/b] (host resources)")

        # UTC clock + time-source consensus (GPS → local NTP → internet NTP → system).
        ts = datetime.now(UTC).strftime("%H:%M:%S UTC")
        tr = self._time_reading
        if tr is not None and tr.offset_ms is not None:
            off = tr.offset_ms
            colour = (
                "red" if abs(off) > 1000
                else "yellow" if abs(off) > 100
                else "green"
            )
            src_label = {
                "gps": "GPS",
                "wsjtx_ft8": "WSJT-X/JS8Call DT",
                "local_ntp": "local NTP",
                "ntp": "NTP",
                "system": "system",
            }.get(tr.source.value, tr.source.value)
            sync = f"  [{colour}]{off:+.0f} ms[/{colour}]  [dim]({src_label})[/dim]"
        elif tr is not None and tr.error:
            sync = f"  [dim]({tr.error})[/dim]"
        elif self._time_queried:
            sync = "  [dim](all time sources unavailable)[/dim]"
        else:
            sync = "  [dim](checking…)[/dim]"
        log.write(f"  time  : {ts}{sync}")

        # Position display — from config [position] or a cached GPS reading.
        pos = self._position
        if pos is None and self.core is not None:
            from ..core.position import position_from_config
            pos = position_from_config(self.core.config)
        if pos is not None:
            source_tag = f"[dim]({pos.source})[/dim]"
            log.write(
                f"  pos   : {pos.lat:+.4f}°  {pos.lon:+.4f}°  "
                f"grid [b]{pos.grid}[/b]  {source_tag}"
            )

        cpu_bits: list[str] = []
        if health.cpu_percent is not None:
            cpu_bits.append(f"{health.cpu_percent:.0f}%")
        if health.load_avg is not None:
            la = health.load_avg
            cpu_bits.append(
                f"load {la[0]:.2f} {la[1]:.2f} {la[2]:.2f}"
                + (f" / {health.cpu_count} cpu" if health.cpu_count else "")
            )
        if cpu_bits:
            log.write(f"  cpu   : {'  '.join(cpu_bits)}")
        if health.mem_total:
            log.write(
                f"  mem   : {format_bytes(health.mem_used)} / "
                f"{format_bytes(health.mem_total)}"
                + (
                    f"  ({health.mem_percent:.0f}%)"
                    if health.mem_percent is not None
                    else ""
                )
            )
        if health.temp_c is not None:
            colour = (
                "red" if health.temp_c >= 80
                else "yellow" if health.temp_c >= 65
                else "green"
            )
            log.write(f"  temp  : [{colour}]{health.temp_c:.0f}°C[/{colour}]")
        if health.has_battery or health.power_plugged is not None:
            self._render_power_line(log, health, format_duration)
        if health.disk_free is not None:
            colour = (
                "red" if (health.disk_used_percent or 0) >= 95
                else "yellow" if (health.disk_used_percent or 0) >= 85
                else "green"
            )
            pct = (
                f"  ({health.disk_used_percent:.0f}% used)"
                if health.disk_used_percent is not None
                else ""
            )
            log.write(
                f"  disk  : [{colour}]{format_bytes(health.disk_free)} free[/{colour}]"
                f" of {format_bytes(health.disk_total)}{pct}"
            )

        # Database size + history extent — the "is my history bloating?" signal.
        try:
            s = self.core.store.stats()
            retention = int(
                self.core.config.general.get("history_retention_days", 0) or 0
            )
            keep = f"{retention}d retention" if retention > 0 else "kept forever"
            log.write(
                f"  data  : {format_bytes(s['size_bytes'])} · {keep}"
            )
        except Exception:  # noqa: BLE001 - never let stats break the board
            pass

        self._render_propagation(log)
        self._render_session_activity(log)

    def _render_session_activity(self, log: RichLog) -> None:
        """Compact per-transport traffic tally for this session."""
        if self.core is None:
            return
        log.write("")
        log.write("[b]Activity[/b] (this session)")
        for t in self.core.transports:
            vol = self._format_traffic_volume(t.name)
            log.write(f"  {t.name:<12}{vol}")

    def _render_power_line(self, log: RichLog, health, format_duration) -> None:
        """Render a battery/power line: charge %, AC/battery, time remaining.

        On a desktop/SBC with no battery we still show the mains state (so an
        operator running off a power supply knows AC is present); on a laptop or
        battery-backed field rig we colour the charge by how low it is and add a
        runtime estimate when discharging.
        """
        bits: list[str] = []
        if health.battery_percent is not None:
            pct = health.battery_percent
            colour = (
                "red" if pct < 15
                else "yellow" if pct < 40
                else "green"
            )
            bits.append(f"[{colour}]{pct:.0f}%[/{colour}]")
        if health.power_plugged is True:
            bits.append("\u26a1 on AC" if health.has_battery else "\u26a1 AC power")
        elif health.power_plugged is False:
            bits.append("on battery")
            if health.battery_secs_left:
                bits.append(f"~{format_duration(health.battery_secs_left)} left")
        if bits:
            log.write(f"  power : {'  '.join(bits)}")

    def _render_propagation(self, log: RichLog) -> None:
        """Render a compact solar conditions block in the System health pane."""
        if self.core is None:
            return
        solar = self._solar_data
        log.write("")
        log.write("[b]HF Conditions[/b] (hamqsl.com)")
        if solar is None:
            if self._solar_fetched:
                log.write("  [dim](unavailable — no internet at launch)[/dim]")
            else:
                log.write("  [dim](fetching…)[/dim]")
            return

        age_s = int((datetime.now(UTC) - solar.fetched_at).total_seconds())
        age = f"{age_s // 60} min ago" if age_s >= 60 else "just now"
        geo = f"  {solar.geo_field}" if solar.geo_field else ""
        log.write(
            f"  solar : SFI=[b]{solar.sfi}[/b]  SSN=[b]{solar.ssn}[/b]"
            f"  A=[b]{solar.a_index}[/b]  K=[b]{solar.k_index}[/b]"
            f"[dim]{geo}  ({age})[/dim]"
        )

        _C = {"Good": "green", "Fair": "yellow", "Poor": "red"}

        def _fmt(val: str) -> str:
            col = _C.get(val, "")
            return f"[{col}]{val}[/{col}]" if col else val

        # One compact line per band group: "Day/Night"
        groups = [
            ("80-40m", "80m-40m"),
            ("30-20m", "30m-20m"),
            ("17-15m", "17m-15m"),
            ("12-10m", "12m-10m"),
        ]
        for label, key in groups:
            cond = solar.conditions.get(key, {})
            d = _fmt(cond.get("day", "?"))
            n = _fmt(cond.get("night", "?"))
            log.write(f"  [b]{label:<7}[/b]  day {d}  night {n}")

    def _render_winlink_paths(self, log: RichLog) -> None:
        """Render Winlink connection-path availability under its status line.

        Each path (telnet → internet via Pat; varahf → Mercury/VARA modem;
        ardop → ARDOP modem) gets an up/down dot from a passive port probe.
        Paths that can't be probed (telnet, serial Pactor) show a neutral dot.
        """
        paths = self._winlink_paths
        if not paths:
            log.write("    [dim](connection paths not probed yet)[/dim]")
            return
        for p in paths:
            reachable = p.get("reachable")
            if reachable is True:
                dot, word = "[green]\u25cf[/green]", "[green]up[/green]"
            elif reachable is False:
                dot, word = "[red]\u25cb[/red]", "[red]down[/red]"
            else:
                dot, word = "[dim]\u00b7[/dim]", "[dim]n/a[/dim]"
            label = p.get("label", p.get("name", "?"))
            detail = p.get("detail", "")
            log.write(f"    {dot} {label} {word} [dim]{detail}[/dim]")

    def _render_js8_status(self, log: RichLog, snap: dict | None) -> None:
        """Render the JS8Call rig operating state under its status line.

        Shows dial frequency / band / audio offset / submode speed and the
        selected callsign, all of which JS8Call learns from the radio via CAT.
        When there is no dial frequency JS8Call has no CAT/rig control, so we say
        so rather than implying the rig state is known.
        """
        if not snap or not snap.get("cat"):
            log.write(
                "    [dim](no CAT/rig control — JS8Call can't read the radio)[/dim]"
            )
            return
        bits: list[str] = []
        dial = snap.get("dial")
        if dial:
            mhz = dial / 1_000_000
            band = snap.get("band")
            bits.append(f"{mhz:.6f} MHz" + (f" ({band})" if band else ""))
        offset = snap.get("offset")
        if offset:
            bits.append(f"offset {int(offset)} Hz")
        speed = snap.get("speed")
        if speed:
            bits.append(f"speed {speed}")
        sel = snap.get("selected")
        if sel:
            bits.append(f"selected {sel}")
        if bits:
            log.write(f"    [dim]{' · '.join(bits)}[/dim]")

    def _render_device_telemetry(self, log: RichLog, tel: dict | None) -> None:
        """Render a MeshCore companion's device telemetry under its status line.

        Surfaces the bits a field operator cares about: node name, battery, and
        the LoRa radio parameters (frequency/bandwidth/spreading factor/coding
        rate/TX power). ``tel`` is whatever ``device_telemetry()`` returned.
        """
        if not tel:
            log.write(
                "    [dim](no device telemetry — companion not responding)[/dim]"
            )
            return
        name = tel.get("name") or "(unnamed)"
        pk = (tel.get("public_key") or "")[:12]
        head = f"    [b]{name}[/b]"
        if pk:
            head += f"  [dim]<{pk}>[/dim]"
        batt = tel.get("battery")
        if batt is not None:
            head += f"   [dim]{self._format_mesh_battery(batt)}[/dim]"
        log.write(head)
        radio_bits: list[str] = []
        freq = tel.get("radio_freq")
        if freq:
            radio_bits.append(f"{freq:.3f} MHz")
        bw = tel.get("radio_bw")
        if bw:
            radio_bits.append(f"BW {bw:.0f} kHz")
        sf = tel.get("radio_sf")
        if sf:
            radio_bits.append(f"SF{sf}")
        cr = tel.get("radio_cr")
        if cr:
            radio_bits.append(f"CR{cr}")
        txp = tel.get("tx_power")
        if txp is not None:
            radio_bits.append(f"{txp} dBm")
        if radio_bits:
            log.write(f"       [dim]{' · '.join(radio_bits)}[/dim]")

    @staticmethod
    def _format_mesh_battery(level: object) -> str:
        """Format a MeshCore battery reading.

        The companion reports either a percentage (<=100) or a millivolt reading
        (>100); for mV we add a rough Li-ion percentage estimate.
        """
        try:
            lvl = int(level)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ""
        if lvl > 100:  # millivolts
            pct = max(0, min(100, round((lvl - 3000) / (4200 - 3000) * 100)))
            return f"batt {lvl} mV (~{pct}%)"
        return f"batt {lvl}%"

    def _render_rns_interfaces(self, log: RichLog, stats: dict | None) -> None:
        """Render per-interface RNS telemetry under the reticulum status line.

        ``stats`` is whatever ``ReticulumTransport.interface_stats()`` returned
        (the same shape as ``RNS.Reticulum.get_interface_stats()``). We surface
        the bits a field operator cares about: link state, throughput, and -
        when present - RNode-specific health (RSSI, SNR, battery, frequency).
        """
        if not stats:
            log.write("    [dim](no rnsd RPC reply - is rnsd running?)[/dim]")
            return
        ifs = stats.get("interfaces") or []
        if not ifs:
            log.write("    [dim](no RNS interfaces reported)[/dim]")
            return
        uptime = stats.get("transport_uptime")
        if uptime:
            log.write(
                f"    [dim]rnsd uptime: {self._format_duration(uptime)} "
                f"rx {self._format_bytes(stats.get('rxb', 0))} / "
                f"tx {self._format_bytes(stats.get('txb', 0))}[/dim]"
            )
        for ifs_row in ifs:
            name = ifs_row.get("short_name") or ifs_row.get("name") or "?"
            online = bool(ifs_row.get("status"))
            up_dot = "[green]\u25cf[/green]" if online else "[red]\u25cb[/red]"
            br = ifs_row.get("bitrate")
            rxb = ifs_row.get("rxb", 0)
            txb = ifs_row.get("txb", 0)
            line = (
                f"    {up_dot} [b]{name}[/b] "
                f"{self._format_bitrate(br)} "
                f"rx {self._format_bytes(rxb)} / tx {self._format_bytes(txb)}"
            )
            log.write(line)
            # RNode-specific extras (only present on RNode interfaces).
            extras: list[str] = []
            if "noise_floor" in ifs_row and ifs_row["noise_floor"] is not None:
                extras.append(f"noise {ifs_row['noise_floor']} dBm")
            if "battery_state" in ifs_row:
                pct = ifs_row.get("battery_percent")
                pct_s = f" {pct}%" if pct is not None else ""
                extras.append(f"batt {ifs_row['battery_state']}{pct_s}")
            if "airtime_short" in ifs_row:
                extras.append(f"airtime {ifs_row['airtime_short']}%/15s")
            if "channel_load_short" in ifs_row:
                extras.append(f"chload {ifs_row['channel_load_short']}%/15s")
            if "peers" in ifs_row and ifs_row["peers"] is not None:
                extras.append(f"peers {ifs_row['peers']}")
            if "clients" in ifs_row and ifs_row["clients"] is not None:
                extras.append(f"clients {ifs_row['clients']}")
            if extras:
                log.write(f"       [dim]{' · '.join(extras)}[/dim]")

    @staticmethod
    def _format_bytes(n: int | float | None) -> str:
        if not n:
            return "0 B"
        n = float(n)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if n < 1024 or unit == "TiB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024
        return f"{n:.1f} TiB"

    @staticmethod
    def _format_bitrate(bps: int | float | None) -> str:
        if not bps:
            return "[dim]--[/dim]"
        bps = float(bps)
        for unit, div in (("Mbps", 1_000_000), ("kbps", 1_000), ("bps", 1)):
            if bps >= div:
                return f"{bps / div:.1f} {unit}"
        return f"{bps:.0f} bps"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        if h < 24:
            return f"{h}h{m:02d}m"
        d, h = divmod(h, 24)
        return f"{d}d{h:02d}h"

    def _format_traffic_volume(self, transport: str) -> str:
        """Compact running tally of inbound activity for one transport.

        Aggregates the three counters we already collect: RNS/HF announces,
        per-transport traffic heartbeats, and delivered messages stored to
        threads. Returns the empty string when nothing has been heard yet.
        """
        ann = self._announce_stats.get(transport, {})
        traf = self._traffic_stats.get(transport, {})
        msgs = self._msg_counts.get(transport, 0)
        ann_n = ann.get("count", 0)
        traf_n = traf.get("count", 0)
        total = ann_n + traf_n + msgs
        if total == 0:
            return "   [dim](no traffic yet)[/dim]"
        last_ts = max(
            (ts for ts in (ann.get("last_ts"), traf.get("last_ts")) if ts),
            default=None,
        )
        last = last_ts.strftime("%H:%M:%S") if last_ts else "-"
        parts: list[str] = []
        if ann_n:
            parts.append(f"ann {ann_n}")
        if traf_n:
            parts.append(f"rx {traf_n}")
        if msgs:
            parts.append(f"msg {msgs}")
        return f"   [dim]traffic: {' · '.join(parts)}  (last {last})[/dim]"


    # -- NomadNet mode --------------------------------------------------------
    def _show_nomadnet(self) -> None:
        """Show the NomadNet (read-only page browsing) surface.

        Reuses the global composer at the bottom as the address bar so the
        typing target stays in the same screen position when switching modes
        (no jumpy in-view input box). Submission is routed to the browser by
        ``view`` in :meth:`on_input_submitted`.
        """
        self.view = "nomadnet"
        self.query_one("#main", ContentSwitcher).current = "nomadnet-view"
        # Keep the bottom composer enabled, just retitle it as an address bar.
        self._enable_composer(True)
        composer = self.query_one("#composer", Input)
        composer.placeholder = "<node hash>[:/page/x.mu]  ·  Enter to browse"
        self._refresh_nomad_nodes()
        self._update_nomad_help()
        self._update_modebar()
        self._update_status()
        composer.focus()

    def _update_nomad_help(self) -> None:
        """Header for the NomadNet surface: what it is + current filter state."""
        try:
            help_line = self.query_one("#nomad-help", Static)
        except Exception:  # noqa: BLE001
            return
        title = (
            "NomadNet pages [dim](read-only)[/dim] — "
            "Enter/tap a node to browse, or type an address below"
        )
        if self._active_fav_only:
            fav = "filter: [b yellow]\u2605 saved nodes only[/b yellow]"
        else:
            fav = "filter: [dim]off (all heard nodes)[/dim]"
        help_line.update(
            f"{title}    {fav}    [f] save/unsave \u00b7 [F4] toggle"
        )

    def _refresh_nomad_nodes(self) -> None:
        """List discovered NomadNet nodes for one-tap browsing.

        Saved (favorited) nodes that haven't re-announced yet are listed first
        and marked offline, so a bookmark is always recallable even before the
        node beacons again. Currently-heard nodes follow, with a leading star
        when they are favorites. Selecting any row opens the page browser.
        """
        if self.core is None:
            return
        lst = self.query_one("#nomad-nodes", ListView)
        lst.clear()
        self._nomad_nodes = []
        ret = next(
            (t for t in self.core.transports if t.name == "reticulum"), None
        )
        live = {
            n["dest"]: n
            for n in (ret.known_nodes() if ret is not None and ret.running else [])
        }
        favs = self.core.favorites
        # Treat a favorite as a NomadNet bookmark when its id looks like a full
        # 32-hex destination hash (the only form _open_nomad can dial) AND it is
        # not a known LXMF peer - peers are messageable, not browsable, so they
        # must not masquerade as sites here.
        saved = [
            f for f in favs.all()
            if len(f.id) == 32
            and all(c in "0123456789abcdef" for c in f.id.lower())
            and not self._is_known_peer(f.id)
        ]
        # Saved-but-currently-silent bookmarks first (so they're always recall-
        # able even before the node re-announces).
        for f in saved:
            if f.id.lower() in live:
                continue
            name = f.label or "(saved)"
            lst.append(ListItem(Label(
                f"\u2605 \U0001f5ce {name}  <{f.id[:16]}>  [dim](offline)[/dim]"
            )))
            self._nomad_nodes.append({"dest": f.id, "name": name})
        # Then live nodes, marking the ones we've saved. With the favorites-only
        # filter on (F4), non-favorite live nodes are hidden so only saved nodes
        # remain.
        for n in list(live.values())[:50]:
            is_fav = favs.is_favorite(n["dest"])
            if self._active_fav_only and not is_fav:
                continue
            name = n.get("name") or "(unnamed)"
            star = "\u2605 " if is_fav else "  "
            lst.append(ListItem(Label(
                f"{star}\U0001f5ce {name}  <{n['dest'][:16]}>"
            )))
            self._nomad_nodes.append(n)
        if not self._nomad_nodes:
            empty = (
                "[dim](no favorite nodes saved — press F4 to show all, or save "
                "one with 'f')[/dim]"
                if self._active_fav_only
                else "[dim](no nodes heard yet — they announce over time)[/dim]"
            )
            lst.append(ListItem(Label(empty)))
            self._nomad_nodes.append({})

    def _open_nomad(
        self, dest: str, path: str = "/page/index.mu", fields: dict | None = None
    ) -> None:
        if self.core is None or not getattr(self.core, "browser", None):
            self._log_system("NomadNet browser unavailable.")
            return
        # Even with Reticulum down we can still serve cached snapshots, so open
        # the viewer in offline mode rather than refusing outright. Dynamic pages
        # (with field_data) can't be cached, so those still need a live link.
        offline = not self.core.browser.available
        if offline:
            if fields:
                self._log_system(
                    "Reticulum is down; dynamic pages need a live link."
                )
                return
            self._log_system(
                "Reticulum is down — showing cached page (if any)."
            )
        self.push_screen(
            BrowseScreen(
                self.core.browser, dest, path, fields or {}, prefer_cache=offline
            )
        )

    def action_toggle_nomad_favorite(self) -> None:
        """Bookmark / un-bookmark the highlighted NomadNet node (key 'f').

        Saved nodes persist to the single config file via core.favorites and
        are recallable from the node list even when offline. No-op outside the
        NomadNet surface so the 'f' key stays free elsewhere.
        """
        if self.core is None or self.view != "nomadnet":
            return
        try:
            lst = self.query_one("#nomad-nodes", ListView)
        except Exception:  # noqa: BLE001
            return
        idx = lst.index
        if idx is None or idx >= len(self._nomad_nodes):
            return
        node = self._nomad_nodes[idx]
        dest = node.get("dest")
        if not dest:
            return
        favs = self.core.favorites
        if favs.is_favorite(dest):
            favs.remove(dest)
            self._log_system(f"node un-favorited: {dest[:12]}")
        else:
            label = node.get("name") or ""
            fav = favs.add(
                dest, label if label and label != "(unnamed)" else "", kind="node"
            )
            self._log_system(f"node favorited: {fav.display}")
        try:
            favs.save(self.core.config)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"could not save favorites: {exc}")
        self._refresh_nomad_nodes()

    @work
    async def action_sync_nomad(self) -> None:
        """Cache every favorite NomadNet node's page for offline viewing ('s').

        Fetches each ``kind == "node"`` favorite's index page live and stores it
        in the offline cache, so the pages stay readable once Reticulum drops.
        Runs as a worker so the UI stays responsive during the round-trips.
        """
        if self.core is None or self.view != "nomadnet":
            return
        browser = getattr(self.core, "browser", None)
        if browser is None:
            self._log_system("NomadNet browser unavailable.")
            return
        node_favs = [f for f in self.core.favorites.all() if f.kind == "node"]
        if not node_favs:
            self._log_system("No favorite nodes to sync (save one with 'f').")
            return
        if not browser.available:
            self._log_system("Reticulum is not running; cannot sync favorites.")
            return
        self._log_system(f"\u21bb syncing {len(node_favs)} favorite node(s)...")
        try:
            res = await browser.sync_favorites(node_favs)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"sync failed: {exc}")
            return
        self._log_system(
            f"\u2713 sync done: {res.ok} cached, {res.failed} failed, "
            f"{res.skipped} skipped"
        )

    # -- Watch pause / clear --------------------------------------------------
    def _toggle_watch_pause(self) -> None:
        self._watch_paused = not self._watch_paused
        state = "PAUSED" if self._watch_paused else "live"
        try:
            self.query_one("#watch-pause", Button).label = (
                "\u25b6 Resume" if self._watch_paused else "\u23f8 Pause"
            )
        except Exception:  # noqa: BLE001
            pass
        if not self._watch_paused:
            # Catch the list up to anything buffered while paused.
            self._rebuild_monitor()
        self._log_system(f"Watch: {state}")

    def _clear_watch(self) -> None:
        self._monitor_msgs.clear()
        self._monitor_entries.clear()
        try:
            self.query_one("#monitor", ListView).clear()
        except Exception:  # noqa: BLE001
            pass

    # -- Favorites surface ----------------------------------------------------
    def action_favorites_view(self) -> None:
        """Open the Favorites page (F6)."""
        self._show_favorites()

    def _show_favorites(self) -> None:
        """Show the Favorites surface: saved nodes, callsigns and hashes.

        The bottom composer is enabled here as an *add* bar so the operator can
        save a callsign or hash **without it being online first** - typing
        ``<id> [label]`` and pressing Enter persists it immediately. Slash
        commands still work (routed by view in :meth:`on_input_submitted`).
        """
        self.view = "favorites"
        self.query_one("#main", ContentSwitcher).current = "favorites-view"
        self._enable_composer(True)
        composer = self.query_one("#composer", Input)
        composer.placeholder = "Add favorite: <callsign|hash> [label] · Enter to add"
        self._render_favorites()
        self._update_modebar()
        self._update_status()
        composer.focus()

    # -- net surface -----------------------------------------------------------

    def _show_net(self) -> None:
        """Show the Net control / roll-call surface."""
        self.view = "net"
        self.query_one("#main", ContentSwitcher).current = "net-view"
        self._enable_composer(True)
        composer = self.query_one("#composer", Input)
        composer.placeholder = "/net open <name> · /net ci <call> [note] · /net close"
        self._render_net()
        self._update_modebar()
        self._update_status()
        composer.focus()

    def _render_net(self) -> None:
        """Redraw the Net surface with current session state."""
        if self.core is None:
            return
        nm = getattr(self.core, "net", None)
        if nm is None:
            return
        log = self.query_one("#net-log", RichLog)
        status = self.query_one("#net-status", Static)
        log.clear()

        session = nm.active
        if session is None:
            status.update("[dim]No active net — /net open <name>[/dim]")
            # Show the most recent closed session for reference.
            recent = nm.recent_sessions(limit=1)
            if recent:
                s = recent[0]
                elapsed = (
                    f"{s.duration_min:.0f} min"
                    if s.duration_min is not None
                    else "?"
                )
                log.write(
                    f"[dim]Last net: [b]{s.name}[/b]  {s.transport}  "
                    f"{s.opened_at.strftime('%Y-%m-%d %H:%M')} UTC  "
                    f"({elapsed})  {len(s.check_ins)} check-in(s)[/dim]"
                )
                for i, ci in enumerate(s.check_ins, 1):
                    note = f"  {ci.note}" if ci.note else ""
                    log.write(
                        f"[dim]  {i:>3}.  {ci.callsign:<12} "
                        f"{ci.checked_in_at.strftime('%H:%M')}Z{note}[/dim]"
                    )
        else:
            elapsed_s = (
                datetime.now(UTC) - session.opened_at
            ).total_seconds()
            elapsed = (
                f"{int(elapsed_s // 60)}m{int(elapsed_s % 60):02d}s"
            )
            status.update(
                f"[green b]OPEN[/green b]  [b]{session.name}[/b]  "
                f"{session.transport}  NC:[b]{session.net_control or '—'}[/b]  "
                f"{len(session.check_ins)} checked in  {elapsed}"
            )
            log.write(
                f"[b]Net:[/b] {session.name}  "
                f"opened {session.opened_at.strftime('%H:%M')}Z  "
                f"transport: {session.transport or '(any)'}"
            )
            if not session.check_ins:
                log.write("[dim]  No check-ins yet.[/dim]")
            else:
                log.write(f"[b]Check-ins ({len(session.check_ins)}):[/b]")
                for i, ci in enumerate(session.check_ins, 1):
                    note = f"  {ci.note}" if ci.note else ""
                    log.write(
                        f"  {i:>3}.  [b]{ci.callsign:<12}[/b] "
                        f"{ci.checked_in_at.strftime('%H:%M')}Z{note}"
                    )

    def _net_open_prompt(self) -> None:
        """Open a new net using the current mode as transport."""
        if self.core is None:
            return
        nm = getattr(self.core, "net", None)
        if nm is None:
            return
        if nm.active and nm.active.is_open:
            self._log_system(
                f"Net '{nm.active.name}' is already open — /net close first."
            )
            return
        nc = getattr(self.core.station, "callsign", "") or ""
        name = "Net"
        try:
            nm.open(name, transport=self.active_transport or "", net_control=nc)
        except ValueError as e:
            self._log_system(str(e))
            return
        self._log_system(
            f"Net opened: '{name}' on {self.active_transport or 'any'}.  "
            "Use /net open <custom name> to rename."
        )
        self._render_net()

    def _net_ci_self(self) -> None:
        """Check in own callsign via the ✓ button."""
        if self.core is None:
            return
        nc = getattr(self.core.station, "callsign", "") or "N0CALL"
        self._handle_net_command(f"ci {nc}")

    def _handle_net_command(self, arg: str) -> None:
        """Handle /net <subcommand> from the composer."""
        if self.core is None:
            self._log_system("Core not ready.")
            return
        nm = getattr(self.core, "net", None)
        if nm is None:
            self._log_system("Net manager unavailable.")
            return

        parts = arg.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("open", "start"):
            name = rest or "Net"
            nc = getattr(self.core.station, "callsign", "") or ""
            try:
                nm.open(
                    name,
                    transport=self.active_transport or "",
                    net_control=nc,
                )
            except ValueError as e:
                self._log_system(str(e))
                return
            self._log_system(
                f"[b]Net open:[/b] '{name}'  transport: "
                f"{self.active_transport or 'any'}  NC: {nc or '—'}"
            )
            if self.view == "net":
                self._render_net()

        elif sub in ("ci", "checkin", "check-in", "heard"):
            # /net ci [callsign] [note]  — bare "ci" checks in own callsign
            ci_parts = rest.split(maxsplit=1)
            if ci_parts and _looks_like_callsign(ci_parts[0]):
                callsign = ci_parts[0].upper()
                note = ci_parts[1].strip() if len(ci_parts) > 1 else ""
            else:
                callsign = (
                    getattr(self.core.station, "callsign", "") or "N0CALL"
                ).upper()
                note = rest
            try:
                ci = nm.check_in(callsign, note)
            except ValueError as e:
                self._log_system(str(e))
                return
            ts = ci.checked_in_at.strftime("%H:%M")
            self._log_system(
                f"[b]Check-in #{len(nm.active.check_ins)}:[/b] "  # type: ignore[union-attr]
                f"[b]{callsign}[/b]  {ts}Z"
                + (f"  {note}" if note else "")
            )
            if self.view == "net":
                self._render_net()

        elif sub in ("close", "end"):
            try:
                closed = nm.close()
            except ValueError as e:
                self._log_system(str(e))
                return
            elapsed = (
                f"{closed.duration_min:.0f} min"
                if closed.duration_min is not None
                else "?"
            )
            calls = ", ".join(ci.callsign for ci in closed.check_ins) or "none"
            self._log_system(
                f"[b]Net closed:[/b] '{closed.name}'  "
                f"{len(closed.check_ins)} check-in(s)  {elapsed}\n"
                f"  Roll call: {calls}"
            )
            if self.view == "net":
                self._render_net()

        elif sub in ("list", "ls", "show"):
            session = nm.active
            if session is None:
                self._log_system("No open net.  Recent: /net sessions")
                return
            lines = [
                f"[b]Net:[/b] {session.name}  "
                f"{len(session.check_ins)} check-in(s)"
            ]
            for i, ci in enumerate(session.check_ins, 1):
                note = f"  {ci.note}" if ci.note else ""
                lines.append(
                    f"  {i:>2}. [b]{ci.callsign}[/b]  "
                    f"{ci.checked_in_at.strftime('%H:%M')}Z{note}"
                )
            self._log_system("\n".join(lines))

        elif sub in ("status", "info"):
            session = nm.active
            if session is None:
                self._log_system("No open net session.")
            else:
                elapsed_s = (
                    datetime.now(UTC) - session.opened_at
                ).total_seconds()
                self._log_system(
                    f"[b]Net:[/b] {session.name}  OPEN  "
                    f"{len(session.check_ins)} check-in(s)  "
                    f"{int(elapsed_s // 60)}m elapsed  "
                    f"NC: {session.net_control or '—'}"
                )

        elif sub in ("sessions", "history", "log"):
            sessions = nm.recent_sessions(limit=5)
            if not sessions:
                self._log_system("No net sessions recorded yet.")
                return
            lines = ["[b]Recent net sessions:[/b]"]
            for s in sessions:
                state = "[green]OPEN[/green]" if s.is_open else "closed"
                dt = s.opened_at.strftime("%m-%d %H:%M")
                lines.append(
                    f"  {dt}Z  {state}  [b]{s.name}[/b]  "
                    f"{s.transport}  {len(s.check_ins)} CI"
                )
            self._log_system("\n".join(lines))

        else:
            self._log_system(
                "Usage: /net open <name> · /net ci [<callsign>] [note] · "
                "/net list · /net close · /net status · /net sessions"
            )

    # -- weather surface -------------------------------------------------------

    # Source badge colours and labels.
    _WX_BADGE: dict[str, str] = {
        "internet":   "[cyan][🌐][/cyan]",
        "js8call":    "[yellow][JS8][/yellow]",
        "winlink":    "[blue][WL][/blue]",
        "reticulum":  "[green][RNS][/green]",
        "meshcore":   "[magenta][MC][/magenta]",
    }

    def _wx_load_grids(self) -> None:
        """Load the saved grid list from config and sync the picker input."""
        if self.core is None:
            return
        raw = str(self.core.config.ui.get("wx_grids", "") or "")
        grids = [g.strip().upper() for g in raw.split(",") if g.strip()]
        # Fall back to station grid_square if nothing saved.
        if not grids:
            st = getattr(self.core, "station", None)
            gs = getattr(st, "grid_square", None) or ""
            if gs:
                grids = [gs.upper()]
        self._wx_grids = grids
        self._wx_grid_idx = min(self._wx_grid_idx, max(0, len(grids) - 1))
        self._wx_sync_grid_input()

    def _wx_sync_grid_input(self) -> None:
        """Update the grid Input to show the current picker grid."""
        try:
            inp = self.query_one("#wx-grid", Input)
            if self._wx_grids:
                grid = self._wx_grids[self._wx_grid_idx]
                total = len(self._wx_grids)
                inp.value = grid
                inp.placeholder = grid
                # Show position hint in the prev/next buttons when >1 grid.
                try:
                    self.query_one("#wx-grid-prev", Button).disabled = total <= 1
                    self.query_one("#wx-grid-next", Button).disabled = total <= 1
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _wx_cycle_grid(self, direction: int) -> None:
        """Cycle to the previous (direction=-1) or next (direction=+1) saved grid."""
        if not self._wx_grids:
            return
        self._wx_grid_idx = (self._wx_grid_idx + direction) % len(self._wx_grids)
        self._wx_sync_grid_input()

    def _show_weather(self) -> None:
        """Show the Weather (WX) surface."""
        self.view = "weather"
        self.query_one("#main", ContentSwitcher).current = "weather-view"
        self._enable_composer(False)
        self._wx_load_grids()
        # If still no grid in the input, fall back to a blank placeholder.
        try:
            inp = self.query_one("#wx-grid", Input)
            if not inp.value:
                inp.placeholder = "FN31"
        except Exception:  # noqa: BLE001
            pass
        self._render_weather()
        self._update_modebar()
        self._update_status()

    def _render_weather(self, transport_filter: str | None = None) -> None:
        """Redraw the WX feed from the store, optionally filtered by transport."""
        if self.core is None:
            return
        try:
            wx_log = self.query_one("#wx-log", RichLog)
        except Exception:  # noqa: BLE001
            return
        wx_log.clear()
        msgs = self.core.store.query(kind="weather_bulletin", limit=200, newest_first=True)
        if transport_filter:
            msgs = [m for m in msgs if m.transport == transport_filter]
        if not msgs:
            wx_log.write(
                "[dim]No weather data yet. "
                "Try '🌐 Fetch' for internet forecast, '📡 JS8' to query via radio, "
                "or connect Winlink to download NWS bulletins. "
                "RNS/MC weather arrives automatically when connected.[/dim]"
            )
            return
        for msg in msgs:
            transport = msg.transport or "?"
            badge = self._WX_BADGE.get(transport, f"[dim][{transport[:3].upper()}][/dim]")
            ts = msg.timestamp.strftime("%m-%d %H:%MZ") if msg.timestamp else "??"
            subject = msg.metadata.get("subject") or ""
            nws_office = msg.metadata.get("nws_office", "")
            source_label = nws_office or (msg.sender or "")
            header = f"{badge} [dim]{ts}[/dim]  "
            if subject:
                header += f"[b]{subject}[/b]"
                if source_label:
                    header += f"  [dim]· {source_label}[/dim]"
            elif source_label:
                header += f"[dim]{source_label}[/dim]"
            wx_log.write(header)
            body = (msg.content or "").strip()
            if body:
                for line in body.splitlines()[:8]:
                    wx_log.write(f"    {line}")
            wx_log.write("")

    @work
    async def _wx_fetch_internet(self) -> None:
        """Fetch a weather forecast from NWS or Open-Meteo for the current grid."""
        if self.core is None:
            return
        try:
            grid_inp = self.query_one("#wx-grid", Input)
            grid = grid_inp.value.strip().upper() or "FN31"
        except Exception:  # noqa: BLE001
            grid = "FN31"
        try:
            wx_log = self.query_one("#wx-log", RichLog)
            wx_log.write(f"[dim]Fetching internet weather for {grid}…[/dim]")
        except Exception:  # noqa: BLE001
            pass
        try:
            lat, lon = grid_to_latlon(grid)
            text, source = await fetch_weather(lat, lon)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"WX internet fetch failed: {exc}")
            try:
                self.query_one("#wx-log", RichLog).write(
                    f"[red]Internet weather unavailable: {exc}[/red]"
                )
            except Exception:  # noqa: BLE001
                pass
            return
        # Store as a transient UnifiedMessage so it appears in the feed.
        from ..core.message import UnifiedMessage as _UM
        import datetime as _dt
        msg = _UM(
            msg_id=f"wx-inet-{grid}-{int(_dt.datetime.now(_dt.timezone.utc).timestamp())}",
            transport="internet",
            sender="NWS/Open-Meteo",
            content=text,
            timestamp=_dt.datetime.now(_dt.timezone.utc),
            metadata={
                "kind": "weather_bulletin",
                "subject": f"Internet WX {grid} ({source})",
                "grid": grid,
                "source": source,
            },
        )
        self.core.store.save(msg)
        self._render_weather()

    @work
    async def _wx_js8_query(self) -> None:
        """Send a JS8Call weather query to the APRS gateway (@APRSIS NWS <grid>)."""
        if self.core is None:
            return
        try:
            wx_log = self.query_one("#wx-log", RichLog)
        except Exception:  # noqa: BLE001
            return
        try:
            grid = self.query_one("#wx-grid", Input).value.strip().upper() or "FN31"
        except Exception:  # noqa: BLE001
            grid = "FN31"
        from ..transports.js8call_transport import JS8CallTransport
        js8 = next(
            (t for t in self.core.transports if isinstance(t, JS8CallTransport)),
            None,
        )
        if js8 is None:
            wx_log.write("[yellow]JS8Call transport not active — cannot query APRS WX.[/yellow]")
            return
        wx_log.write(
            f"[dim]⛅ JS8/APRS query sent for {grid[:4]}. "
            "Reply will appear when received.[/dim]"
        )
        ok = await js8.send_wx_query(grid)
        if not ok:
            wx_log.write("[yellow]JS8Call WX query failed — is JS8Call running?[/yellow]")

    @work
    async def _wx_winlink_scan(self) -> None:
        """Scan the store for Winlink weather bulletins and refresh the feed."""
        if self.core is None:
            return
        try:
            wx_log = self.query_one("#wx-log", RichLog)
            wx_log.write("[dim]Scanning Winlink inbox for NWS bulletins…[/dim]")
        except Exception:  # noqa: BLE001
            pass
        # The store query with kind= already covers stamped messages; just render.
        self._render_weather(transport_filter="winlink")

    @work
    async def _wx_subscribe_guide(self) -> None:
        """Show the Winlink NWS subscription guide and optionally open compose."""
        try:
            grid = self.query_one("#wx-grid", Input).value.strip().upper()
        except Exception:  # noqa: BLE001
            grid = ""
        result = await self.push_screen_wait(WinlinkWXSubscribeScreen(grid=grid))
        if result:
            # Pre-fill compose with the subscription request.
            grid4 = grid[:4] if grid else ""
            subj = f"SUBSCRIBE {grid4}".strip()
            await self.push_screen_wait(
                WinlinkEmailComposeScreen(
                    subject=subj,
                    callsign=self._my_callsign(),
                )
            )

    def _my_callsign(self) -> str:
        """Return the operator's callsign from config."""
        if self.core is None:
            return ""
        st = getattr(self.core, "station", None)
        if st is not None:
            cs = getattr(st, "callsign", None) or ""
            if cs:
                return str(cs)
        return str(self.core.config.station.get("callsign", "") or "")

    @work
    async def _wx_run_setup(self) -> None:
        """Ask whether to add MeshCore #weather channel and Reticulum #weather groups."""
        if self.core is None:
            return
        from ..transports.meshcore_transport import MeshCoreTransport
        from ..transports.reticulum_transport import ReticulumTransport
        has_mc = any(isinstance(t, MeshCoreTransport) for t in self.core.transports)
        has_rns = any(isinstance(t, ReticulumTransport) for t in self.core.transports)
        current_grids = str(self.core.config.ui.get("wx_grids", "") or "")
        result = await self.push_screen_wait(
            WXSetupScreen(
                has_meshcore=has_mc,
                has_reticulum=has_rns,
                current_grids=current_grids,
            )
        )
        if result is None:
            return
        await self._apply_wx_result(result)

    async def _apply_wx_result(self, result: dict) -> None:
        """Apply a WX setup result dict (grids + meshcore/reticulum flags) to config."""
        if self.core is None:
            return

        # Save grid squares to config and reload the picker (always write, even
        # when empty, so the user can clear a previously saved list).
        grids_str = result.get("grids", "").strip()
        ui = dict(self.core.config.ui)
        ui["wx_grids"] = grids_str
        self.core.config.data["ui"] = ui
        try:
            self.core.config.save()
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Could not save WX grids: {exc}")
        self._wx_load_grids()
        n = len([g for g in grids_str.split(",") if g.strip()])
        if n:
            self._log_system(f"Saved {n} grid square(s) for WX picker.")
        else:
            self._log_system("WX grid list cleared.")

        if result.get("meshcore"):
            t = self._meshcore_transport()
            if t is not None and hasattr(t, "channels"):
                try:
                    # Find the first free slot above the public channel (index 0).
                    used = {ch["index"] for ch in t.channels()}
                    max_ch = getattr(t, "MAX_CHANNELS", 8)
                    idx = next(
                        (i for i in range(1, max_ch) if i not in used), None
                    )
                    if idx is None:
                        self._log_system(
                            "MeshCore: all channel slots used — remove one first "
                            "with /channel rm <index>."
                        )
                    else:
                        # Check if #weather already configured.
                        existing = [
                            ch for ch in t.channels()
                            if ch.get("name", "").lower() == "weather"
                        ]
                        if existing:
                            self._log_system(
                                f"MeshCore #weather already at @{existing[0]['index']}."
                            )
                        else:
                            t.name_channel(idx, "#weather")
                            self._persist_meshcore_channels(t)
                            if t.running:
                                ok = await t.create_channel(idx, "#weather", None)
                                if ok:
                                    self._log_system(
                                        f"Added MeshCore #weather at channel @{idx}."
                                    )
                                else:
                                    self._log_system(
                                        f"MeshCore #weather saved (@{idx}) but "
                                        "device update failed — reconnect to apply."
                                    )
                            else:
                                self._log_system(
                                    f"MeshCore #weather saved (@{idx}); "
                                    "will be created when MeshCore connects."
                                )
                            self._refresh_threads()
                except Exception as exc:  # noqa: BLE001
                    self._log_system(f"MeshCore channel setup failed: {exc}")
            else:
                self._log_system(
                    "MeshCore not configured — add it to config first."
                )

        if result.get("reticulum"):
            try:
                gr = self.core.groups
                added = []
                for gname in ("weather", "nws_alerts"):
                    g = gr.ensure_group(gname)
                    changed = False
                    if "reticulum" not in g.transports:
                        g.transports.append("reticulum")
                        gr._dirty = True  # noqa: SLF001
                        changed = True
                    if "weather" not in g.tags:
                        gr.add_tag(gname, "weather")
                        changed = True
                    if changed:
                        added.append(f"#{gname}")
                if added:
                    gr.save(self.core.config)
                    # Re-push updated group list to running transports so they
                    # join the new groups without a restart.
                    self.core._apply_station_identity()  # noqa: SLF001
                    self._log_system(
                        f"Added Reticulum groups: {', '.join(added)}. "
                        "They appear in the RNS mode contacts panel."
                    )
                else:
                    self._log_system(
                        "Reticulum #weather and #nws_alerts already configured."
                    )
            except Exception as exc:  # noqa: BLE001
                self._log_system(f"Reticulum group setup failed: {exc}")

    # -- favorites (continued) -------------------------------------------------

    def _favorite_kind(self, fav: Favorite) -> str:
        """Classify a favorite as 'node' (NomadNet server), 'hash' or 'callsign'."""
        return self._favorite_kind_by_id(fav.id)

    def _favorite_kind_by_id(self, fid: str) -> str:
        # A leading '@' is a JS8Call group, by convention (e.g. @EMS) - this is
        # syntactic so it wins over any stored kind.
        if (fid or "").strip().startswith("@"):
            return "group"
        # An explicitly stored kind wins (so a node saved while offline still
        # opens the page browser and groups under "NomadNet servers").
        if self.core is not None:
            fav = self.core.favorites.match(fid)
            if fav is not None and fav.kind:
                if fav.kind == "node":
                    return "node"
                if fav.kind == "callsign":
                    return "callsign"
                if fav.kind == "group":
                    return "group"
                if fav.kind == "peer":
                    return "hash"
                # MeshCore favorites: a channel (by name) or a contact/user
                # (by public-key prefix). Both open in the MeshCore mode.
                if fav.kind == "mc_channel":
                    return "mc_channel"
                if fav.kind == "mc_peer":
                    return "mc_peer"
        fid_l = (fid or "").strip().lower()
        is_hex = len(fid_l) >= 8 and all(c in "0123456789abcdef" for c in fid_l)
        if not is_hex:
            return "callsign"
        # A bare hash is ambiguous; resolve it from heard announce aspects when
        # possible (node = NomadNet site, peer = LXMF identity). Unknown hashes
        # fall back to "hash" (treated as a messageable peer).
        ret = self._reticulum_transport()
        if ret is not None and ret.running:
            try:
                cls = ret.classify_dest(fid_l)
            except Exception:  # noqa: BLE001
                cls = ""
            if cls == "node":
                return "node"
            if cls == "peer":
                return "hash"
        return "hash"


    def _is_known_peer(self, fid: str) -> bool:
        """True when ``fid`` is known to be an LXMF peer (not a NomadNet site).

        Uses an explicit saved ``peer`` kind or the heard announce aspect, so we
        can exclude messageable peers from the NomadNet (page-browser) node list.
        """
        if self.core is None or not fid:
            return False
        fav = self.core.favorites.match(fid)
        if fav is not None and fav.kind == "peer":
            return True
        ret = self._reticulum_transport()
        if ret is not None and ret.running:
            try:
                return ret.classify_dest(fid) == "peer"
            except Exception:  # noqa: BLE001
                return False
        return False

    def _render_favorites(self) -> None:
        """Render saved favorites, grouped by kind (servers, callsigns, groups,
        hashes)."""
        if self.core is None:
            return
        lst = self.query_one("#favorites-list", ListView)
        lst.clear()
        self._fav_keys = []
        favs = self.core.favorites.all()
        if not favs:
            lst.append(ListItem(Label(
                "[dim](no favorites yet — type '<callsign|@group|hash> [label]' "
                "below to add one)[/dim]"
            )))
            self._fav_keys.append("")
            return
        nodes, calls, groups, hashes = [], [], [], []
        mc_channels, mc_peers = [], []
        for f in favs:
            kind = self._favorite_kind(f)
            if kind == "node":
                nodes.append(f)
            elif kind == "group":
                groups.append(f)
            elif kind == "callsign":
                calls.append(f)
            elif kind == "mc_channel":
                mc_channels.append(f)
            elif kind == "mc_peer":
                mc_peers.append(f)
            else:
                hashes.append(f)
        sections = (
            ("NomadNet servers", nodes, "\U0001f5ce"),  # 🗎
            ("Callsigns", calls, "\U0001f4fb"),          # 📻
            ("JS8Call groups", groups, "\U0001f4e2"),    # 📢
            ("MeshCore channels", mc_channels, "\U0001f4e1"),  # 📡
            ("MeshCore contacts", mc_peers, "\U0001f9d1"),     # 🧑
            ("Hashes / peers", hashes, "\U0001f517"),    # 🔗
        )
        now = datetime.now(UTC)
        for title, items, glyph in sections:
            if not items:
                continue
            lst.append(ListItem(Label(f"[b]{title}[/b]  [dim]({len(items)})[/dim]")))
            self._fav_keys.append("")  # header row: not a target
            for f in items:
                ago = (
                    self._format_ago(now - f.last_seen)
                    if f.last_seen
                    else "never"
                )
                name = f.label or "(no label)"
                # Show full id for callsigns/groups/channels; truncate long hex.
                shown_id = (
                    f.id
                    if glyph in ("\U0001f4fb", "\U0001f4e2", "\U0001f4e1")
                    else f.id[:16]
                )
                lst.append(ListItem(Label(
                    f"  {glyph} {name}  [dim]<{shown_id}>  last seen: {ago}[/dim]"
                )))
                self._fav_keys.append(f.id)

    def _open_favorite(self, fid: str) -> None:
        """Act on a chosen favorite: browse a node, or open a conversation."""
        if self.core is None:
            return
        kind = self._favorite_kind_by_id(fid)
        if kind == "node":
            self._open_nomad(fid)
            return
        names = [t.name for t in self.core.transports]
        if kind in ("mc_channel", "mc_peer"):
            if "meshcore" not in names:
                self._log_system(
                    "MeshCore is not configured; can't open this favorite."
                )
                return
            if kind == "mc_channel":
                idx = self._meshcore_channel_index(fid)
                if idx is None:
                    self._log_system(
                        f"channel '#{fid.lstrip('#')}' is not known to this "
                        "device yet — add it with /channel add."
                    )
                    return
                target = f"@{idx}"
            else:  # mc_peer: a contact addressed by its pubkey prefix
                target = fid
            self._select_mode("meshcore")
            self.current_target = target
            self._refresh_threads()
            self._load_thread(target)
            self._update_status()
            return
        if kind == "hash":
            mode = "reticulum" if "reticulum" in names else None
            target = fid
        elif kind == "group":
            # JS8Call @groups: open the group thread in the js8call mode.
            mode = "js8call" if "js8call" in names else next(
                (n for n in names if n != "reticulum"), None
            )
            target = "@" + fid.lstrip("@").upper()
        else:  # callsign
            mode = "js8call" if "js8call" in names else next(
                (n for n in names if n != "reticulum"), None
            )
            target = fid.upper()
        if mode is None:
            self._log_system(f"no transport available to open favorite {fid[:16]}")
            return
        self._select_mode(mode)
        self.current_target = target
        self._refresh_threads()
        self._load_thread(target)
        self._update_status()

    def action_remove_favorite(self) -> None:
        """Remove the selected favorite (Del / Remove button, Favorites view)."""
        if self.core is None or self.view != "favorites":
            return
        try:
            lst = self.query_one("#favorites-list", ListView)
        except Exception:  # noqa: BLE001
            return
        idx = lst.index
        if idx is None or idx >= len(self._fav_keys):
            self._log_system(
                "select a favorite row first (arrow keys), or use /fav rm <id>"
            )
            return
        fid = self._fav_keys[idx]
        if not fid:
            self._log_system(
                "that row is a section header — pick a favorite, or /fav rm <id>"
            )
            return
        if self.core.favorites.remove(fid):
            try:
                self.core.favorites.save(self.core.config)
            except Exception as exc:  # noqa: BLE001
                self._log_system(f"could not save favorites: {exc}")
            self._log_system(f"removed favorite: {fid[:16]}")
            self._refresh_monitor_ticker()
            self._rebuild_monitor()
        self._render_favorites()

    def _add_favorite_from_input(self, raw: str) -> None:
        """Add a favorite typed into the Favorites add-bar (offline-friendly).

        Syntax: ``[type] <id> [label]`` where an optional leading *type* keyword
        pins how the favorite opens/groups (important when the peer/node isn't
        online to auto-classify):

        * ``node`` / ``server`` / ``nomadnet`` -> NomadNet server (page browser)
        * ``peer`` -> messageable LXMF/Reticulum hash
        * ``call`` / ``callsign`` -> HF callsign
        * ``group`` / ``grp`` -> JS8Call group (or just prefix the id with '@')
        * ``channel`` / ``chan`` -> MeshCore channel (by #name)
        * ``contact`` / ``mc`` / ``meshcore`` -> MeshCore contact (pubkey prefix)

        With no type keyword the kind is inferred: a leading ``@`` -> JS8Call
        group, hex -> peer, else callsign.
        """
        if self.core is None:
            return
        kind = ""
        rest = raw.strip()
        first = rest.split(maxsplit=1)[0].lower().rstrip(":") if rest else ""
        if first in ("node", "server", "nomadnet"):
            kind = "node"
        elif first in ("peer",):
            kind = "peer"
        elif first in ("call", "callsign"):
            kind = "callsign"
        elif first in ("group", "grp"):
            kind = "group"
        elif first in ("channel", "chan"):
            kind = "mc_channel"
        elif first in ("contact", "mc", "meshcore"):
            kind = "mc_peer"
        if kind:
            rest = rest.split(maxsplit=1)[1] if " " in rest else ""
        parts = rest.split(maxsplit=1)
        if not parts or not parts[0]:
            self._log_system(
                "usage: [node|peer|call|group|channel|contact] <id> [label]  "
                "(or @GROUP)"
            )
            return
        ident = parts[0]
        # A leading '@' always means a JS8Call group.
        if ident.startswith("@"):
            kind = "group"
        # MeshCore channels are stored by name; drop a decorative leading '#'.
        if kind == "mc_channel":
            ident = ident.lstrip("#") or "public"
        label = parts[1].strip() if len(parts) > 1 else ""
        fav = self.core.favorites.add(ident, label, kind=kind)
        try:
            self.core.favorites.save(self.core.config)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"could not save favorites: {exc}")
        tag = f" [{kind}]" if kind else ""
        self._log_system(f"favorite added{tag}: {fav.display}")
        self._contacts_auto_link_favorite(ident)
        self._refresh_monitor_ticker()
        self._rebuild_monitor()
        self._render_favorites()

    def _favorite_current_conversation(self, label: str = "") -> None:
        """Favorite the open conversation (★ Favorite / '/fav here').

        Tailored to MeshCore: a channel is saved by its ``#name`` (kind
        ``mc_channel``) and a contact by its public-key prefix (kind
        ``mc_peer``), so each reopens straight into the Mesh panel. In other
        modes the target is saved as-is (a ``@group`` or a callsign/hash),
        letting the usual classification group it.
        """
        if self.core is None:
            return
        target = self.current_target
        if not target:
            self._log_system("open a conversation first, then \u2605 Favorite it.")
            return
        kind = ""
        ident = target
        if self.active_transport == "meshcore":
            if target.startswith("@") and target[1:].isdigit():
                idx = int(target[1:])
                name = self._meshcore_channel_name(idx)
                ident = name or ("public" if idx == 0 else f"channel{idx}")
                kind = "mc_channel"
                if not label and name:
                    label = f"#{name}"
            else:
                # A direct MeshCore conversation: the target is a pubkey prefix.
                kind = "mc_peer"
                if not label:
                    label = self._friendly_name(target)
        fav = self.core.favorites.add(ident, label, kind=kind)
        try:
            self.core.favorites.save(self.core.config)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"could not save favorites: {exc}")
        self._refresh_monitor_ticker()
        self._rebuild_monitor()
        if self.view == "favorites":
            self._render_favorites()
        label_part = f" ({fav.label})" if fav.label else ""
        self._log_system(f"\u2605 favorited: {fav.id}{label_part}")

    @work(thread=True)
    def _import_js8_groups(self) -> None:
        """Fetch the operator's JS8Call @groups and add them as favorites.

        Runs the blocking TCP/JSON query in a worker thread so the UI never
        stalls; results are applied back on the main thread.
        """
        if self.core is None:
            return
        js8 = self.core.config.transports.get("js8call", {})
        if not js8.get("enabled", False):
            self.call_from_thread(
                self._log_system, "JS8Call is not enabled in your config."
            )
            return
        from ..core.js8call_query import query_groups

        host = js8.get("host", "127.0.0.1")
        port = int(js8.get("port", 2442))
        self.call_from_thread(
            self._log_system, f"Querying JS8Call for groups at {host}:{port}…"
        )
        groups = query_groups(host, port)
        self.call_from_thread(self._apply_imported_groups, groups)

    def _apply_imported_groups(self, groups: list[str]) -> None:
        """Add fetched @groups as favorites (called on the main thread)."""
        if self.core is None:
            return
        if not groups:
            self._log_system(
                "No JS8Call groups found (is JS8Call running with the TCP API "
                "enabled, and have you joined any groups?)."
            )
            return
        added = 0
        for name in groups:
            ident = f"@{name}"
            if not self.core.favorites.is_favorite(ident):
                added += 1
            self.core.favorites.add(ident, kind="group")
        try:
            self.core.favorites.save(self.core.config)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"could not save favorites: {exc}")
        joined = ", ".join(f"@{g}" for g in groups)
        self._log_system(
            f"Imported {len(groups)} JS8Call group(s); {added} new: {joined}"
        )
        self._refresh_monitor_ticker()
        if self.view == "favorites":
            self._render_favorites()

    # -- mode application -----------------------------------------------------
    def _active_transport_obj(self) -> Transport | None:
        if self.core is None or self.active_transport is None:
            return None
        return next(
            (t for t in self.core.transports if t.name == self.active_transport),
            None,
        )

    def _refresh_compose_limit(self) -> None:
        """Cache the active mode's documented per-message size cap (bytes)."""
        t = self._active_transport_obj()
        self._compose_limit = (
            t.capabilities().max_message_size if t is not None else 0
        )

    def _check_compose_limit(self, text: str) -> bool:
        """Whether ``text`` may be sent on the active transport.

        Measures the UTF-8 *byte* length (one accented/emoji char can be several
        bytes, which is what the on-air framing counts). Hard-blocks anything
        over a transport's documented cap; for soft-limit transports (JS8Call,
        which has no published cap and auto-frames) it only warns and allows it.
        """
        limit = self._compose_limit
        if not limit:
            return True
        size = len(text.encode("utf-8"))
        if size <= limit:
            return True
        over = size - limit
        if self.active_transport in self._SOFT_LIMIT_TRANSPORTS:
            self._log_system(
                f"{size} bytes — JS8Call will send this as several "
                "transmissions (it has no hard length limit)."
            )
            return True
        self._log_system(
            f"Too long for {self.active_transport}: {over} byte(s) over the "
            f"{limit}-byte limit. Trim the message and resend."
        )
        return False

    def _compose_counter_markup(self) -> str:
        """A live ``124/134`` size counter for the active mode's composer.

        Empty unless an operating mode with a size cap is active and the composer
        holds a non-command message. Turns red past a hard cap, yellow past a
        soft (advisory) one.
        """
        limit = self._compose_limit
        if not limit or self.view != "active":
            return ""
        try:
            val = self.query_one("#composer", Input).value
        except Exception:  # noqa: BLE001
            return ""
        if not val or val.lstrip().startswith("/"):
            return ""  # commands are not size-limited
        size = len(val.encode("utf-8"))
        if size <= limit:
            return f"    [dim]{size}/{limit}[/dim]"
        color = (
            "yellow" if self.active_transport in self._SOFT_LIMIT_TRANSPORTS
            else "red"
        )
        return f"    [{color}]{size}/{limit}[/{color}]"

    def _apply_mode(self) -> None:
        # Switching mode resets the open conversation (sending is mode-bound).
        # Channel-based transports (MeshCore) default to their primary channel
        # (the public channel 0) so the panel is ready to send straight away.
        self.current_target = self._default_channel_target()
        self._refresh_compose_limit()
        self.query_one("#messages", RichLog).clear()
        self._refresh_threads()
        self._update_active_banner()
        self._update_modebar()
        self._update_status()
        if self.current_target:
            self._load_thread(self.current_target)
            self._log_system(
                f"Active mode: {self.active_transport} — "
                f"{self._display_id(self.current_target)}"
            )
        elif self.active_transport:
            # No conversation selected: show every message for this mode so the
            # window isn't empty (e.g. the JS8Call firehose).
            self._show_all_messages()
        self._update_composer_placeholder()

    def _default_channel_target(self) -> str | None:
        """Default conversation for the active mode, or None.

        Channel-based transports (MeshCore) start with no channel selected so the
        right panel shows all channels at once — same as JS8Call's all-messages
        firehose. The operator clicks a specific channel to filter, then presses
        Escape to return to the full feed.
        """
        return None

    def _update_modebar(self) -> None:
        """Refresh the persistent mode selector: active highlight + health dots.

        Exactly one chip carries the ``-active`` class - the one whose surface
        is currently on screen. We derive that from :meth:`_current_mode_key`
        (which already encodes "nomadnet wins when view=='nomadnet'") instead
        of comparing chips against ``self.active_transport`` directly, because
        ``active_transport`` is intentionally NOT cleared when switching to
        NomadNet/Watch/Health (so returning to ``active`` view restores the
        last transport). Without that single source of truth, two chips
        (e.g. js8call AND nomadnet) would both highlight at once.
        """
        try:
            bar = self.query_one("#modebar", Horizontal)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        current = self._current_mode_key()
        for btn in bar.query(Button):
            bid = btn.id or ""
            if bid == "mode-nomadnet":
                short = self._MODE_SHORT_LABELS.get("nomadnet", "Nomad")
                btn.label = f"{self._health_dot('nomadnet')} {short}"
                btn.set_class(current == "nomadnet", "-active")
                btn.set_class(
                    self._health.get("nomadnet") is ReachabilityStatus.DOWN,
                    "-down",
                )
            elif bid.startswith("mode-"):
                name = bid[len("mode-"):]
                short = self._MODE_SHORT_LABELS.get(name, name)
                btn.label = f"{self._health_dot(name)} {short}"
                btn.set_class(
                    self.view == "active" and current == name,
                    "-active",
                )
                btn.set_class(
                    self._health.get(name) is ReachabilityStatus.DOWN,
                    "-down",
                )
            elif bid == "view-watch":
                btn.set_class(self.view == "monitor", "-active")
            elif bid == "view-health":
                btn.set_class(self.view == "health", "-active")
            elif bid == "view-logs":
                btn.set_class(self.view == "logs", "-active")
            elif bid == "view-archive":
                btn.set_class(self.view == "archive", "-active")
            elif bid == "view-net":
                btn.set_class(self.view == "net", "-active")
            elif bid == "view-weather":
                btn.set_class(self.view == "weather", "-active")
        self._update_input_indicator()
        self._update_mesh_bar()
        self._update_js8_bar()
        self._update_winlink_bar()
        self._update_wx_bar()
        # View changed -> re-evaluate context bindings (e.g. F4 only on Watch)
        # so the footer shows/hides them correctly.
        self.refresh_bindings()

    def _update_mesh_bar(self) -> None:
        """Show the MeshCore action bar (Announce/Flood) only in Mesh mode."""
        try:
            bar = self.query_one("#mesh-bar", Horizontal)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        bar.display = (
            self.view == "active" and self.active_transport == "meshcore"
        )
        down = self._health.get("meshcore") is ReachabilityStatus.DOWN
        for btn in bar.query(Button):
            if btn.id == "mesh-start":
                btn.disabled = not down
            else:
                btn.disabled = down

    def _winlink_transport(self) -> Transport | None:
        """The live WinlinkTransport instance, or None when not configured."""
        if self.core is None:
            return None
        return next(
            (t for t in self.core.transports if t.name == "winlink"), None
        )

    def _update_winlink_bar(self) -> None:
        """Show the Winlink action bar (Subject/Connect/Gateways) in winlink mode.

        The label summarises the configured connection method + gateway and any
        pending subject, so the operator can see at a glance how the next send
        will be delivered.
        """
        try:
            bar = self.query_one("#winlink-bar", Horizontal)
            label = self.query_one("#winlink-bar-label", Static)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        show = self.view == "active" and self.active_transport == "winlink"
        bar.display = show
        down = self._health.get("winlink") is ReachabilityStatus.DOWN
        for btn in bar.query(Button):
            if btn.id == "winlink-start":
                btn.disabled = not down
            elif btn.id == "winlink-connect":
                btn.disabled = down
            # All other buttons (Compose, Subject, Forms, Outbox, Gateways)
            # work regardless of Pat's state.
        if not show:
            return
        t = self._winlink_transport()
        if t is not None and hasattr(t, "connect_summary"):
            scheme = t.connect_summary()
        else:
            method = getattr(t, "_method", None)
            scheme = method.scheme if method is not None else "telnet"
        gateway = ""
        if t is not None:
            gateway = str(t.config.get("gateway", "")).strip()
        gw_text = gateway or ("CMS" if scheme == "telnet" else "\u2014")
        parts = [f"[b]Winlink[/b] [dim]via[/dim] {scheme}", f"[dim]gw:[/dim]{gw_text}"]
        if self._winlink_subject:
            subj = self._winlink_subject
            if len(subj) > 24:
                subj = subj[:21] + "..."
            parts.append(f"[dim]subj:[/dim]\u201c{subj}\u201d")
        if self._attach_queue:
            parts.append(f"[dim]\U0001f4ce[/dim]{len(self._attach_queue)}")
        label.update("  ".join(parts))

    def _winlink_subject_prompt(self) -> None:
        """Pre-fill the composer with ``/subject `` so the operator can type one."""
        try:
            composer = self.query_one("#composer", Input)
        except Exception:  # noqa: BLE001
            return
        composer.value = "/subject "
        composer.cursor_position = len(composer.value)
        composer.focus()

    @work
    async def _winlink_open_forms(self) -> None:
        """End-to-end Winlink form flow: pick → fill → build → queue to outbox.

        Drives three steps without blocking the UI: a forms picker, a generated
        field form, then ``compose_form`` (which queues to Pat's outbox). Nothing
        is transmitted — the operator presses Connect to send, which is the
        natural review gate (mirrors the ``winlink compose-form`` CLI).
        """
        from ..transports.winlink_transport import detect_form_fields

        t = self._winlink_transport()
        if t is None or not hasattr(t, "list_forms"):
            self._log_system("Winlink is not enabled.")
            return
        self._log_system("Winlink: loading form catalog \u2026")
        try:
            forms = await t.list_forms()
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Winlink form catalog failed: {exc}")
            return
        if not forms:
            self._log_system(
                "No Winlink forms installed. Run 'radioapp winlink forms-update' "
                "to download them."
            )
            return
        template = await self.push_screen_wait(WinlinkFormsScreen(forms))
        if not template:
            return
        try:
            text = await t.get_form_template(template)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Winlink template fetch failed: {exc}")
            return
        fields = detect_form_fields(text or "")
        self._log_system(
            f"Winlink: form [b]{template}[/b] "
            + (f"({len(fields)} fields)" if fields else "(no prompt fields — fill To/Subject)")
        )
        try:
            result = await self.push_screen_wait(
                WinlinkComposeFormScreen(template, fields)
            )
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Winlink form compose failed: {exc}")
            return
        if not result:
            return
        if not getattr(t, "running", False):
            self._log_system("Winlink transport is not running (is Pat reachable?).")
            return
        self._log_system(f"Winlink: building form {template} \u2026")
        try:
            built = await t.compose_form(
                result["template"],
                result["responses"],
                to=result["to"],
                cc=result["cc"],
                subject=result["subject"],
            )
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Winlink form build failed: {exc}")
            return
        if built is None:
            self._log_system(
                "Winlink form build failed (check the template and that Pat is "
                "reachable)."
            )
            return
        self._log_system(
            "Winlink form queued to Pat's outbox \u2014 "
            f"To: {built.get('to') or '(none)'} \u00b7 "
            f"Subj: {built.get('subject') or '(none)'}. "
            "Nothing sent yet; press Connect to transmit."
        )
        self._refresh_active_pane()

    def _update_wx_bar(self) -> None:
        """Dim the JS8 WX query button when JS8Call is not running."""
        try:
            btn = self.query_one("#wx-query", Button)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        down = self._health.get("js8call") is ReachabilityStatus.DOWN
        btn.disabled = down

    @work
    async def _winlink_email_compose(self) -> None:
        """Open the full-screen Winlink email compose modal.

        Pre-fills Subject from any pending /subject, Attachments from the
        current queue, and Callsign/Date/Time for template substitution.
        On submit, builds and queues the message to Pat's outbox. If the user
        picks "ICS Forms →" from the template selector, hands off to the
        existing Pat form flow instead.
        """
        now = datetime.now(UTC)
        callsign = (self.core.station.callsign or "") if self.core else ""
        result = await self.push_screen_wait(
            WinlinkEmailComposeScreen(
                subject=self._winlink_subject,
                attachments=list(self._attach_queue),
                callsign=callsign,
                date_utc=now.strftime("%d %b %Y"),
                time_utc=now.strftime("%H%M"),
            )
        )
        if result is None:
            return
        if result.get("action") == "ics_forms":
            self._winlink_open_forms()
            return
        to = result.get("to") or ""
        if not to:
            return
        body = result.get("body") or ""
        if not body.strip():
            self._log_system("Compose: no message body — nothing queued.")
            return
        me = callsign or "unknown"
        msg = UnifiedMessage.direct(me, to, body)
        if result.get("subject"):
            msg.metadata["subject"] = result["subject"]
        if result.get("cc"):
            msg.metadata["cc"] = result["cc"]
        if result.get("attachments"):
            msg.metadata["attach"] = list(result["attachments"])
        ok = await self.core.router.send(msg, force_transport="winlink")
        self._winlink_subject = ""
        self._attach_queue = []
        self._update_winlink_bar()
        if ok:
            self._log_system(
                f"Winlink email queued to Pat's outbox — "
                f"To: {to} · "
                f"Subj: {result.get('subject') or '(no subject)'}. "
                "Nothing sent yet; press Connect to transmit."
            )
        else:
            self._log_system(f"Winlink: failed to queue email to {to}.")
        self._render_message(msg, outgoing=True, ok=ok)
        self._append_monitor(msg)
        self._refresh_threads()

    def _set_winlink_subject(self, text: str) -> None:
        """Set (or clear) the pending subject for the next Winlink message."""
        self._winlink_subject = text.strip()
        self._update_winlink_bar()
        if self._winlink_subject:
            self._log_system(
                f"Winlink subject set: \u201c{self._winlink_subject}\u201d"
            )
        else:
            self._log_system("Winlink subject cleared.")

    def _set_winlink_gateway(self, gateway: str) -> None:
        """Set the RMS gateway (or CMS target) for subsequent Winlink sessions."""
        t = self._winlink_transport()
        if t is None:
            self._log_system("Winlink is not enabled.")
            return
        t.config["gateway"] = gateway.strip()
        self._update_winlink_bar()
        if gateway.strip():
            self._log_system(f"Winlink gateway set: {gateway.strip()}")
        else:
            self._log_system("Winlink gateway cleared (telnet uses default CMS).")

    def _add_attachment(self, arg: str) -> None:
        """Queue (or list/clear) attachment file paths for the next message.

        ``/attach <path>`` queues a file; ``/attach`` lists the queue;
        ``/attach clear`` empties it. Paths are validated up front so the
        operator finds out immediately if a file is missing. Available on any
        active mode whose transport advertises ``supports_attachments`` (Winlink
        email, Reticulum LXMF); other modes get a hint instead.
        """
        arg = arg.strip()
        t = self._active_transport_obj()
        caps = t.capabilities() if t is not None else None
        if not (caps and caps.supports_attachments):
            self._log_system(
                f"no attachment support on '{self.active_transport or '(none)'}'."
            )
            return
        if not arg:
            if self._attach_queue:
                names = ", ".join(os.path.basename(p) for p in self._attach_queue)
                self._log_system(f"Attachments queued: {names}")
            else:
                self._log_system(
                    "No attachments queued. Use /attach <path> to add one."
                )
            return
        if arg.lower() == "clear":
            self._attach_queue = []
            self._update_winlink_bar()
            self._log_system("Attachments cleared.")
            return
        path = os.path.expanduser(arg)
        if not os.path.isfile(path):
            self._log_system(f"Attachment not found: {arg}")
            return
        self._attach_queue.append(path)
        self._update_winlink_bar()
        self._log_system(
            f"Attachment queued: {os.path.basename(path)} "
            f"({len(self._attach_queue)} total). Send to deliver."
        )

    def _winlink_download_dir(self) -> str:
        """Where saved inbound attachments go.

        A per-transport ``download_dir`` override wins; otherwise the central
        ``[storage].download_dir`` chosen at setup is used, so Winlink downloads
        land with every other mode's.
        """
        t = self._winlink_transport()
        if t is not None:
            configured = str(t.config.get("download_dir", "")).strip()
            if configured:
                return os.path.expanduser(configured)
        if self.core is not None:
            return str(self.core.config.download_dir())
        return os.path.join(
            os.path.expanduser("~"), ".local", "share", "radio_app", "downloads"
        )

    @work(exclusive=True)
    async def _winlink_save_attachments(self) -> None:
        """Download attachments of the latest received mail in the open thread."""
        t = self._winlink_transport()
        if t is None or not hasattr(t, "save_attachments"):
            self._log_system("Winlink is not enabled.")
            return
        if self.core is None or not self.current_target:
            self._log_system("Open a Winlink conversation first.")
            return
        # Find the most recent received message (in this thread) that both has a
        # Pat MID and lists attachments.
        mid = None
        names: list[str] = []
        for msg in reversed(self.core.store.read_thread(self.current_target)):
            if msg.transport != "winlink":
                continue
            m = msg.metadata.get("mid")
            atts = msg.metadata.get("attachments") or []
            if m and atts:
                mid, names = str(m), [str(a) for a in atts]
                break
        if not mid:
            self._log_system("No received attachments in this conversation.")
            return
        dest = self._winlink_download_dir()
        self._log_system(f"Saving {len(names)} attachment(s) to {dest} \u2026")
        try:
            saved = await t.save_attachments(mid, dest)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Attachment download failed: {exc}")
            return
        if saved:
            self._log_system("Saved: " + ", ".join(saved))
        else:
            self._log_system("No attachments were saved.")

    def action_pick_gateway(self, call: str = "") -> None:
        """Pick an RMS gateway (clicked in the /gateways list) and connect."""
        if not call:
            return
        self._set_winlink_gateway(call)
        self._winlink_connect(call)

    @work(exclusive=True)
    async def _winlink_connect(self, gateway: str | None = None) -> None:
        """Trigger a Pat session to deliver the outbox and receive new mail."""
        t = self._winlink_transport()
        if t is None or not hasattr(t, "connect_now"):
            self._log_system("Winlink is not enabled.")
            return
        url = None
        if gateway:
            scheme = getattr(t, "_method", None)
            scheme = scheme.scheme if scheme is not None else "telnet"
            url = f"{scheme}://{gateway}"
        target = url or (
            t.connect_summary() if hasattr(t, "connect_summary")
            else t.build_connect_url()
        )
        # Radio interlock: a Winlink RF session keys the shared radio. Refuse to
        # start one while JS8Call/Mercury holds the radio, and warn if another
        # radio app is still running (it would key over this session).
        session_rf = bool(t.capabilities().uses_shared_radio)
        if session_rf and self.core is not None:
            il = self.core.radio_interlock
            dec = il.acquire("winlink")
            if not dec.granted:
                self._log_system(
                    f"\u26d4 radio busy: {dec.blocked_by} is using the radio. "
                    f"Switch away from {dec.blocked_by} (or stop its TX) before an "
                    "RF Winlink session — or use a telnet path."
                )
                return
            others = self._running_radio_contenders(exclude="winlink")
            if others:
                self._log_system(
                    f"\u26a0 radio: {', '.join(others)} still running — make sure "
                    "it's idle during this RF session so it doesn't key over Winlink."
                )
        self._log_system(f"Winlink: connecting via {target} \u2026")
        # Report how many messages are queued to send, so the operator knows a
        # send is expected (and we can summarise how many actually went out).
        queued_before: int | None = None
        if hasattr(t, "outbox_count"):
            try:
                queued_before = await t.outbox_count()
            except Exception:  # noqa: BLE001
                queued_before = None
        if queued_before:
            self._log_system(
                f"Winlink: {queued_before} message(s) queued to send."
            )
        elif queued_before == 0:
            self._log_system("Winlink: outbox empty — checking for new mail.")
        # Best-effort live progress from Pat's WebSocket while the session runs.
        stop = asyncio.Event()
        stream_task = None
        # Track per-message transfers seen so we can summarise send/receive even
        # if Pat's final counts are terse.
        seen: dict[str, set[str]] = {"sent": set(), "recv": set()}
        # Suppress Pat's replayed log backlog (it tails its log file to new WS
        # clients) until the session is actually live.
        log_state: dict[str, bool] = {"live": False}
        # The raw Pat log transcript (LogLine) is both verbose and replayed to
        # new WS clients (so it shows outdated backlog). Hide it by default and
        # rely on the concise structured Status/Progress/Notification events plus
        # the final "sent N, received M" summary. Operators who want the full
        # Pat transcript can opt in with [transports.winlink] verbose_session_log.
        verbose = bool(
            (getattr(t, "config", {}) or {}).get("verbose_session_log", False)
        )
        if hasattr(t, "stream_events"):
            def _on_event(ev: dict) -> None:
                if not self._winlink_event_is_live(ev, log_state):
                    return
                if "LogLine" in ev and not verbose:
                    return
                prog = ev.get("Progress")
                if isinstance(prog, dict) and prog.get("done"):
                    mid = str(prog.get("mid") or "").strip()
                    if prog.get("sending"):
                        seen["sent"].add(mid or f"s{len(seen['sent'])}")
                    elif prog.get("receiving"):
                        seen["recv"].add(mid or f"r{len(seen['recv'])}")
                line = self._format_winlink_event(ev)
                if line:
                    self._log_system(line)

            async def _pump() -> None:
                try:
                    await t.stream_events(
                        _on_event, stop.is_set,
                        on_prompt=self._winlink_password_prompt,
                    )
                except Exception:  # noqa: BLE001 - feedback is optional
                    pass

            stream_task = asyncio.create_task(_pump())
        try:
            received = await t.connect_now(url)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Winlink connect failed: {exc}")
            return
        finally:
            stop.set()
            if stream_task is not None:
                stream_task.cancel()
            # Release the radio claim taken for this RF session.
            if session_rf and self.core is not None:
                self.core.radio_interlock.release("winlink")
        # Work out how many messages actually went out: the outbox delta is the
        # authoritative "sent" count (forwarded messages leave the outbox); fall
        # back to the per-message transfers we watched stream by.
        queued_after: int | None = None
        if hasattr(t, "outbox_count"):
            try:
                queued_after = await t.outbox_count()
            except Exception:  # noqa: BLE001
                queued_after = None
        if queued_before is not None and queued_after is not None:
            sent = max(0, queued_before - queued_after)
        else:
            sent = len(seen["sent"])
        # connect_now's NumReceived is authoritative for received; cross-check
        # with the transfers we saw.
        got = max(int(received or 0), len(seen["recv"]))
        self._log_system(
            f"\u2713 Winlink session complete \u2014 sent {sent}, received {got}."
        )
        if queued_after:
            self._log_system(
                f"[dim]Winlink: {queued_after} message(s) still queued "
                "(not forwarded this session).[/dim]"
            )
        self._refresh_active_pane()

    async def _winlink_password_prompt(self, prompt: dict) -> str | None:
        """Answer Pat's mid-session secure-login password prompt (option A).

        Pops a masked input and returns what the operator types, kept in memory
        only for this session (never written to config/disk). Returns ``None`` on
        cancel or if the operator doesn't respond before Pat's ~60s timeout, so
        Pat falls back to its own handling. Mirrors Pat's prompt message so the
        operator sees exactly which callsign/account is being authenticated.
        """
        message = str(prompt.get("message") or "Enter Winlink secure-login password")
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str | None] = loop.create_future()

        def _done(value: str | None) -> None:
            if not future.done():
                future.set_result(value)

        self.push_screen(PasswordPromptScreen(message), _done)
        try:
            # Stay within Pat's 60s prompt window; decline if it elapses.
            return await asyncio.wait_for(future, timeout=55)
        except TimeoutError:
            self._log_system("Winlink: password prompt timed out.")
            return None

    @staticmethod
    def _winlink_event_is_live(ev: dict, state: dict) -> bool:
        """Filter Pat's replayed log backlog from genuine live session events.

        When a WebSocket client connects, Pat *tails its log file* and replays
        recent historical lines (``LogLine``) before the session begins — that's
        old data, not this session. Real-time events (``Status`` dialing/connected
        and ``Progress``) are pushed live, never replayed, so the first of those
        marks the session as live (``state['live'] = True``). LogLines are shown
        only once live; every non-LogLine event always passes. (Timestamps in the
        log can't be trusted to filter — the Pat host's clock/timezone may differ
        from ours.)
        """
        status = ev.get("Status")
        if isinstance(status, dict) and (
            status.get("dialing") or status.get("connected")
        ):
            state["live"] = True
        if ev.get("Progress") is not None or ev.get("Notification") is not None:
            state["live"] = True
        if "LogLine" in ev:
            return bool(state.get("live"))
        return True

    @staticmethod
    def _format_winlink_event(ev: dict) -> str | None:
        """Turn one Pat ``/ws`` event into a progress line (or None to suppress).

        Surfaces the session in detail: per-message transfer progress (with an
        up/down arrow for send vs receive), connection state, notifications, and
        the live Pat log transcript (``LogLine``) so the operator sees exactly
        what the session is doing. Keepalive pings and mailbox-changed events are
        suppressed.
        """
        prog = ev.get("Progress")
        if isinstance(prog, dict):
            subj = str(prog.get("subject") or "").strip() or "(no subject)"
            if prog.get("sending"):
                arrow, verb = "\u2191", "send"
            elif prog.get("receiving"):
                arrow, verb = "\u2193", "recv"
            else:
                arrow, verb = "\u2022", ""
            head = f"Winlink {arrow} {verb}".rstrip()
            if prog.get("done"):
                return f"{head} done: {subj}"
            total = int(prog.get("bytes_total") or 0)
            xfer = int(prog.get("bytes_transferred") or 0)
            amount = f"{(100 * xfer // total)}% of {total}B" if total else f"{xfer}B"
            return f"{head} {amount}: {subj}"
        status = ev.get("Status")
        if isinstance(status, dict):
            if status.get("dialing"):
                return "Winlink: dialing\u2026"
            if status.get("connected"):
                ra = str(status.get("remote_addr") or "").strip()
                return f"Winlink: connected{(' to ' + ra) if ra else ''}"
            return None
        note = ev.get("Notification")
        if isinstance(note, dict):
            text = " \u2014 ".join(
                x for x in (note.get("title"), note.get("body")) if x
            )
            return f"Winlink \U0001f4e8 {text}" if text else None
        # The live Pat log transcript — the verbose, Pat-terminal-style detail.
        line = ev.get("LogLine")
        if isinstance(line, str) and line.strip():
            return f"[dim]pat\u2502 {line.strip()}[/dim]"
        return None

    @work(exclusive=True)
    async def _winlink_list_gateways(self) -> None:
        """List nearby RMS gateways from Pat (``/api/rmslist``)."""
        t = self._winlink_transport()
        if t is None or not hasattr(t, "list_gateways"):
            self._log_system("Winlink is not enabled.")
            return
        self._log_system("Winlink: fetching RMS gateway list \u2026")
        try:
            gateways = await t.list_gateways()
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Winlink gateway list failed: {exc}")
            return
        if not gateways:
            self._log_system(
                "No gateways returned (telnet mode, or none in range/cached)."
            )
            return
        self._log_system(f"Nearby RMS gateways ({len(gateways)} shown):")
        log = self.query_one("#messages", RichLog)
        for gw in gateways[:15]:
            call = str(gw.get("callsign") or gw.get("Callsign") or "?")
            mode = gw.get("mode") or gw.get("Mode") or ""
            dist = gw.get("distance") or gw.get("Distance") or ""
            extra = " ".join(str(x) for x in (mode, dist) if x)
            # Make each callsign clickable: clicking picks it as the gateway and
            # starts a session. Strip quotes so it can't break the action arg.
            safe = call.replace("'", "").replace("\\", "")
            log.write(
                f"  [b][@click=app.pick_gateway('{safe}')]{call}[/][/b]  "
                f"[dim]{extra}[/dim]"
            )
        self._log_system(
            "Click a callsign above, or use [b]/gateway <CALL>[/b] then "
            "[b]Connect[/b]."
        )

    @work
    async def _winlink_show_outbox(self) -> None:
        """Show Pat's outbox queue in the message log."""
        t = self._winlink_transport()
        if t is None or not hasattr(t, "list_outbox"):
            self._log_system("Winlink is not enabled.")
            return
        try:
            items = await t.list_outbox()
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"Winlink outbox fetch failed: {exc}")
            return
        if not items:
            self._log_system("Winlink outbox is empty.")
            return
        self._log_system(f"Winlink outbox ({len(items)} queued — Connect to send):")
        log = self.query_one("#messages", RichLog)
        for item in items:
            to = item.get("to") or "(no address)"
            subj = item.get("subject") or "(no subject)"
            mid = item.get("mid") or ""
            size = item.get("size") or 0
            size_str = f"{size // 1024} KB" if size >= 1024 else f"{size} B"
            log.write(
                f"  [b]To:[/b] {to}  [b]Subj:[/b] {subj}  "
                f"[dim]{size_str} · {mid}[/dim]"
            )

    def _js8_transport(self) -> Transport | None:
        """The live JS8CallTransport instance, or None when not configured."""
        if self.core is None:
            return None
        return next(
            (t for t in self.core.transports if t.name == "js8call"), None
        )

    def _update_js8_bar(self) -> None:
        """Show the JS8 band bar (current freq/band + band switches) in JS8 mode.

        The dial frequency is whatever the transport last learned from JS8Call's
        RIG.FREQ events; the Health probe timer keeps it warm by re-querying.
        """
        try:
            bar = self.query_one("#js8-bar", Horizontal)
            label = self.query_one("#js8-bar-label", Static)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        show = self.view == "active" and self.active_transport == "js8call"
        bar.display = show
        down = self._health.get("js8call") is ReachabilityStatus.DOWN
        for btn in bar.query(Button):
            if btn.id == "js8-start":
                btn.disabled = not down
            else:
                btn.disabled = down
        # The bottom one-click query bar (SNR?/HEARING?/STATUS?/INFO?) tracks the
        # band bar's visibility - both belong to the JS8 chat panel.
        try:
            qbar = self.query_one("#js8-query-bar", Horizontal)
            qbar.display = show
            for btn in qbar.query(Button):
                btn.disabled = down
        except Exception:  # noqa: BLE001 - not mounted yet
            pass
        if not show:
            return
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            label.update("JS8Call [dim]— not running[/dim]")
            return
        hz = getattr(t, "dial_freq", None)
        band = t.current_band()
        if hz:
            label.update(f"JS8Call  [b]{hz / 1e6:.3f} MHz[/b] [dim]({band or '?'})[/dim]")
        else:
            label.update("JS8Call  [dim]freq unknown — \u21bb to query[/dim]")
        for btn in bar.query(Button):
            if btn.id and btn.id.startswith("js8-band-"):
                btn.set_class(btn.id == f"js8-band-{band}", "-active")

    @work
    async def _js8_refresh_freq(self) -> None:
        """Re-query JS8Call for the current dial frequency and refresh the bar."""
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running.")
            return
        await t.request_dial_freq()
        # Give JS8Call a beat to reply (its RIG.FREQ event updates the cache).
        await asyncio.sleep(0.4)
        self._update_js8_bar()

    def _js8_switch_band(self, band: str) -> None:
        """Switch JS8Call to a named band's standard dial frequency."""
        hz = dial_for_band(band)
        if hz is None:
            self._log_system(f"unknown band '{band}'.")
            return
        self._js8_set_freq(hz)

    def _js8_send_query(self, name: str) -> None:
        """Send a one-click JS8 directed query (e.g. ``SNR?``) to the open chat.

        The query goes to whatever conversation is open — a callsign for a direct
        query, or an ``@GROUP`` to ask the whole group — using the same path as
        typing it, so the target prefix is added automatically.
        """
        if self.active_transport != "js8call":
            self._log_system("Switch to the JS8Call mode first (press F3).")
            return
        if not self.current_target:
            self._log_system(
                "Open a conversation first (/to <callsign|@GROUP>)."
            )
            return
        self._send(f"{name}?")

    @work
    async def _js8_send_cq(self) -> None:
        """Transmit a CQ call (📣 CQ button)."""
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running — start it first.")
            return
        callsign = ""
        if self.core is not None:
            callsign = str(
                t.config.get("callsign")
                or self.core.config.station.get("callsign", "")
                or ""
            )
        ok = await t.send_cq(callsign)
        if ok:
            self._log_system("\U0001f4e3 CQ sent.")
        else:
            self._log_system("CQ failed — check JS8Call connection.")

    @work
    async def _js8_send_hb(self) -> None:
        """Transmit a JS8Call heartbeat (💓 HB button)."""
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running — start it first.")
            return
        grid = ""
        if self.core is not None:
            grid = str(self.core.config.station.get("grid_square", "") or "")
        ok = await t.send_heartbeat(grid)
        if ok:
            self._log_system("\U0001f493 Heartbeat sent (@HB).")
        else:
            self._log_system("Heartbeat failed — check JS8Call connection.")

    @work
    async def _js8_send_beacon(self) -> None:
        """Send a JS8Call position beacon (sets STATION.SET_GRID in JS8Call)."""
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running — start it first.")
            return
        from ..core.position import position_from_config
        pos = self._position
        if pos is None:
            pos = position_from_config(self.core.config) if self.core else None
        if pos is None:
            self._log_system(
                "No position set. Use /position <grid> to configure one."
            )
            return
        ok = await t.send_position_beacon(pos)
        if ok:
            self._log_system(
                f"\U0001f4cd Beacon sent: grid [b]{pos.grid}[/b] set in JS8Call."
            )
        else:
            self._log_system("Beacon failed — check JS8Call connection.")

    @work
    async def _js8_show_inbox(self) -> None:
        """``/inbox`` — list the messages JS8Call is holding for store-and-forward."""
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running.")
            return
        await t.request_inbox()
        await asyncio.sleep(1.2)  # let the async INBOX.MESSAGES reply land
        msgs = t.inbox_messages()
        if not msgs:
            self._log_system("JS8Call inbox is empty.")
            return
        self._log_system(f"JS8Call inbox ({len(msgs)}):")
        for m in msgs:
            who = f"{m['from'] or '?'} \u2192 {m['to'] or '?'}"
            self._log_system(f"  [{who}] {m['text']}")

    @work
    async def _js8_relay(self, arg: str) -> None:
        """``/relay <CALL> <text>`` — leave a store-and-forward message in JS8Call."""
        if self.active_transport != "js8call":
            self._log_system("Switch to the JS8Call mode first (press F3).")
            return
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            self._log_system("usage: /relay <CALL> <message text>")
            return
        call, body = parts[0], parts[1]
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running.")
            return
        if await t.store_relay_message(call, body):
            self._log_system(
                f"Stored relay for {call.upper()} \u2014 JS8Call will forward it "
                "when it next hears that station."
            )
        else:
            self._log_system("Could not store the relay message.")

    def _js8_sms_prompt(self) -> None:
        """SMS push-button: pre-fill the composer with ``/sms `` to type into.

        Switches to JS8Call mode first if needed, since the APRS gateway send
        only works there. The operator then types ``<phone> <message>``.
        """
        if self.active_transport != "js8call":
            self._select_mode("js8call")
        try:
            composer = self.query_one("#composer", Input)
        except Exception:  # noqa: BLE001
            return
        composer.value = "/sms "
        composer.cursor_position = len(composer.value)
        composer.focus()
        self._log_system(
            "SMS via APRS gateway \u2014 type: /sms <phone> <message>  "
            "[dim](relayed by JS8Call \u2192 SMSGTE)[/dim]"
        )

    @work
    async def _js8_send_sms(self, arg: str) -> None:
        """``/sms <phone> <text>`` — text a phone via JS8Call's APRS gateway."""
        if self.active_transport != "js8call":
            self._log_system("Switch to the JS8Call mode first (press F3).")
            return
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            self._log_system("usage: /sms <phone> <message text>")
            return
        phone, body = parts[0], parts[1]
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running.")
            return
        if await t.send_sms(phone, body):
            self._log_system(
                f"\u2709 SMS to {phone} queued via APRS (SMSGTE) \u2014 "
                "JS8Call will transmit it on the next cycle."
            )
        else:
            self._log_system(
                "Could not send the SMS. Check the number and that JS8Call's "
                "APRS gateway is enabled."
            )

    @work
    async def _js8_directed_cmd(self, arg: str) -> None:
        """``/cmd [<CALL|@GROUP>] <COMMAND>`` — send a JS8 directed command."""
        if self.active_transport != "js8call":
            self._log_system("Switch to the JS8Call mode first (press F3).")
            return
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running.")
            return
        parts = arg.split()
        if len(parts) == 1 and self.current_target:
            target, command = self.current_target, parts[0]
        elif len(parts) >= 2:
            target, command = parts[0], parts[1]
        else:
            self._log_system("usage: /cmd [<CALL|@GROUP>] <SNR?|GRID?|INFO?|...>")
            return
        if await t.send_directed_command(target, command):
            self._log_system(f"Sent directed command: {target.upper()} "
                             f"{command.upper()}")
        else:
            self._log_system(f"Unknown/failed JS8 command: {command}")

    @work
    async def _js8_set_freq(self, hz: int) -> None:
        """Move the radio's dial via JS8Call (requires CAT/rig control there)."""
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("JS8Call is not running; cannot change frequency.")
            return
        ok = await t.set_dial_freq(hz)
        band = band_for_freq(hz) or "?"
        if ok:
            self._log_system(
                f"JS8Call \u2192 {hz / 1e6:.3f} MHz ({band}). "
                "[dim](needs CAT/rig control enabled in JS8Call)[/dim]"
            )
            await t.request_dial_freq()
            await asyncio.sleep(0.4)
            self._update_js8_bar()
        else:
            self._log_system("JS8Call frequency change failed (see logs).")

    def _handle_freq_command(self, arg: str) -> None:
        """``/freq`` shows the current dial freq; ``/freq <MHz|Hz>`` sets it."""
        if self.active_transport != "js8call":
            self._log_system("Switch to the JS8Call mode first (press F3).")
            return
        if not arg:
            self._js8_refresh_freq()
            return
        hz = _parse_freq_to_hz(arg)
        if hz is None:
            self._log_system("usage: /freq <MHz e.g. 14.078 | Hz e.g. 14078000>")
            return
        self._js8_set_freq(hz)

    def _handle_band_command(self, arg: str) -> None:
        """``/band`` lists bands; ``/band <name>`` switches (e.g. /band 20m)."""
        if self.active_transport != "js8call":
            self._log_system("Switch to the JS8Call mode first (press F3).")
            return
        if not arg:
            self._log_system(
                "Bands: " + ", ".join(JS8_BAND_DIAL_HZ) + "  — /band <name>"
            )
            return
        self._js8_switch_band(arg.strip())

    def _build_mode_selector(self) -> None:
        """Create one button per transport (mode) plus Stream/Health controls."""
        if self.core is None:
            return
        bar = self.query_one("#modebar", Horizontal)
        by_name = {t.name: t for t in self.core.transports}
        for key in self._mode_keys():
            label = self._MODE_SHORT_LABELS.get(key, key)
            bar.mount(Button(label, id=f"mode-{key}", classes="modebtn"))
        bar.mount(Static("", id="modebar-spacer"))
        bar.mount(Button("\u25f7 Stream", id="view-watch", classes="modebtn"))
        bar.mount(Button("\u2795 Health", id="view-health", classes="modebtn"))
        bar.mount(Button("\U0001f5c2 History", id="view-archive", classes="modebtn"))
        bar.mount(Button("\u25ce Net", id="view-net", classes="modebtn"))
        bar.mount(Button("\u26c5 WX", id="view-weather", classes="modebtn"))
        bar.mount(Static("\u2328", id="input-ind"))

    def _health_dot(self, name: str) -> str:
        """Glyph for a transport's last reachability probe result."""
        status = self._health.get(name)
        if status is ReachabilityStatus.OK:
            return "[green]\u25cf[/green]"      # ● up
        if status is ReachabilityStatus.DOWN:
            return "[red]\u25cb[/red]"          # ○ down
        if status is ReachabilityStatus.NOT_APPLICABLE:
            return "[dim]\u00b7[/dim]"          # · n/a
        return "[dim]\u25cc[/dim]"              # ◌ unknown/not probed yet

    @work(exclusive=True)
    async def _refresh_health(self) -> None:
        """Passively probe each transport's endpoint (no transmission)."""
        if self.core is None:
            return
        for t in self.core.transports:
            try:
                self._health[t.name] = await t.check_reachable()
            except Exception:  # noqa: BLE001 - a probe must never crash the UI
                self._health[t.name] = ReachabilityStatus.DOWN
            # Transports that expose richer device telemetry (e.g. MeshCore
            # battery + radio params) get a passive snapshot for the Health
            # board. Only worth fetching when the endpoint is reachable.
            if (
                hasattr(t, "device_telemetry")
                and self._health.get(t.name) is ReachabilityStatus.OK
            ):
                try:
                    self._device_telemetry[t.name] = await t.device_telemetry()
                except Exception:  # noqa: BLE001 - telemetry must never crash UI
                    self._device_telemetry[t.name] = {}
            # Keep the JS8 dial-frequency cache warm so the band bar stays
            # current (the reply arrives asynchronously as a RIG.FREQ event).
            if (
                t.name == "js8call"
                and self._health.get(t.name) is ReachabilityStatus.OK
                and hasattr(t, "request_dial_freq")
            ):
                try:
                    await t.request_dial_freq()
                except Exception:  # noqa: BLE001 - never crash the probe timer
                    pass
                # Also pull the fuller operating snapshot (speed + selected
                # callsign) for the Health board; the reply arrives async as a
                # STATION.STATUS event.
                if hasattr(t, "radio_status"):
                    try:
                        await t.radio_status()
                    except Exception:  # noqa: BLE001 - never crash the probe timer
                        pass
            # Winlink: probe each connection path's endpoint (telnet via Pat,
            # varahf/Mercury @8300, ardop @8515) so the Health board can show
            # which modems are up — even when they're expected to be down.
            if t.name == "winlink" and hasattr(t, "path_status"):
                try:
                    self._winlink_paths = await t.path_status()
                except Exception:  # noqa: BLE001 - never crash the probe timer
                    self._winlink_paths = []
        # NomadNet is a virtual mode riding Reticulum: it inherits Reticulum's
        # reachability, or is N/A when Reticulum is not configured at all.
        if "reticulum" in self._health:
            self._health["nomadnet"] = self._health["reticulum"]
        else:
            self._health["nomadnet"] = ReachabilityStatus.NOT_APPLICABLE

        # Time consensus check — GPS → local NTP → internet NTP → system.
        # At most every 60 s; run in a thread to avoid blocking the event loop.
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        now_mono = loop.time()
        if now_mono - self._last_time_check >= 60.0:
            self._last_time_check = now_mono
            try:
                from ..core.timesource import TimeConsensus
                pos_cfg = self.core.config.data.get("position", {}) if self.core else {}
                tc = TimeConsensus(
                    gpsd_host=pos_cfg.get("gpsd_host", "127.0.0.1"),
                    gpsd_port=int(pos_cfg.get("gpsd_port", 2947)),
                    timeout=1.5,
                    wsjtx_monitor=self.core.wsjtx_monitor if self.core else None,
                )
                self._time_reading = await _asyncio.wait_for(
                    loop.run_in_executor(None, tc.best_reading),
                    timeout=5.0,
                )
                self._time_queried = True
            except Exception:  # noqa: BLE001 - never crash the health probe
                self._time_reading = None
                self._time_queried = True

        # GPS position refresh — only when gpsd is explicitly enabled in config.
        if (
            self.core is not None
            and self.core.config.data.get("position", {}).get("gpsd_enabled", False)
        ):
            try:
                from ..core.position import GPSReader
                gpsd_host = self.core.config.position.get("gpsd_host", "127.0.0.1")
                gpsd_port = int(self.core.config.position.get("gpsd_port", 2947))
                reader = GPSReader(host=gpsd_host, port=gpsd_port, timeout=3.0)
                self._position = await _asyncio.wait_for(
                    loop.run_in_executor(None, reader.read),
                    timeout=4.0,
                )
            except Exception:  # noqa: BLE001 - GPS failure must never crash health probe
                pass

        self._update_modebar()
        if self.view == "health":
            self._render_health()

    @work(exclusive=True)
    async def _check_scheduled(self) -> None:
        """Fire any messages whose scheduled send time has arrived."""
        if self.core is None:
            return
        now = datetime.now(UTC)
        try:
            pending = self.core.store.schedule_pending(up_to=now)
        except Exception:  # noqa: BLE001
            return
        for entry in pending:
            kind = entry.message.metadata.get("kind")
            if kind == "band_change":
                ok = await self._fire_scheduled_band_change(entry)
                self.core.store.schedule_mark_sent(entry.id, success=ok)
                if ok and entry.message.metadata.get("recur_daily"):
                    self._reschedule_daily_band_change(entry)
            else:
                ok = await self.core.router.send(
                    entry.message, force_transport=entry.transport
                )
                self.core.store.schedule_mark_sent(entry.id, success=ok)
                status_str = "sent" if ok else "[red]FAILED[/red]"
                preview = entry.message.content[:40]
                self._log_system(f"Scheduled message {status_str}: {preview!r}")

    # -- input-mode detection -------------------------------------------------
    def _note_input(self, mode: str) -> None:
        if mode != self._input_mode:
            self._input_mode = mode
            self._update_input_indicator()

    def _update_input_indicator(self) -> None:
        try:
            ind = self.query_one("#input-ind", Static)
        except Exception:  # noqa: BLE001
            return
        glyph = "\u2328" if self._input_mode == "key" else "\u261e"
        touch = " touch" if self._touch_layout else ""
        ind.update(f"{glyph}{touch}")

    # -- per-mode command history ---------------------------------------------
    def _hist_key(self) -> str:
        """History bucket for the current mode."""
        if self.view == "active" and self.active_transport:
            return self.active_transport
        return self.view or "active"

    def _push_cmd_history(self, text: str) -> None:
        if not text or not self._cmd_hist_limit:
            return
        key = self._hist_key()
        hist = self._cmd_history.get(key)
        if hist is None:
            hist = deque(maxlen=self._cmd_hist_limit)
            self._cmd_history[key] = hist
        if not hist or hist[-1] != text:
            hist.append(text)
        self._cmd_hist_pos[key] = -1
        self._cmd_hist_draft.pop(key, None)

    def _navigate_cmd_history(self, back: bool) -> None:
        try:
            composer = self.query_one("#composer", Input)
        except Exception:  # noqa: BLE001
            return
        key = self._hist_key()
        hist = self._cmd_history.get(key)
        if not hist:
            return
        pos = self._cmd_hist_pos.get(key, -1)
        if back:
            if pos == -1:
                self._cmd_hist_draft[key] = composer.value
                pos = len(hist) - 1
            elif pos > 0:
                pos -= 1
            else:
                return  # already at oldest entry
        else:
            if pos == -1:
                return  # nothing to go forward to
            if pos < len(hist) - 1:
                pos += 1
            else:
                pos = -1
                composer.value = self._cmd_hist_draft.pop(key, "")
                composer.cursor_position = len(composer.value)
                self._cmd_hist_pos[key] = pos
                return
        self._cmd_hist_pos[key] = pos
        composer.value = hist[pos]
        composer.cursor_position = len(composer.value)

    def on_key(self, event) -> None:  # noqa: ANN001 - Textual event
        self._note_input("key")
        if event.key in ("up", "down"):
            try:
                composer = self.query_one("#composer", Input)
            except Exception:  # noqa: BLE001
                return
            if composer.has_focus:
                self._navigate_cmd_history(event.key == "up")
                event.prevent_default()
                event.stop()

    def on_click(self, event) -> None:  # noqa: ANN001 - Textual event
        self._note_input("pointer")
        # Tapping the input-mode glyph toggles the touch-friendly (larger) layout.
        widget = getattr(event, "widget", None)
        if widget is not None and getattr(widget, "id", None) == "input-ind":
            self._toggle_touch_layout()

    def _toggle_touch_layout(self) -> None:
        self._touch_layout = not self._touch_layout
        self.set_class(self._touch_layout, "-touch")
        self._update_input_indicator()

    @staticmethod
    def _short(s: str) -> str:
        return s if len(s) <= 12 else s[:12] + "..."

    @staticmethod
    def _looks_like_hash(s: str) -> bool:
        """True for a long hex string (an RNS/LXMF destination hash)."""
        return len(s) >= 16 and all(c in "0123456789abcdefABCDEF" for c in s)

    def _friendly_name(self, key: str) -> str:
        """A saved friendly name (favorite label) for an id, or '' if none."""
        if self.core is None or not key:
            return ""
        fav = self.core.favorites.match(key)
        return fav.label if fav is not None else ""

    def _normalize_target(self, target: str) -> str:
        """Normalize a typed conversation target for the active transport.

        Amateur callsigns (JS8Call/Mercury) are case-insensitive and shown
        upper-case, so we upper them. MeshCore contacts (names + hex pubkey
        prefixes) and Reticulum hashes are case-sensitive, so they are kept
        verbatim. ``@groups``/channels are always kept as typed.
        """
        target = target.strip()
        if target.startswith("@"):
            return target
        if self.active_transport == "js8call":
            return target.upper()
        return target

    def _display_id(self, key: str) -> str:
        """Readable form of a conversation id for display.

        A saved friendly name replaces the raw id entirely; otherwise a long hex
        hash is truncated so it stays readable. Callsigns and @groups are shown
        unchanged.
        """
        if not key:
            return key
        name = self._friendly_name(key)
        if name:
            return name
        # MeshCore-style channel tags ('@<index>') render as a friendly '#name'
        # when the active transport names the channel.
        channel = self._channel_display(key)
        if channel:
            return channel
        if self._looks_like_hash(key):
            return key[:10] + "\u2026"
        return key

    def _channel_display(self, key: str) -> str:
        """Friendly label for a group-channel tag of the active transport.

        Returns e.g. ``#ops (ch 1)`` for '@1' when the active mode is a
        channel-capable transport (MeshCore) that knows that channel, or '' when
        the key is not a channel tag / the mode has no channels.
        """
        if self.core is None or not key.startswith("@") or not key[1:].isdigit():
            return ""
        active = self._active_transport_obj()
        if active is None or not hasattr(active, "channels"):
            return ""
        idx = int(key[1:])
        for ch in active.channels():
            if ch["index"] == idx:
                name = ch.get("name") or ("public" if idx == 0 else f"channel {idx}")
                return f"#{name} [dim](ch {idx})[/dim]"
        return ""

    # -- thread list (scoped to active mode) ----------------------------------
    def _refresh_threads(self) -> None:
        if self.core is None:
            return
        view = self.query_one("#threads", ListView)
        keys: list[str] = []
        active_group_tags: set[str] = set()
        # Conversations we've actually exchanged messages with (stored), so the
        # pane can rank them above contacts we've only opened/saved.
        dialog_keys: set[str] = set()
        if self.active_transport is not None:
            for k, _, _ in self.core.store.threads():
                if self.core.store.thread_transport(k) == self.active_transport:
                    keys.append(k)
                    dialog_keys.add(k)
            for group in self.core.groups.all():
                if self.active_transport in group.transports:
                    active_group_tags.add(group.tag)
                    if group.tag not in keys:
                        keys.append(group.tag)
            # MeshCore (and any transport that exposes channels) lists its group
            # channels as conversations up front, so the operator can drop into a
            # channel before any traffic has arrived. Channel tags are '@<index>'
            # to match the group/channel addressing used on send & receive.
            active = self._active_transport_obj()
            if active is not None and hasattr(active, "channels"):
                for ch in active.channels():
                    tag = f"@{ch['index']}"
                    if tag not in keys:
                        keys.append(tag)
            # Group favorites (e.g. JS8Call @groups saved in Favorites) ALWAYS
            # belong in the left pane for group-capable, non-channel transports,
            # so every group you track is visible whether or not you've exchanged
            # any traffic with it. (MeshCore's groups are numeric channels handled
            # above, so it is excluded via the channels() check.)
            caps = active.capabilities() if active is not None else None
            if caps is not None and caps.supports_groups and not hasattr(
                active, "channels"
            ):
                for fav in self.core.favorites.all():
                    if fav.kind != "group" and not fav.id.startswith("@"):
                        continue
                    tag = fav.id if fav.id.startswith("@") else f"@{fav.id}"
                    active_group_tags.add(tag)
                    if tag not in keys:
                        keys.append(tag)
            # Remember the open conversation, then restore every conversation
            # opened in this mode so the left pane persists across mode switches
            # (conversations with no stored messages would otherwise vanish).
            self._remember_thread(self.current_target)
            for key in self._opened_threads.get(self.active_transport, []):
                if key not in keys:
                    keys.append(key)
            if self.current_target and self.current_target not in keys:
                keys.append(self.current_target)
        # Sort the pane into three tiers, alphabetical within each:
        #   0) groups (@-prefixed) — always at the top,
        #   1) contacts you've had a dialog with (stored messages),
        #   2) everyone else (opened/saved but no traffic yet).
        def _tier(k: str) -> int:
            if k.startswith("@"):
                return 0
            return 1 if k in dialog_keys else 2

        keys.sort(key=lambda k: (_tier(k), k.lower()))
        # Per-mode favorites-only filter: keep favorite peers, the open
        # conversation (so the filter never blanks out what you're reading), and
        # ALL of this mode's groups — your @groups are always worth seeing, even
        # before you've exchanged any messages with them.
        if self._active_fav_only:
            keys = [
                k
                for k in keys
                if k == self.current_target
                or k in active_group_tags
                or self.core.favorites.is_favorite(k)
            ]
        view.clear()
        self._thread_keys = keys
        for key in keys:
            view.append(ListItem(Label(self._display_id(key))))

    def _remember_thread(self, key: str | None) -> None:
        """Record a conversation as 'opened' in the active mode (de-duplicated).

        Lets :meth:`_refresh_threads` keep it in the left pane after switching
        modes, even when it has no stored messages yet.
        """
        if not key or self.active_transport is None:
            return
        opened = self._opened_threads.setdefault(self.active_transport, [])
        if key not in opened:
            opened.append(key)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_id = event.list_view.id
        idx = event.list_view.index
        if idx is None:
            return
        if list_id == "threads":
            if idx >= len(self._thread_keys):
                return
            self.current_target = self._thread_keys[idx]
            self._load_thread(self.current_target)
            self._update_status()
        elif list_id == "monitor":
            self._open_from_monitor(idx)
        elif list_id == "nomad-nodes":
            if idx < len(self._nomad_nodes):
                node = self._nomad_nodes[idx]
                if node.get("dest"):
                    self._open_nomad(node["dest"])
        elif list_id == "favorites-list":
            if idx < len(self._fav_keys) and self._fav_keys[idx]:
                self._open_favorite(self._fav_keys[idx])
        elif list_id == "search-results":
            if idx < len(self._search_hits):
                thread_key, transport = self._search_hits[idx]
                if thread_key:
                    self._open_thread(thread_key, transport)
        elif list_id == "archive-list":
            if idx < len(self._archive_rows):
                thread_key, transport = self._archive_rows[idx]
                if thread_key:
                    self._open_thread(thread_key, transport)

    def _load_thread(self, thread_key: str) -> None:
        if self.core is None:
            return
        log = self.query_one("#messages", RichLog)
        log.clear()
        log.write(f"[bold]-- {self._display_id(thread_key)} --[/bold]")
        for msg in self.core.store.read_thread(thread_key):
            self._render_message(msg)

    def _show_all_messages(self) -> None:
        """Firehose view: every message for the active mode (no conversation).

        Shown when a chat mode has no specific callsign/@group/contact selected,
        so the operator can watch all of that mode's traffic at once (e.g. the
        JS8Call window with nothing focused). Each row keeps its sender and
        group tag, and inbound senders stay clickable to start a reply.
        """
        if self.core is None or not self.active_transport:
            return
        log = self.query_one("#messages", RichLog)
        log.clear()
        log.write(
            f"[bold]-- all {self.active_transport} messages —[/bold] "
            "[dim]no conversation selected; type /to <callsign|@GROUP> to "
            "focus one, or click a name to reply[/dim]"
        )
        msgs = self.core.store.read_transport(self.active_transport)
        if not msgs:
            log.write(
                "[dim italic]  (nothing heard yet on this mode)[/dim italic]"
            )
            return
        for msg in msgs:
            self._render_message(msg)

    def _refresh_active_pane(self) -> None:
        """Re-render the message pane for the active mode's current state.

        Loads the open conversation when one is selected, otherwise shows the
        all-messages firehose for the mode.
        """
        if self.current_target:
            self._load_thread(self.current_target)
        else:
            self._show_all_messages()

    # -- monitor --------------------------------------------------------------
    def _append_monitor(self, msg: UnifiedMessage) -> None:
        # Keep the full history so the favorites-only filter can rebuild.
        self._monitor_msgs.append(msg)
        # When Watch is paused, buffer (kept above) but don't render live.
        if self._watch_paused:
            return
        # Sorting by mode groups rows by transport, so a new arrival can't just
        # be appended at the end — rebuild to keep the grouping correct.
        if self._watch_sort_by_mode:
            self._rebuild_monitor()
            return
        if not self._monitor_passes(msg):
            return
        self._render_monitor_row(msg)

    # Per-mode colour for the Watch feed's transport tag, so it's obvious at a
    # glance which mode each line came from.
    _MODE_COLORS = {
        "reticulum": "cyan",
        "js8call": "yellow",
        "meshcore": "green",
        "nomadnet": "blue",
    }

    def _mode_color(self, transport: str) -> str:
        return self._MODE_COLORS.get(transport, "white")

    @staticmethod
    def _subject_md(msg: UnifiedMessage) -> str:
        """A bold ``Subject \u2014 `` prefix for Winlink mail.

        Winlink is email: the subject carries half the meaning, so prepend it to
        the body when rendering. Returns ``""`` for transports that have no
        subject. Brackets/backslashes are escaped so an arbitrary email subject
        can't break the surrounding Rich markup.
        """
        if msg.transport != "winlink":
            return ""
        subj = str(msg.metadata.get("subject") or "").strip()
        if not subj:
            return ""
        safe = subj.replace("\\", "\\\\").replace("[", "\\[")
        return f"[b]{safe}[/b] \u2014 "

    @staticmethod
    def _attachments_md(msg: UnifiedMessage) -> str:
        """A dim ``\U0001f4ce name1, name2`` suffix listing message attachments.

        Returns ``""`` when there are none. Names are escaped so they can't
        break the surrounding Rich markup.
        """
        names = msg.metadata.get("attachments") or []
        names = [str(n).strip() for n in names if str(n).strip()]
        if not names:
            return ""
        safe = ", ".join(
            n.replace("\\", "\\\\").replace("[", "\\[") for n in names
        )
        return f" [dim]\U0001f4ce {safe}[/dim]"

    def _render_monitor_row(self, msg: UnifiedMessage) -> None:
        mlist = self.query_one("#monitor", ListView)
        ts = msg.timestamp.strftime("%H:%M:%S")
        sec = "ENC" if msg.metadata.get("encrypted") else "---"
        tgt = f"@{msg.group}" if msg.group else (msg.recipient or "")
        star = "\u2605 " if self._msg_is_favorite(msg) else ""
        # Outbound messages (your own sends, any transport) are echoed into the
        # Watch feed too, so a watched conversation shows BOTH sides. They carry
        # a non-RECEIVED status, which is how we tell them apart from inbound.
        is_out = msg.status is not DeliveryStatus.RECEIVED
        who = "[cyan]you[/cyan]" if is_out else msg.sender
        # Colour-coded, fixed-width mode tag so the source mode is obvious and
        # the columns line up (helpful when sorted by mode).
        name = msg.transport or "?"
        color = self._mode_color(name)
        mode_tag = f"[{color}]{name:<9}[/{color}]"
        line = (
            f"{ts} {mode_tag} {sec} {star}{who} -> {tgt}: "
            f"{self._subject_md(msg)}{msg.content}{self._attachments_md(msg)}"
        )
        mlist.append(ListItem(Label(line)))
        self._monitor_entries.append((msg.thread_key, msg.transport))
        # Bound the rendered list so a long live session can't grow the widget
        # without limit. The master buffer (a capped deque) already holds at most
        # `cap` rows; once the rendered list drifts to ~2x that, rebuild from the
        # deque (resets widget + index map together, ≤ cap, staying aligned).
        # The 2x hysteresis amortises the rebuild to O(1) per message.
        cap = self._watch_buffer_limit
        if cap and len(self._monitor_entries) > 2 * cap:
            self._rebuild_monitor()
            return
        mlist.scroll_end(animate=False)

    def _monitor_passes(self, msg: UnifiedMessage) -> bool:
        """Whether a message survives the Watch stream's active filter.

        The favorites-only and group filters are mutually exclusive; at most one
        is active at a time (selecting one clears the other), so this is a simple
        either/or. With no filter active every message passes.
        """
        if self._monitor_group_filter is not None:
            return self._msg_in_group(msg, self._monitor_group_filter)
        if self._monitor_fav_only:
            return self._msg_is_favorite(msg)
        return True

    def _msg_in_group(self, msg: UnifiedMessage, name: str) -> bool:
        """True if a message belongs to the named group (member or tag).

        Uses the stamp the router already wrote (``msg.groups``) when present,
        and otherwise resolves live against the registry so your own outbound
        sends to the group show too (keeping both sides of a watched group).
        """
        if self.core is None:
            return False
        names = msg.groups
        if not names:
            try:
                names = self.core.groups.groups_for_message(msg)
            except Exception:  # noqa: BLE001 - filtering must never crash Watch
                return False
        return name in names

    def _msg_is_favorite(self, msg: UnifiedMessage) -> bool:
        if self.core is None:
            return False
        # You are always "favorite": your own outbound messages (echoed into
        # the Watch feed as "you") survive the favorites-only filter so a
        # watched conversation still shows both sides.
        if msg.status is not DeliveryStatus.RECEIVED:
            return True
        favs = self.core.favorites
        for candidate in (
            msg.sender,
            msg.recipient or "",
            msg.metadata.get("rns_dest", ""),
            msg.metadata.get("display_name", ""),
        ):
            if candidate and favs.is_favorite(candidate):
                return True
        return False

    def _rebuild_monitor(self) -> None:
        """Re-render the Monitor list honouring the favorites filter + sort mode."""
        mlist = self.query_one("#monitor", ListView)
        mlist.clear()
        self._monitor_entries.clear()
        msgs = self._monitor_msgs
        if self._watch_sort_by_mode:
            # Group by transport (mode), then chronologically within each mode.
            msgs = sorted(
                msgs, key=lambda m: (m.transport or "~", m.timestamp)
            )
        for msg in msgs:
            if not self._monitor_passes(msg):
                continue
            self._render_monitor_row(msg)

    def _toggle_watch_sort(self) -> None:
        """Toggle the Watch feed between time order and grouped-by-mode order."""
        self._watch_sort_by_mode = not self._watch_sort_by_mode
        self._rebuild_monitor()
        try:
            btn = self.query_one("#watch-sort", Button)
            btn.label = (
                "\u21c5 By time" if self._watch_sort_by_mode else "\u21c5 By mode"
            )
        except Exception:  # noqa: BLE001 - button may not be mounted in tests
            pass
        self._log_system(
            "Watch sorted by mode." if self._watch_sort_by_mode
            else "Watch sorted by time."
        )

    def _update_monitor_help(self) -> None:
        """Reflect the Monitor scope and active filter in the header.

        The transport scope ("all transports") and the active filter (favorites
        or a group) are shown as two independent segments, so the filter state
        never overwrites the scope label. Favorites and group are mutually
        exclusive — at most one shows at a time.
        """
        try:
            help_line = self.query_one("#monitor-help", Static)
        except Exception:  # noqa: BLE001 - widget may not be mounted yet
            return
        scope = "Monitor - [b]all transports[/b] (read-only)"
        if self._monitor_group_filter:
            grp = self._monitor_group_filter
            filt = f"filter: [b cyan]\u25c9 group @{grp}[/b cyan]"
        elif self._monitor_fav_only:
            filt = "filter: [b yellow]\u2605 favorites only[/b yellow]"
        else:
            filt = "filter: [dim]off (all senders)[/dim]"
        help_line.update(
            f"{scope}    {filt}    "
            "[F4] favorites \u00b7 [g] group"
        )

    def _refresh_monitor_ticker(self) -> None:
        """Update the 'Favorites' line at the top of the Monitor view."""
        try:
            ticker = self.query_one("#monitor-ticker", Static)
        except Exception:  # noqa: BLE001 - widget may not be mounted yet
            return
        if self.core is None or not self.core.favorites.all():
            ticker.update(
                "Favorites: (none added — '/fav add <id>' to track)"
            )
            return
        if not self._fav_recent:
            count = len(self.core.favorites.all())
            ticker.update(
                f"Favorites: {count} tracked — waiting for any to come online..."
            )
            return
        now = datetime.now(UTC)
        parts = []
        # Newest favorite first.
        for ts, peer, transport in reversed(self._fav_recent):
            ago = self._format_ago(now - ts)
            parts.append(f"[b]\u2605 {peer}[/b] [{transport}] {ago}")
        ticker.update("Favorites online: " + "  ·  ".join(parts))

    @staticmethod
    def _format_ago(delta: timedelta) -> str:
        secs = int(max(0, delta.total_seconds()))
        if secs < 60:
            return f"{secs}s"
        mins, secs = divmod(secs, 60)
        if mins < 60:
            return f"{mins}m{secs:02d}s"
        hrs, mins = divmod(mins, 60)
        return f"{hrs}h{mins:02d}m"

    def _set_friendly_name(self, arg: str) -> None:
        """Assign (or clear) a friendly name for the open conversation.

        The name is stored as the conversation id's favorite label, so it shows
        in the left pane and the conversation header instead of the raw hash.
        Usage: ``/name <friendly name>`` to set, ``/name -`` to clear, ``/name``
        to show the current name.
        """
        if self.core is None:
            return
        target = self.current_target
        if not target:
            self._log_system(
                "Open a conversation first, then '/name <friendly name>'."
            )
            return
        if target.startswith("@"):
            self._log_system("Groups already have a name.")
            return
        if not arg:
            current = self._friendly_name(target)
            self._log_system(
                f"name for {self._short(target)}: {current or '(none)'}  "
                "\u2014 set with '/name <friendly name>', clear with '/name -'."
            )
            return
        favs = self.core.favorites
        new_label = "" if arg.strip() == "-" else arg.strip()
        favs.set_label(target, new_label)
        try:
            favs.save(self.core.config)
        except Exception as exc:  # noqa: BLE001
            self._log_system(f"could not save name: {exc}")
            return
        # Reflect the new name in the left pane and the open conversation now.
        self._refresh_threads()
        if self.view == "active" and self.current_target == target:
            self._load_thread(target)
        self._update_status()
        if new_label:
            self._log_system(
                f"named {self._short(target)} \u2192 {new_label} "
                "(also saved as a contact)."
            )
        else:
            self._log_system(f"cleared the name for {self._short(target)}.")

    def _handle_fav_command(self, arg: str) -> None:
        """In-TUI favorites: /fav list | add <id> [label] | rm <id> | only."""
        if self.core is None:
            return
        parts = arg.split(maxsplit=2)
        action = parts[0].lower() if parts else "list"
        favs = self.core.favorites
        if action in ("", "list", "ls"):
            items = favs.all()
            if not items:
                self._log_system(
                    "favorites: (none) - '/fav add <callsign|hash> [label]'"
                )
                return
            for f in items:
                ago = (
                    self._format_ago(datetime.now(UTC) - f.last_seen)
                    if f.last_seen
                    else "never"
                )
                label = f" ({f.label})" if f.label else ""
                self._log_system(f"  \u2605 {f.id}{label}   last seen: {ago}")
            return
        if action in ("only", "filter"):
            self._toggle_fav_only()
            return
        if action in ("here", "this", "current"):
            # Favorite the open conversation (MeshCore channel/contact aware).
            label = arg.split(maxsplit=1)[1].strip() if " " in arg else ""
            self._favorite_current_conversation(label)
            return
        if action in ("groups", "import", "import-groups"):
            self._import_js8_groups()
            return
        if action in ("add",):
            if len(parts) < 2:
                self._log_system(
                    "usage: /fav add [node|peer|call|group|channel|contact] "
                    "<id> [label]"
                )
                return
            # Delegate to the shared parser so type keywords (node/peer/call/
            # group/channel/contact) and '@group' all work here too.
            rest = arg.split(maxsplit=1)[1] if " " in arg else ""
            self._add_favorite_from_input(rest)
            return
        if action in ("rm", "remove", "del"):
            if len(parts) < 2:
                self._log_system("usage: /fav rm <callsign|rns-hash>")
                return
            ident = parts[1]
            ok = favs.remove(ident)
            try:
                favs.save(self.core.config)
            except Exception as exc:  # noqa: BLE001
                self._log_system(f"could not save favorites: {exc}")
            # Drop any cached recents whose label matches.
            kept = [
                (ts, peer, t)
                for ts, peer, t in self._fav_recent
                if peer.lower() != ident.lower()
            ]
            self._fav_recent = deque(kept, maxlen=self._fav_recent.maxlen)
            self._refresh_monitor_ticker()
            self._rebuild_monitor()
            if self.view == "favorites":
                self._render_favorites()
            self._log_system("removed" if ok else "no matching favorite")
            return
        self._log_system(
            "usage: /fav list | add <id> [label] | here [label] | "
            "rm <id> | only | groups"
        )

    def _handle_tmpl_command(self, arg: str) -> None:
        """List, load, add, or delete message templates.

        /tmpl list           — list all template names
        /tmpl <name>         — load template into composer
        /tmpl add <n> <text> — create a new template
        /tmpl del <name>     — delete a template
        """
        if self.core is None:
            return
        from ..core.templates import Templates
        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""

        if sub in ("add", "new"):
            rest = parts[1].strip() if len(parts) > 1 else ""
            subparts = rest.split(None, 1)
            if len(subparts) < 2:
                self._log_system(
                    'usage: /tmpl add <name> <text>  e.g. /tmpl add ack "Message received"'
                )
                return
            name, text = subparts[0], subparts[1]
            self.core.config.set("templates", name, text)
            self.core.config.save()
            self._log_system(f"Template '{name}' saved.")
            return

        if sub in ("del", "delete", "rm", "remove"):
            name = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                self._log_system("usage: /tmpl del <name>")
                return
            tmpls_data = self.core.config.data.get("templates", {})
            if name not in tmpls_data:
                self._log_system(
                    f"Template '{name}' not found. "
                    f"Available: {', '.join(sorted(tmpls_data)) or '(none)'}"
                )
                return
            del tmpls_data[name]
            self.core.config.save()
            self._log_system(f"Template '{name}' deleted.")
            return

        tmpls = Templates.from_config(self.core.config)
        if not arg or sub == "list":
            names = tmpls.names()
            if not names:
                self._log_system(
                    "No templates. Use /tmpl add <name> <text> to create one."
                )
            else:
                joined = "  ".join(f"[b]{n}[/b]" for n in names)
                self._log_system(
                    "Templates: " + joined
                    + "  [dim](/tmpl add <n> <text> to add, /tmpl del <n> to remove)[/dim]"
                )
            return
        text = tmpls.get(arg.strip())
        if text is None:
            names = tmpls.names()
            hint = ", ".join(names) if names else "(none configured)"
            self._log_system(f"Template '{arg}' not found. Available: {hint}")
            return
        try:
            from textual.widgets import Input
            composer = self.query_one("#composer", Input)
            composer.value = text
            composer.focus()
        except Exception:  # noqa: BLE001 - not fatal if composer unavailable
            self._log_system(f"Template text: {text}")

    def _handle_bands_command(self, arg: str) -> None:
        """Show band-plan reference or recent HF band activity log.

        /bands             — band-plan + solar conditions
        /bands <band>      — filter plan to one band (e.g. /bands 40m)
        /bands activity    — show recent messages with band metadata
        /bands activity 40m — filter activity log to one band
        """
        from ..core.bandplan import format_mhz, lookup
        parts = arg.strip().lower().split(None, 1) if arg.strip() else []
        first = parts[0] if parts else ""

        if first in ("activity", "log", "rx"):
            if self.core is None:
                return
            band_filter = parts[1].strip() if len(parts) > 1 else None
            since = datetime.now(UTC) - timedelta(hours=24)
            msgs = self.core.store.query(
                band=band_filter, since=since, limit=100, newest_first=False
            )
            # Keep only messages that have a band in metadata (HF traffic).
            msgs = [m for m in msgs if m.metadata.get("band")]
            if not msgs:
                qualifier = f" on {band_filter}" if band_filter else ""
                self._log_system(
                    f"No HF band activity{qualifier} in the last 24h."
                )
                return
            title = f"[b]Band activity{f' — {band_filter}' if band_filter else ''} (last 24h):[/b]"
            lines = [title]
            for m in msgs:
                ts = m.timestamp.strftime("%H:%M")
                band_tag = m.metadata.get("band", "?")
                snr = m.metadata.get("snr")
                snr_str = f" SNR{snr:+.0f}" if snr is not None else ""
                direction = "→" if m.status.value != "received" else "←"
                preview = m.content[:50]
                lines.append(
                    f"  {ts}  [b]{band_tag:<5}[/b]  {m.sender:<10}"
                    f"  {direction}  {preview!r}{snr_str}"
                )
            self._log_system("\n".join(lines))
            return

        band = arg.strip().lower() or None
        entries = lookup(band=band, region="US")
        if not entries:
            self._log_system(
                f"No band-plan entries{f' for {band}' if band else ''}."
            )
            return

        solar = self._solar_data
        lines = []
        if solar:
            age_s = int(
                (
                    __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    )
                    - solar.fetched_at
                ).total_seconds()
            )
            age = f"{age_s // 60} min ago" if age_s >= 60 else "just now"
            geo = f"  Geo:[b]{solar.geo_field}[/b]" if solar.geo_field else ""
            lines.append(
                f"[dim]Solar  SFI=[b]{solar.sfi}[/b]  SSN=[b]{solar.ssn}[/b]"
                f"  A=[b]{solar.a_index}[/b]  K=[b]{solar.k_index}[/b]{geo}"
                f"  ({age})[/dim]"
            )
            lines.append("")

        _COND_COLOR = {"Good": "green", "Fair": "yellow", "Poor": "red"}

        lines.append(f"[b]Band plan{f' — {band}' if band else ''}:[/b]")
        current_band = None
        for e in entries:
            if e.band != current_band:
                current_band = e.band
                if solar:
                    cond = solar.condition_for(e.band)
                    parts = []
                    for period in ("day", "night"):
                        val = cond.get(period, "")
                        if val:
                            col = _COND_COLOR.get(val, "")
                            label = f"[{col}]{val}[/{col}]" if col else val
                            parts.append(f"{period.capitalize()}: {label}")
                    suffix = f"  [dim]({' / '.join(parts)})[/dim]" if parts else ""
                else:
                    suffix = ""
                lines.append(f"  [b]{e.band}[/b]{suffix}")
            freq = format_mhz(e.freq_khz)
            tp = f" [{e.transport}]" if e.transport else ""
            lines.append(f"    {freq:<14} {e.mode:<6}{tp}  {e.notes}")
        self._log_system("\n".join(lines))

    def _handle_sched_command(self, arg: str) -> None:
        """Schedule, list, or cancel deferred sends and band changes.

        Usage: /sched +30m  |  /sched 19:00  |  /sched +1h optional message text
               /sched list  — show pending queue
               /sched cancel <id>  — cancel a pending scheduled message
               /sched band <band> <time> [daily]  — schedule a JS8Call band change
        """
        from ..core.message import UnifiedMessage

        parts = arg.strip().split(None, 1)
        if not parts:
            self._log_system(
                "Usage: /sched +30m | HH:MM [text] | list | cancel <id> "
                "| band <band> <time> [daily]"
            )
            return

        if parts[0].lower() == "list":
            if self.core is None:
                return
            pending = self.core.store.schedule_pending()
            if not pending:
                self._log_system("No scheduled messages pending.")
                return
            lines = [f"[b]Scheduled messages[/b] ({len(pending)} pending):"]
            for e in pending:
                ts = e.fire_at.strftime("%H:%M UTC")
                short_id = e.id[:8]
                kind = e.message.metadata.get("kind")
                if kind == "band_change":
                    band = e.message.metadata.get("band", "?")
                    daily = " [dim](daily)[/dim]" if e.message.metadata.get("recur_daily") else ""
                    lines.append(
                        f"  [b]{ts}[/b]  → [b]{band}[/b] [dim](band change){daily}[/dim]"
                        f"  [dim](id:{short_id})[/dim]"
                    )
                else:
                    target = e.message.recipient or (
                        f"@{e.message.group}" if e.message.group else "?"
                    )
                    preview = e.message.content[:40]
                    lines.append(
                        f"  [b]{ts}[/b]  → {target}  [dim]{preview!r}[/dim]"
                        f"  [dim](id:{short_id})[/dim]"
                    )
            lines.append("[dim]/sched cancel <id> to cancel[/dim]")
            self._log_system("\n".join(lines))
            return

        if parts[0].lower() == "band":
            self._handle_sched_band_command(parts[1].strip() if len(parts) > 1 else "")
            return

        if parts[0].lower() == "cancel":
            if self.core is None:
                return
            target_id = parts[1].strip() if len(parts) > 1 else ""
            if not target_id:
                self._log_system("usage: /sched cancel <id>  (from /sched list)")
                return
            # Allow partial ID match (first 8 chars)
            pending = self.core.store.schedule_pending()
            matches = [e for e in pending if e.id.startswith(target_id)]
            if not matches:
                self._log_system(f"No pending message with id starting '{target_id}'.")
                return
            if len(matches) > 1:
                self._log_system(
                    f"Ambiguous id '{target_id}' matches {len(matches)} messages; "
                    "use more characters."
                )
                return
            ok = self.core.store.schedule_cancel(matches[0].id)
            if ok:
                self._log_system(f"Cancelled: {matches[0].id[:8]}")
            else:
                self._log_system(f"Could not cancel {matches[0].id[:8]} (already sent?).")
            return

        time_spec = parts[0]
        text_override = parts[1] if len(parts) > 1 else None

        now = datetime.now(UTC)
        try:
            if time_spec.startswith("+"):
                raw = time_spec[1:].lower()
                if "h" in raw and "m" in raw:
                    h_part, rest = raw.split("h")
                    mins = int(h_part) * 60 + int(rest.rstrip("m"))
                elif "h" in raw:
                    mins = int(raw.rstrip("h")) * 60
                else:
                    mins = int(raw.rstrip("m"))
                fire_at = now + timedelta(minutes=mins)
            else:
                hh, mm = time_spec.split(":")
                fire_at = now.replace(
                    hour=int(hh), minute=int(mm), second=0, microsecond=0
                )
                if fire_at <= now:
                    fire_at += timedelta(days=1)
        except (ValueError, AttributeError):
            self._log_system("Invalid time. Use: /sched +30m  or  /sched 19:00")
            return

        if text_override:
            content = text_override
        else:
            try:
                from textual.widgets import Input as _Input
                composer = self.query_one("#composer", _Input)
                content = composer.value.strip()
            except Exception:  # noqa: BLE001
                content = ""
        if not content:
            self._log_system(
                "/sched: no message text. Type a message or use /sched 19:00 text"
            )
            return

        if self.core is None:
            return

        thread = (
            getattr(self, "_active_thread", None)
            or getattr(self, "_watch_thread", None)
        )
        name = (self.core.station.callsign if self.core.station else None) or "unknown"
        if thread and thread.startswith("@"):
            msg = UnifiedMessage.to_group(name, thread[1:], content)
        elif thread:
            msg = UnifiedMessage.direct(name, thread, content)
        else:
            self._log_system(
                "/sched: no active thread — navigate to a conversation first"
            )
            return

        self.core.store.schedule_add(msg, fire_at)
        ts = fire_at.strftime("%H:%M UTC")
        self._log_system(f"Message scheduled for {ts}: {content[:40]!r}")

    def _handle_sched_band_command(self, arg: str) -> None:
        """Parse and schedule a JS8Call band change.

        Syntax: <band> <time> [daily]
        Examples:
          /sched band 40m 20:00
          /sched band 20m +2h daily
        """
        if self.core is None:
            return
        from ..core.message import AddressType, UnifiedMessage
        from ..transports.js8call_transport import dial_for_band

        parts = arg.split()
        if len(parts) < 2:
            self._log_system(
                "usage: /sched band <band> <time> [daily]\n"
                "  e.g. /sched band 40m 20:00\n"
                "       /sched band 20m +2h daily"
            )
            return
        band = parts[0].lower()
        time_spec = parts[1]
        recur_daily = len(parts) >= 3 and parts[2].lower() == "daily"

        # Validate band name.
        if dial_for_band(band) is None:
            from ..core.bandplan import lookup
            known = sorted({e.band for e in lookup(region="US")})
            self._log_system(
                f"Unknown band '{band}'. Valid bands: {', '.join(known)}"
            )
            return

        now = datetime.now(UTC)
        try:
            if time_spec.startswith("+"):
                raw = time_spec[1:].lower()
                if "h" in raw and "m" in raw:
                    h_part, rest = raw.split("h")
                    mins = int(h_part) * 60 + int(rest.rstrip("m"))
                elif "h" in raw:
                    mins = int(raw.rstrip("h")) * 60
                else:
                    mins = int(raw.rstrip("m"))
                fire_at = now + timedelta(minutes=mins)
            else:
                hh, mm = time_spec.split(":")
                fire_at = now.replace(
                    hour=int(hh), minute=int(mm), second=0, microsecond=0
                )
                if fire_at <= now:
                    fire_at += timedelta(days=1)
        except (ValueError, AttributeError):
            self._log_system("Invalid time. Use: /sched band 40m 20:00  or  +2h")
            return

        name = self.core.station.callsign or "scheduler"
        msg = UnifiedMessage(
            sender=name,
            content=f"Band change: {band}",
            address_type=AddressType.BROADCAST,
            metadata={"kind": "band_change", "band": band, "recur_daily": recur_daily},
            transport="js8call",
        )
        self.core.store.schedule_add(msg, fire_at, transport="js8call")
        ts = fire_at.strftime("%H:%M UTC")
        repeat = " (daily)" if recur_daily else ""
        self._log_system(f"Band change to {band} scheduled for {ts}{repeat}.")

    async def _fire_scheduled_band_change(self, entry) -> bool:
        """Execute a due band-change scheduled entry.

        Skips (logs + returns False) when JS8Call is not running or when another
        transport currently holds the radio interlock.
        """
        band = entry.message.metadata.get("band", "?")
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system(
                f"⏰ Band change to {band} skipped — JS8Call not running."
            )
            return False
        blocker = self.core.radio_interlock.blocked_by("js8call")
        if blocker is not None:
            self._log_system(
                f"⏰ Band change to {band} skipped — radio busy ({blocker})."
            )
            return False
        from ..transports.js8call_transport import dial_for_band
        hz = dial_for_band(band)
        if hz is None:
            self._log_system(f"⏰ Band change: unknown band '{band}'.")
            return False
        ok = await t.set_dial_freq(hz)
        if ok:
            self._log_system(f"⏰ Band changed to [b]{band}[/b] as scheduled.")
            self._update_js8_bar()
        else:
            self._log_system(f"⏰ Band change to {band} failed — JS8Call API error.")
        return ok

    def _reschedule_daily_band_change(self, entry) -> None:
        """Re-queue a daily band-change entry for the next day."""
        from ..core.message import AddressType, UnifiedMessage
        new_fire = entry.fire_at + timedelta(days=1)
        msg = UnifiedMessage(
            sender=entry.message.sender,
            content=entry.message.content,
            address_type=AddressType.BROADCAST,
            metadata=dict(entry.message.metadata),
            transport="js8call",
        )
        self.core.store.schedule_add(msg, new_fire, transport="js8call")

    def _handle_bandscan_command(self, arg: str) -> None:
        """Run an on-demand band scan: heartbeat + listen per band, then report.

        /bandscan <band1,band2,...> <dwell_minutes>
        """
        parts = arg.strip().split()
        if len(parts) != 2:
            self._log_system(
                "usage: /bandscan <band1,band2,...> <dwell_minutes>  "
                "e.g. /bandscan 80m,40m,20m 5"
            )
            return
        bands = [b.strip() for b in parts[0].split(",") if b.strip()]
        if not bands:
            self._log_system("usage: /bandscan <band1,band2,...> <dwell_minutes>")
            return
        try:
            dwell_min = float(parts[1])
            if dwell_min <= 0:
                raise ValueError
        except ValueError:
            self._log_system(f"invalid dwell minutes: '{parts[1]}'")
            return
        self._run_bandscan(bands, dwell_min * 60)

    @work(exclusive=True)
    async def _run_bandscan(self, bands: list[str], dwell_s: float) -> None:
        """Cycle ``bands``, heartbeat + listen on each, then offer to switch.

        Running /bandscan again cancels an in-progress scan (Textual's
        exclusive-worker semantics) — there's no separate stop command.
        """
        if self.core is None:
            return
        t = self._js8_transport()
        if t is None or not getattr(t, "running", False):
            self._log_system("Band scan: JS8Call is not running.")
            return
        blocker = self.core.radio_interlock.blocked_by("js8call")
        if blocker is not None:
            self._log_system(f"⛔ Band scan blocked — radio busy ({blocker}).")
            return
        from ..transports.js8call_transport import dial_for_band
        unknown = [b for b in bands if dial_for_band(b) is None]
        if unknown:
            self._log_system(f"Band scan: unknown band(s): {', '.join(unknown)}")
            return

        grid = self.core.config.station.get("grid_square", "")
        self._log_system(
            f"📡 Band scan starting: {', '.join(bands)} "
            f"({int(dwell_s)}s each) — sending JS8 heartbeats…"
        )
        from ..core.band_scan import run_band_scan
        report = await run_band_scan(
            t, self.core.router, self.core.radio_interlock,
            bands, dwell_s, grid=grid, on_progress=self._log_system,
        )
        if report is None:
            return  # already logged (radio busy)
        self._update_js8_bar()

        lines = ["[b]Band scan results:[/b]"]
        for r in report.results:
            snr = f", avg SNR {r.avg_snr:+.0f} dB" if r.avg_snr is not None else ""
            lines.append(f"  {r.band}: {r.heard_count} heard{snr}")
        self._log_system("\n".join(lines))

        best = report.best()
        if best is None:
            self._log_system("Band scan: no replies heard on any band.")
            if report.original_band:
                hz = dial_for_band(report.original_band)
                if hz:
                    await t.set_dial_freq(hz)
                    self._update_js8_bar()
            return

        result = await self.push_screen_wait(
            BandScanResultScreen(report.results, best.band)
        )
        if result:
            hz = dial_for_band(best.band)
            if hz:
                await t.set_dial_freq(hz)
                self._update_js8_bar()
                self._log_system(f"✓ Switched to {best.band}.")
        elif report.original_band:
            hz = dial_for_band(report.original_band)
            if hz:
                await t.set_dial_freq(hz)
                self._update_js8_bar()

    def _handle_roster_command(self, arg: str) -> None:
        """Show the presence roster (recently-heard callsigns)."""
        from ..core.roster import get_roster

        if self.core is None:
            return

        raw = arg.strip().lower().rstrip("h") if arg.strip() else "24"
        try:
            hours = int(raw)
        except ValueError:
            hours = 24
        since = datetime.now(UTC) - timedelta(hours=hours)
        entries = get_roster(self.core.store, since=since, limit=30)
        if not entries:
            self._log_system(f"Roster: no stations heard in the last {hours}h.")
            return
        lines = [f"[b]Roster — last {hours}h:[/b]  {len(entries)} station(s)"]
        for e in entries:
            ts = e.last_seen.strftime("%m-%d %H:%M")
            snr = f" SNR {e.last_snr:+.0f}" if e.last_snr is not None else ""
            lines.append(
                f"  [b]{e.callsign}[/b]  {e.transport}  {ts}{snr}  ×{e.message_count}"
            )
        self._log_system("\n".join(lines))

    def _handle_subs_command(self, arg: str) -> None:
        """Manage group subscriptions from the TUI.

        /subs           — list current subscriptions and all configured groups
        /subs add @EMS  — subscribe to a group
        /subs rm @EMS   — unsubscribe from a group
        """
        if self.core is None:
            return
        subs = list(self.core.config.subscriptions.get("groups", []))
        all_groups = list(self.core.config.groups.keys())

        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""

        if sub in ("add", "sub"):
            name = parts[1].strip().lstrip("@") if len(parts) > 1 else ""
            if not name:
                self._log_system("usage: /subs add <@GROUP>")
                return
            if name not in subs:
                subs.append(name)
                self.core.config.set("subscriptions", "groups", subs)
                self.core.config.save()
            self._log_system(f"Subscribed to @{name}.")
            return

        if sub in ("rm", "remove", "del", "unsub"):
            name = parts[1].strip().lstrip("@") if len(parts) > 1 else ""
            if not name:
                self._log_system("usage: /subs rm <@GROUP>")
                return
            if name in subs:
                subs.remove(name)
                self.core.config.set("subscriptions", "groups", subs)
                self.core.config.save()
                self._log_system(f"Unsubscribed from @{name}.")
            else:
                self._log_system(f"@{name} is not in your subscriptions.")
            return

        # Default: show list
        lines = ["[b]Group subscriptions[/b]"]
        if all_groups:
            for g in sorted(all_groups):
                marker = "[green]✓[/green]" if g in subs else "[dim]○[/dim]"
                lines.append(f"  {marker}  @{g}")
        else:
            lines.append("  [dim]No groups configured.[/dim]")
        lines.append(
            "[dim]/subs add @GROUP or /subs rm @GROUP to change subscriptions[/dim]"
        )
        self._log_system("\n".join(lines))

    def _contacts_auto_link_favorite(self, address: str) -> None:
        """When favoriting an address that belongs to a contact, auto-favorite all
        other identities linked to that contact."""
        if self.core is None:
            return
        book = getattr(self.core, "contact_book", None)
        if book is None:
            return
        contact = book.by_address("", address)
        if contact is None:
            return
        all_ids = book.identities_for(contact.contact_id)
        newly_added = []
        for ident in all_ids:
            if ident.address == address:
                continue
            if not self.core.favorites.is_favorite(ident.address):
                self.core.favorites.add(ident.address, label=contact.display_name)
                newly_added.append(f"{ident.transport}:{ident.address}")
        if newly_added:
            try:
                self.core.favorites.save(self.core.config)
            except Exception:  # noqa: BLE001
                pass
            self._log_system(
                f"Also favorited {len(newly_added)} linked "
                f"{'identity' if len(newly_added)==1 else 'identities'} "
                f"for [b]{contact.display_name}[/b]: "
                + ", ".join(newly_added)
            )

    def _handle_bridge_command(self, arg: str) -> None:
        """Show active bridge rules.

        /bridge        — list configured rules
        /bridge list   — same
        """
        if self.core is None:
            return
        rules = self.core.bridge.rules if hasattr(self.core, "bridge") else []
        if not rules:
            self._log_system(
                "No bridge rules configured. Add [[bridge]] entries to config.toml.\n"
                "Example:\n"
                "  [[bridge]]\n"
                "  from = \"js8call\"\n"
                "  to   = \"reticulum\"\n"
                "  filter = \"*\"   # *, broadcast, group, direct"
            )
            return
        lines = [f"[b]Bridge rules[/b] ({len(rules)} active):"]
        for r in rules:
            addr = f" [{r.address_filter}]" if r.address_filter != "*" else ""
            lines.append(f"  [b]{r.from_transport}[/b] → [b]{r.to_transport}[/b]{addr}")
        lines.append(
            "[dim]Bridged messages carry metadata.bridged=True and "
            "metadata.bridge_origin=<source transport>[/dim]"
        )
        self._log_system("\n".join(lines))

    def _handle_filters_command(self, arg: str) -> None:
        """List, add, edit, delete, or reorder inbound filter rules.

        /filters                                — list rules (evaluation order)
        /filters add <name> <action> [k=v ...]  — add a rule
        /filters edit <ref> <action> [k=v ...]  — replace a rule's action/match
        /filters del <ref>                      — delete a rule (ref = name or #)
        /filters mv <ref> up|down                — reorder
        """
        if self.core is None:
            return
        from ..core.filters import build_filter_rule

        engine = self.core.filters
        parts = arg.strip().split()
        sub = parts[0].lower() if parts else ""

        if not arg or sub == "list":
            self._filters_list(engine)
            return

        if sub == "add":
            if len(parts) < 3:
                self._log_system(
                    "usage: /filters add <name> <action> [key=value ...]  "
                    "e.g. /filters add ems-hf notify group=EMS transport=js8call"
                )
                return
            try:
                rule = build_filter_rule(parts[1], parts[2], parts[3:])
                engine.add_rule(rule)
            except ValueError as exc:
                self._log_system(f"Error: {exc}")
                return
            engine.save(self.core.config)
            self._log_system(f"Filter '{rule.name}' added ({rule.action.value}).")
            return

        if sub == "edit":
            if len(parts) < 3:
                self._log_system(
                    "usage: /filters edit <name|#> <action> [key=value ...]"
                )
                return
            try:
                ok = engine.edit_rule(parts[1], parts[2], parts[3:])
            except ValueError as exc:
                self._log_system(f"Error: {exc}")
                return
            if not ok:
                self._log_system(f"No such filter rule '{parts[1]}'.")
                return
            engine.save(self.core.config)
            self._log_system(f"Filter '{parts[1]}' updated.")
            return

        if sub in ("del", "delete", "rm", "remove"):
            if len(parts) < 2:
                self._log_system("usage: /filters del <name|#>")
                return
            removed = engine.remove_rule(parts[1])
            if removed is None:
                self._log_system(f"No such filter rule '{parts[1]}'.")
                return
            engine.save(self.core.config)
            self._log_system(f"Filter '{removed.name}' deleted.")
            return

        if sub in ("mv", "move"):
            if len(parts) < 3 or parts[2].lower() not in ("up", "down"):
                self._log_system("usage: /filters mv <name|#> up|down")
                return
            if not engine.move_rule(parts[1], parts[2].lower()):
                self._log_system(f"Could not move '{parts[1]}' {parts[2].lower()}.")
                return
            engine.save(self.core.config)
            self._filters_list(engine)
            return

        self._log_system(
            f"unknown /filters action '{sub}'  ·  try: add | edit | del | mv"
        )

    def _filters_list(self, engine: object) -> None:
        from ..core.filters import FilterEngine
        assert isinstance(engine, FilterEngine)
        rules = engine.rules
        if not rules:
            self._log_system(
                "No filter rules configured. Everything defaults to 'show'.\n"
                "Use /filters add <name> <action> [key=value ...] to create one."
            )
            return
        lines = [f"[b]Filter rules[/b] ({len(rules)}, evaluated top to bottom):"]
        for i, r in enumerate(rules, start=1):
            match = ", ".join(f"{k}={v}" for k, v in r.match.items()) or "(all)"
            name = r.name or "(unnamed)"
            lines.append(
                f"  {i}. [b]{name}[/b]  {r.action.value}  [dim]{match}[/dim]"
            )
        lines.append(
            "[dim]/filters add|edit|del|mv — manage rules; first match wins[/dim]"
        )
        self._log_system("\n".join(lines))

    def _handle_contacts_command(self, arg: str) -> None:
        """Manage the cross-mode contacts book.

        /contacts                                    — list all contacts
        /contacts <name>                             — show contact details
        /contacts new <name> [notes...]              — create contact
        /contacts delete <name>                      — delete contact
        /contacts link <name> <transport> <addr> [label] — link identity
        /contacts unlink <transport> <addr>          — unlink identity
        /contacts rename <name> <new_name>           — rename contact
        """
        if self.core is None:
            return
        book = getattr(self.core, "contact_book", None)
        if book is None:
            self._log_system("Contacts not available.")
            return

        parts = arg.strip().split(None, 3)
        action_kw = {"new", "delete", "link", "unlink", "rename"}
        first = parts[0].lower() if parts else ""

        # /contacts  (no arg) — list all
        if not parts:
            contacts = book.all()
            if not contacts:
                self._log_system(
                    "Contacts: (none) — '/contacts new <name>' to create one."
                )
                return
            lines = [f"[b]Contacts[/b] ({len(contacts)}):"]
            for c in contacts:
                ids = book.identities_for(c.contact_id)
                n = len(ids)
                tag = f"  [dim]({n} identity)[/dim]" if n == 1 else f"  [dim]({n} identities)[/dim]"
                lines.append(f"  {c.display_name}{tag}")
            self._log_system("\n".join(lines))
            return

        # /contacts new <name> [notes...]
        if first == "new":
            rest = " ".join(parts[1:]).strip()
            name_parts = rest.split("//", 1)
            name = name_parts[0].strip()
            notes = name_parts[1].strip() if len(name_parts) > 1 else ""
            if not name:
                self._log_system("usage: /contacts new <name> [// notes]")
                return
            try:
                c = book.add(name, notes=notes)
            except ValueError as exc:
                self._log_system(f"error: {exc}")
                return
            self._log_system(f"Contact created: [b]{c.display_name}[/b]")
            return

        # /contacts delete <name>
        if first == "delete":
            name = " ".join(parts[1:]).strip()
            if not name:
                self._log_system("usage: /contacts delete <name>")
                return
            matches = book.by_name(name)
            if not matches:
                self._log_system(f"No contact matching '{name}'.")
                return
            c = matches[0]
            ids = book.identities_for(c.contact_id)
            book.delete(c.contact_id)
            self._log_system(
                f"Deleted [b]{c.display_name}[/b] "
                f"and {len(ids)} linked {'identity' if len(ids)==1 else 'identities'}."
            )
            return

        # /contacts link <name> <transport> <addr> [label]
        if first == "link":
            sub_parts = " ".join(parts[1:]).strip().split(None, 3)
            if len(sub_parts) < 3:
                self._log_system(
                    "usage: /contacts link <name> <transport> <addr> [label]"
                )
                return
            name, transport, address = sub_parts[0], sub_parts[1].lower(), sub_parts[2]
            label = sub_parts[3] if len(sub_parts) > 3 else ""
            matches = book.by_name(name)
            if not matches:
                self._log_system(f"No contact matching '{name}'.")
                return
            c = matches[0]
            try:
                book.link(c.contact_id, transport, address, label=label)
            except ValueError as exc:
                self._log_system(f"error: {exc}")
                return
            self._log_system(
                f"Linked [b]{transport}:{address}[/b] to [b]{c.display_name}[/b]."
            )
            return

        # /contacts unlink <transport> <addr>
        if first == "unlink":
            sub_parts = " ".join(parts[1:]).strip().split(None, 1)
            if len(sub_parts) < 2:
                self._log_system("usage: /contacts unlink <transport> <addr>")
                return
            transport, address = sub_parts[0].lower(), sub_parts[1]
            if book.unlink(transport, address):
                self._log_system(f"Unlinked {transport}:{address}.")
            else:
                self._log_system(f"{transport}:{address} was not linked to any contact.")
            return

        # /contacts rename <name> <new_name>
        if first == "rename":
            sub_parts = " ".join(parts[1:]).strip().split("//", 1)
            if len(sub_parts) < 2:
                self._log_system("usage: /contacts rename <old name> // <new name>")
                return
            old_name = sub_parts[0].strip()
            new_name = sub_parts[1].strip()
            if not old_name or not new_name:
                self._log_system("usage: /contacts rename <old name> // <new name>")
                return
            matches = book.by_name(old_name)
            if not matches:
                self._log_system(f"No contact matching '{old_name}'.")
                return
            c = matches[0]
            try:
                book.rename(c.contact_id, new_name)
            except ValueError as exc:
                self._log_system(f"error: {exc}")
                return
            self._log_system(f"Renamed [b]{c.display_name}[/b] → [b]{new_name}[/b].")
            return

        # /contacts <name>  — show contact details
        name = arg.strip()
        matches = book.by_name(name)
        if not matches:
            self._log_system(f"No contact matching '{name}'.")
            return
        for c in matches:
            ids = book.identities_for(c.contact_id)
            lines = [f"[b]{c.display_name}[/b]"]
            if c.notes:
                lines.append(f"  notes: {c.notes}")
            if ids:
                for ident in ids:
                    lbl = f"  ({ident.label})" if ident.label else ""
                    lines.append(f"  {ident.transport:<12} {ident.address}{lbl}")
            else:
                lines.append("  (no linked identities)")
            self._log_system("\n".join(lines))

    def _handle_groups_command(self, arg: str) -> None:
        """Manage group routing configuration from the TUI.

        /groups                              — list all groups
        /groups @EMS                         — show group details
        /groups new @EMS [transport ...]     — create group
        /groups delete @EMS                  — delete group
        /groups add @EMS transport:id        — add incoming member
        /groups rm @EMS transport:id         — remove incoming member
        /groups tag @EMS @tag                — add incoming tag
        /groups untag @EMS @tag              — remove incoming tag
        """
        from ..core.groups import GroupRegistry

        if self.core is None:
            return

        reg = GroupRegistry.from_config(self.core.config)
        parts = arg.strip().split(None, 2)

        if not parts:
            self._groups_list(reg)
            return

        first = parts[0].lower()
        _ACTIONS = {"new", "delete", "add", "rm", "remove", "tag", "untag"}

        if first.startswith("@") or first not in _ACTIONS:
            self._groups_show(reg, parts[0])
            return

        action = first

        if action == "new":
            if len(parts) < 2:
                self._log_system("usage: /groups new @NAME [transport ...]")
                return
            name = parts[1].lstrip("@")
            transports = parts[2].split() if len(parts) > 2 else []
            g = reg.ensure_group(name)
            if transports:
                g.transports = transports
                reg._dirty = True  # noqa: SLF001
            reg.save(self.core.config)
            note = f" on {', '.join(transports)}" if transports else ""
            self._log_system(f"Created group @{name}{note}.")
            return

        if action == "delete":
            if len(parts) < 2:
                self._log_system("usage: /groups delete @EMS")
                return
            name = parts[1].lstrip("@")
            if reg.remove_group(name):
                reg.save(self.core.config)
                self._log_system(f"Deleted group @{name}.")
            else:
                self._log_system(f"No such group @{name}.")
            return

        if action == "add":
            if len(parts) < 3:
                self._log_system("usage: /groups add @EMS transport:identifier")
                return
            name = parts[1].lstrip("@")
            try:
                m = reg.add_member(name, parts[2])
                reg.save(self.core.config)
                self._log_system(f"@{name}: added member {m.spec}.")
            except ValueError as exc:
                self._log_system(f"Error: {exc}")
            return

        if action in ("rm", "remove"):
            if len(parts) < 3:
                self._log_system("usage: /groups rm @EMS transport:identifier")
                return
            name = parts[1].lstrip("@")
            if reg.remove_member(name, parts[2]):
                reg.save(self.core.config)
                self._log_system(f"@{name}: removed member {parts[2]}.")
            else:
                self._log_system(f"@{name}: '{parts[2]}' is not a member.")
            return

        if action == "tag":
            if len(parts) < 3:
                self._log_system("usage: /groups tag @EMS @tag")
                return
            name = parts[1].lstrip("@")
            try:
                t = reg.add_tag(name, parts[2])
                reg.save(self.core.config)
                self._log_system(f"@{name}: now claims tag @{t}.")
            except ValueError as exc:
                self._log_system(f"Error: {exc}")
            return

        if action == "untag":
            if len(parts) < 3:
                self._log_system("usage: /groups untag @EMS @tag")
                return
            name = parts[1].lstrip("@")
            if reg.remove_tag(name, parts[2]):
                reg.save(self.core.config)
                self._log_system(f"@{name}: removed tag @{parts[2].lstrip('@')}.")
            else:
                self._log_system(f"@{name}: no such tag '{parts[2]}'.")
            return

        self._log_system(
            f"unknown /groups action '{action}'  ·  "
            "try: new | delete | add | rm | tag | untag"
        )

    def _groups_list(self, reg: object) -> None:
        from ..core.groups import GroupRegistry
        assert isinstance(reg, GroupRegistry)
        groups = reg.all()
        if not groups:
            self._log_system(
                "No groups configured. Use /groups new @NAME to create one."
            )
            return
        lines = ["[b]Groups:[/b]"]
        for g in sorted(groups, key=lambda g: g.name):
            marker = "[green]✓[/green]" if reg.is_subscribed(g.name) else "[dim]○[/dim]"
            where = ", ".join(g.transports) or "-"
            extras = []
            if g.members:
                extras.append(f"{len(g.members)} member(s)")
            if g.tags:
                extras.append(f"tags: {', '.join('@' + t for t in g.tags)}")
            suffix = f"  [dim]{'; '.join(extras)}[/dim]" if extras else ""
            lines.append(f"  {marker}  [b]@{g.name}[/b]  via {where}{suffix}")
        lines.append(
            "[dim]/groups @NAME for details  ·  /groups new @NAME to create[/dim]"
        )
        self._log_system("\n".join(lines))

    def _groups_show(self, reg: object, name_arg: str) -> None:
        from ..core.groups import GroupRegistry
        assert isinstance(reg, GroupRegistry)
        name = name_arg.lstrip("@")
        g = reg.get(name)
        if g is None:
            self._log_system(
                f"No such group @{name}.  Use /groups new @{name} to create it."
            )
            return
        lines = [f"[b]@{g.name}[/b]  ({g.display_name})"]
        lines.append(
            f"  outbound transports: {', '.join(g.transports) or '(none)'}"
        )
        lines.append(
            f"  subscribed: {'[green]yes[/green]' if reg.is_subscribed(g.name) else '[dim]no[/dim]'}"
        )
        if g.members:
            lines.append("  incoming members:")
            for m in g.members:
                who = m.transport or "any"
                lines.append(f"    [dim]{m.identifier}  [{who}][/dim]")
        else:
            lines.append("  incoming members: (none)")
        lines.append(
            "  incoming tags: "
            + (", ".join("@" + t for t in g.tags) if g.tags else "(none)")
        )
        self._log_system("\n".join(lines))

    def _handle_position_command(self, arg: str) -> None:
        """Show or set the station position.

        /position           — show current position
        /position <grid>    — set position by Maidenhead grid square (e.g. FN31pr)
        /position clear     — remove manually configured position
        """
        if self.core is None:
            return
        from ..core.maidenhead import grid_to_latlon, _GRID_RE as _GRE

        sub = arg.strip()
        if not sub:
            from ..core.position import position_from_config
            pos = self._position
            if pos is None:
                pos = position_from_config(self.core.config)
            if pos is None:
                self._log_system(
                    "No position set. Use /position <grid> to set manually."
                )
            else:
                self._log_system(
                    f"Position: {pos.lat:+.4f}°  {pos.lon:+.4f}°  "
                    f"grid [b]{pos.grid}[/b]  [dim]({pos.source})[/dim]"
                )
            return

        if sub.lower() == "clear":
            pos_data = self.core.config.data.get("position", {})
            changed = False
            for key in ("lat", "lon", "fixed_grid"):
                if key in pos_data:
                    del pos_data[key]
                    changed = True
            if changed:
                self.core.config.save()
                self._position = None
                self._log_system("Position cleared.")
            else:
                self._log_system("No manually configured position to clear.")
            return

        # Treat argument as a Maidenhead grid square.
        grid = sub.upper()
        if not _GRE.match(grid):
            self._log_system(
                f"'{sub}' is not a valid Maidenhead grid (e.g. FN31, FN31pr)."
            )
            return
        try:
            lat, lon = grid_to_latlon(grid)
        except ValueError as exc:
            self._log_system(f"Grid error: {exc}")
            return
        self.core.config.set("position", "lat", round(lat, 6))
        self.core.config.set("position", "lon", round(lon, 6))
        self.core.config.save()
        # Update cached position so Health panel reflects it immediately.
        from ..core.position import Position
        self._position = Position(lat=lat, lon=lon, source="manual")
        self._log_system(
            f"Position set: {lat:+.4f}°  {lon:+.4f}°  grid [b]{grid}[/b]"
        )

    @work
    async def _handle_start_command(self, arg: str) -> None:
        """Launch or reconnect the backing process for a transport.

        /start          — start the currently active transport's backing app
        /start js8call  — start a specific transport by name
        """
        if self.core is None:
            return
        pm = getattr(self.core, "proc_manager", None)
        if pm is None:
            self._log_system("Process manager unavailable.")
            return

        name = (arg.strip().lower() or self.active_transport or "").strip()
        if not name:
            self._log_system("usage: /start [transport_name]  (or pick a mode first)")
            return

        if pm.definition(name) is None:
            known = ", ".join(pm.known_transports())
            self._log_system(
                f"Unknown transport {name!r}. Manageable transports: {known}"
            )
            return

        transport = next(
            (t for t in self.core.transports if t.name == name), None
        )
        if transport is None:
            self._log_system(
                f"{name} is not enabled in your config. Add it to [transports.{name}]."
            )
            return

        # Build the contextual prompt function so the modal runs in the TUI.
        async def _prompt(transport_name: str, default_cmd: str) -> str | None:
            result: list[str | None] = [None]
            ev = asyncio.Event()

            def _cb(val: str | None) -> None:
                result[0] = val
                ev.set()

            self.app.push_screen(
                LaunchCmdScreen(transport_name, default_cmd), _cb
            )
            await ev.wait()
            return result[0]

        # Show a status line before the potentially-slow launch.
        if pm.is_running(name):
            self._log_system(f"Reconnecting {name}…")
        else:
            self._log_system(f"Starting {name}…")

        ok = await pm.start(name, transport, prompt_fn=_prompt)
        if ok:
            self._log_system(f"{name} ready.")
            self._refresh_health()
            self._update_status()
        else:
            self._log_system(
                f"Failed to start {name}. Check logs or set "
                f"[transports.{name}].launch_cmd in your config."
            )

    def _handle_browse_command(self, arg: str) -> None:
        """Open the NomadNet page viewer: /browse <hash>[:/page/x.mu]."""
        if self.core is None:
            return
        if not getattr(self.core, "browser", None):
            self._log_system("NomadNet browser unavailable.")
            return
        if not arg:
            self._log_system("usage: /browse <hash>[:/page/x.mu]  (see /nodes)")
            return
        dest, path, fields = parse_address(arg)
        if not dest:
            self._log_system("usage: /browse <hash>[:/page/x.mu]")
            return
        # Offline is fine for cached pages; only dynamic (field_data) pages need
        # a live link.
        offline = not self.core.browser.available
        if offline and fields:
            self._log_system(
                "Reticulum is down; dynamic pages need a live link."
            )
            return
        if offline:
            self._log_system("Reticulum is down — showing cached page (if any).")
        self.push_screen(
            BrowseScreen(self.core.browser, dest, path, fields, prefer_cache=offline)
        )

    def _handle_nodes_command(self) -> None:
        """List discovered NomadNet nodes in the message log."""
        if self.core is None:
            return
        ret = next(
            (t for t in self.core.transports if t.name == "reticulum"), None
        )
        if ret is None or not ret.running:
            self._log_system("Reticulum is not running; no nodes to list.")
            return
        nodes = ret.known_nodes()
        if not nodes:
            self._log_system("No NomadNet nodes heard yet (waiting for announces).")
            return
        self._log_system("NomadNet nodes (use /browse <hash>):")
        for n in nodes[:20]:
            name = n["name"] or "(unnamed)"
            self._log_system(f"  {n['dest']}  {name}")

    def _handle_peers_command(self) -> None:
        """List discovered LXMF peers (messageable identities) in the log."""
        if self.core is None:
            return
        ret = next(
            (t for t in self.core.transports if t.name == "reticulum"), None
        )
        if ret is None or not ret.running:
            self._log_system("Reticulum is not running; no peers to list.")
            return
        peers = ret.known_peers()
        if not peers:
            self._log_system("No LXMF peers heard yet (waiting for announces).")
            return
        self._log_system("LXMF peers (messageable identities):")
        for p in peers[:20]:
            name = p["name"] or "(anonymous)"
            self._log_system(f"  {p['dest']}  {name}")

    def _open_from_monitor(self, idx: int) -> None:
        if idx >= len(self._monitor_entries):
            return
        thread_key, transport = self._monitor_entries[idx]
        self._open_thread(thread_key, transport)

    def _open_thread(self, thread_key: str, transport: str) -> None:
        """Open a conversation, switching the active mode to its transport."""
        # Entering a conversation switches the active mode to its transport.
        if transport and transport != self.active_transport:
            self.active_transport = transport
            self._update_radio_claim()
            self._update_modebar()
        self.current_target = thread_key
        self.view = "active"
        self.query_one("#main", ContentSwitcher).current = "active-view"
        self._enable_composer(True)
        self._refresh_threads()
        self._load_thread(thread_key)
        self._update_status()
        self.query_one("#composer", Input).focus()

    # -- search palette -------------------------------------------------------

    def action_search(self) -> None:
        """Open the full-text history search palette (Ctrl+F)."""
        self._show_search()

    def action_close_search(self) -> None:
        """Close the search palette, returning to the prior surface."""
        if self.view != "search":
            return
        prev = self._search_prev_view
        if prev == "monitor":
            self._show_watch()
        elif prev == "health":
            self._show_health()
        elif prev == "logs":
            self._show_logs()
        elif prev == "favorites":
            self._show_favorites()
        elif prev == "nomadnet":
            self._show_nomadnet()
        else:
            self._show_active()

    def _show_search(self) -> None:
        """Show the search palette and focus its input.

        Remembers the current surface so Esc can restore it. The global composer
        is disabled here; the dedicated search box drives the query.
        """
        if self.view != "search":
            self._search_prev_view = self.view
        self.view = "search"
        self.query_one("#main", ContentSwitcher).current = "search-view"
        self._enable_composer(False)
        self._update_modebar()
        self._update_status()
        box = self.query_one("#search-input", Input)
        box.focus()
        # Re-run the current term so reopening keeps prior results in view.
        self._run_search(box.value.strip())

    def _run_search(self, term: str) -> None:
        """Execute a search and render ranked hits (newest/most-relevant first)."""
        results = self.query_one("#search-results", ListView)
        results.clear()
        self._search_hits.clear()
        term = (term or "").strip()
        if self.core is None or not term:
            return
        try:
            hits = self.core.store.search_ranked(term, limit=200)
        except Exception:  # noqa: BLE001 - a bad query must never crash the UI
            hits = []
        if not hits:
            results.append(ListItem(Label("[dim]No matches.[/dim]")))
            self._search_hits.append(("", ""))
            return
        for hit in hits:
            results.append(ListItem(Label(self._format_search_hit(hit))))
            self._search_hits.append((hit.thread_key, hit.message.transport or ""))

    def _format_search_hit(self, hit) -> str:
        """Render one search result row: time · mode · sender · snippet."""
        msg = hit.message
        ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")
        name = msg.transport or "?"
        color = self._mode_color(name)
        mode_tag = f"[{color}]{name:<9}[/{color}]"
        who = (
            "[cyan]you[/cyan]"
            if msg.status is not DeliveryStatus.RECEIVED
            else msg.sender
        )
        snippet = self._render_snippet(hit.snippet)
        return f"[dim]{ts}[/dim] {mode_tag} {who}: {snippet}"

    @staticmethod
    def _render_snippet(snippet: str) -> str:
        """Escape Rich markup in a snippet, then apply match highlighting.

        The store wraps matched terms in sentinel control chars (SNIPPET_OPEN/
        CLOSE) that can't occur in real text, so we can safely escape first and
        swap the sentinels for reverse-video markup afterward.
        """
        from ..core.store import SNIPPET_CLOSE, SNIPPET_OPEN

        safe = snippet.replace("\\", "\\\\").replace("[", "\\[")
        safe = safe.replace(SNIPPET_OPEN, "[reverse]").replace(
            SNIPPET_CLOSE, "[/reverse]"
        )
        return safe

    # -- "All chats" archive --------------------------------------------------

    def action_archive(self) -> None:
        """Open the cross-mode "All chats" archive surface."""
        self._show_archive()

    def _show_archive(self) -> None:
        """Show every conversation across all modes (read-only), newest first."""
        self.view = "archive"
        self.query_one("#main", ContentSwitcher).current = "archive-view"
        self._enable_composer(False)
        self._render_archive()
        self._update_modebar()
        self._update_status()

    def _render_archive(self) -> None:
        """Render the conversation rollups, honouring the mode filter."""
        try:
            lst = self.query_one("#archive-list", ListView)
        except Exception:  # noqa: BLE001 - surface not mounted yet
            return
        lst.clear()
        self._archive_rows.clear()
        if self.core is None:
            return
        try:
            summaries = self.core.store.thread_summaries()
        except Exception:  # noqa: BLE001 - never let a query crash the UI
            summaries = []
        flt = self._archive_mode_filter
        shown = [s for s in summaries if not flt or s.transport == flt]
        if not shown:
            msg = (
                f"[dim]No conversations for '{flt}'.[/dim]"
                if flt
                else "[dim]No conversations yet.[/dim]"
            )
            lst.append(ListItem(Label(msg)))
            self._archive_rows.append(("", ""))
            self._update_archive_help(len(shown))
            return
        for s in shown:
            lst.append(ListItem(Label(self._format_archive_row(s))))
            self._archive_rows.append((s.thread_key, s.transport))
        self._update_archive_help(len(shown))

    def _format_archive_row(self, s) -> str:
        """Render one archive row: date  mode  thread  count  last preview."""
        ts = (s.last_ts or "")[:16].replace("T", " ")
        name = s.transport or "?"
        color = self._mode_color(name)
        mode_tag = f"[{color}]{name:<9}[/{color}]"
        who = (
            "you"
            if s.last_status not in ("received", "")
            else (s.last_sender or "?")
        )
        preview = (s.last_content or "").replace("\n", " ")
        if len(preview) > 48:
            preview = preview[:47] + "\u2026"
        preview = preview.replace("\\", "\\\\").replace("[", "\\[")
        title = self._display_id(s.thread_key)
        return (
            f"[dim]{ts}[/dim] {mode_tag} [b]{title}[/b] "
            f"[dim]({s.count})[/dim]  {who}: {preview}"
        )

    def _update_archive_help(self, count: int) -> None:
        try:
            help_line = self.query_one("#archive-help", Static)
            btn = self.query_one("#archive-mode", Button)
        except Exception:  # noqa: BLE001 - surface not mounted yet
            return
        flt = self._archive_mode_filter
        scope = f"mode: {flt}" if flt else "all modes"
        help_line.update(
            f"All chats — {count} conversation(s), {scope} (read-only). "
            "Enter opens one."
        )
        btn.label = f"\u25cf {flt}" if flt else "\u25cb Mode"

    def _cycle_archive_filter(self) -> None:
        """Cycle the archive's mode filter: all -> each transport -> all."""
        if self.core is None:
            return
        names = [t.name for t in self.core.transports]
        # Only offer filters for modes that actually have conversations, so the
        # cycle never lands on an always-empty mode.
        try:
            present = {
                s.transport for s in self.core.store.thread_summaries() if s.transport
            }
        except Exception:  # noqa: BLE001
            present = set()
        options: list[str | None] = [None] + [n for n in names if n in present]
        try:
            idx = options.index(self._archive_mode_filter)
        except ValueError:
            idx = 0
        self._archive_mode_filter = options[(idx + 1) % len(options)]
        self._render_archive()
        self._update_status()

    # -- composer -------------------------------------------------------------
    def _enable_composer(self, enabled: bool) -> None:
        composer = self.query_one("#composer", Input)
        composer.disabled = not enabled
        composer.placeholder = (
            "Type a message or /help ..."
            if enabled
            else "Read-only view - tap the Watch tab, or pick a mode"
        )

    def _update_composer_placeholder(self) -> None:
        """Update the composer hint text to match the current mode and target."""
        try:
            composer = self.query_one("#composer", Input)
        except Exception:  # noqa: BLE001
            return
        if composer.disabled:
            return
        if self.active_transport == "meshcore" and not self.current_target:
            composer.placeholder = (
                "click a channel to chat  ·  /to @0 for public channel"
            )
        else:
            composer.placeholder = "Type a message or /help ..."

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # The dedicated search box drives the history palette; Enter just keeps
        # the results (selection opens a thread). Route it before view logic.
        if event.input.id == "search-input":
            self._run_search(event.value.strip())
            return
        # In NomadNet the bottom composer is repurposed as an address bar so the
        # input position stays put across modes - route by view, not widget id.
        # Slash-commands (/fav, /help, /quit, ...) must still work there, so we
        # only treat *non-command* input as a page address.
        if self.view == "nomadnet" and not event.value.lstrip().startswith("/"):
            addr = event.value.strip()
            event.input.value = ""
            if not addr:
                return
            self._push_cmd_history(addr)
            dest, path, fields = parse_address(addr)
            if not dest:
                self._log_system("usage: <node hash>[:/page/x.mu]")
                return
            self._open_nomad(dest, path, fields)
            return
        # In Favorites the bottom composer is an "add favorite" bar; non-command
        # input adds the typed id (works offline - peer need not be online).
        if self.view == "favorites" and not event.value.lstrip().startswith("/"):
            raw = event.value.strip()
            event.input.value = ""
            if raw:
                self._push_cmd_history(raw)
                self._add_favorite_from_input(raw)
            return
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self._push_cmd_history(text)
        # Hidden easter egg: the classic adventure magic word opens the About
        # screen instead of sending. (Undocumented; see also the Ctrl+G chord.)
        if text.lower() == "xyzzy":
            self.action_about()
            return
        if text.startswith("/"):
            await self._handle_command(text)
            return
        # Enforce the active protocol's documented per-message size cap.
        if not self._check_compose_limit(text):
            event.input.value = text  # keep their text so they can trim it
            self._update_status()
            return
        self._send(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        # Live, type-ahead history search (FTS prefix-matches the last word).
        if event.input.id == "search-input":
            if self.view == "search":
                self._run_search(event.value.strip())
            return
        # Keep the live size counter in the status bar current as the operator
        # types into the active mode's composer.
        if (
            event.input.id == "composer"
            and self.view == "active"
            and self._compose_limit
        ):
            try:
                self._update_status()
            except Exception:  # noqa: BLE001 - UI may be mid-teardown
                pass

    def _show_help(self) -> None:
        """Print a mode-aware command list for ``/help``.

        Shows the active mode's commands first (only those actually usable in
        that panel — see ``_MODE_COMMAND_HELP``), then the universal commands
        that work in every mode. With no mode picked yet, just point the
        operator at F3 so the list isn't misleadingly empty.
        """
        mode = self.active_transport
        if mode and mode in _MODE_COMMAND_HELP:
            self._log_system(f"{mode} commands:")
            for line in _MODE_COMMAND_HELP[mode]:
                self._log_system(f"  {line}")
        elif mode:
            self._log_system(f"{mode}: no mode-specific commands.")
        else:
            self._log_system("Pick a mode (F3) to see its commands.")
        self._log_system(f"Everywhere: {_UNIVERSAL_COMMAND_HELP}")

    async def _handle_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/quit", "/q", "/exit"):
            await self.action_quit()
        elif cmd == "/help":
            self._show_help()
        elif cmd == "/to":
            if not self.active_transport:
                self._log_system("Pick a mode first (press F3).")
                return
            if not arg:
                self._log_system("usage: /to <callsign|@GROUP> [message]")
                return
            # Accept an optional trailing message: "/to @EMS SNR?" switches to the
            # @EMS conversation AND sends "SNR?". Only the first token is the
            # target; the remainder (if any) is sent as a message.
            target, _, trailing = arg.partition(" ")
            self.current_target = self._normalize_target(target)
            self._refresh_threads()
            self._load_thread(self.current_target)
            self._update_status()
            trailing = trailing.strip()
            if trailing:
                self._send(trailing)
        elif cmd == "/monitor":
            self._show_watch()
        elif cmd in ("/favorites", "/favs"):
            self._show_favorites()
        elif cmd == "/logs":
            self._show_logs()
        elif cmd in ("/loglevel", "/loglvl"):
            self._set_log_level(arg)
        elif cmd in ("/search", "/find"):
            self._show_search()
            if arg:
                box = self.query_one("#search-input", Input)
                box.value = arg
                self._run_search(arg)
        elif cmd in ("/chats", "/archive", "/all"):
            self._show_archive()
        elif cmd == "/mode":
            # Typed convenience: cycle modes just like the F3 key.
            self.action_choose_mode()
        elif cmd == "/refresh":
            self.action_refresh()
        elif cmd == "/fav":
            self._handle_fav_command(arg)
        elif cmd == "/browse":
            self._handle_browse_command(arg)
        elif cmd == "/nodes":
            self._handle_nodes_command()
        elif cmd == "/peers":
            self._handle_peers_command()
        elif cmd in ("/whoami", "/id"):
            self.action_identity()
        elif cmd == "/announce":
            self.action_announce()
        elif cmd in ("/channel", "/chan"):
            await self._handle_channel_command(arg)
        elif cmd in ("/freq", "/frequency"):
            self._handle_freq_command(arg)
        elif cmd == "/band":
            self._handle_band_command(arg)
        elif cmd == "/inbox":
            self._js8_show_inbox()
        elif cmd == "/relay":
            self._js8_relay(arg)
        elif cmd == "/sms":
            self._js8_send_sms(arg)
        elif cmd == "/cmd":
            self._js8_directed_cmd(arg)
        elif cmd in ("/name", "/rename"):
            self._set_friendly_name(arg)
        elif cmd in ("/close", "/delete"):
            # Close the named conversation, or the open one when no id is given.
            self._close_chat(arg or self.current_target)
        elif cmd == "/path":
            # Optional explicit id; otherwise act on the open conversation.
            if arg:
                self.current_target = arg
            self.action_find_path()
        elif cmd == "/subject":
            self._set_winlink_subject(arg)
        elif cmd == "/attach":
            self._add_attachment(arg)
        elif cmd == "/save":
            self._winlink_save_attachments()
        elif cmd == "/connect":
            self._winlink_connect(arg or None)
        elif cmd in ("/gateway", "/gw"):
            self._set_winlink_gateway(arg)
        elif cmd == "/gateways":
            self._winlink_list_gateways()
        elif cmd in ("/tmpl", "/template"):
            self._handle_tmpl_command(arg)
        elif cmd == "/bands":
            self._handle_bands_command(arg)
        elif cmd == "/sched":
            self._handle_sched_command(arg)
        elif cmd == "/roster":
            self._handle_roster_command(arg)
        elif cmd == "/bandscan":
            self._handle_bandscan_command(arg)
        elif cmd in ("/subs", "/subscriptions"):
            self._handle_subs_command(arg)
        elif cmd in ("/groups", "/group"):
            self._handle_groups_command(arg)
        elif cmd in ("/contacts", "/contact"):
            self._handle_contacts_command(arg)
        elif cmd == "/bridge":
            self._handle_bridge_command(arg)
        elif cmd == "/filters":
            self._handle_filters_command(arg)
        elif cmd in ("/position", "/pos", "/grid"):
            self._handle_position_command(arg)
        elif cmd == "/start":
            self._handle_start_command(arg)
        elif cmd == "/net":
            self._handle_net_command(arg)
        elif cmd == "/wx":
            if arg:
                try:
                    self.query_one("#wx-grid", Input).value = arg.strip().upper()
                except Exception:  # noqa: BLE001
                    pass
            self._show_weather()
            self._wx_fetch_internet()
        else:
            self._log_system(f"unknown command: {cmd}")

    @work
    async def _send(self, text: str) -> None:
        if self.core is None:
            return
        if not self.active_transport:
            self._log_system("Pick a mode first (press F3).")
            return
        if not self.current_target:
            if self.active_transport == "meshcore":
                self._log_system(
                    "No channel selected. Use /to @0 for the public channel, "
                    "or /channel list to see available channels."
                )
            else:
                self._log_system("No conversation selected. Use /to <callsign|@GROUP>.")
            return
        t = self._active_transport_obj()
        caps = t.capabilities() if t else None
        if t and getattr(t, "carries_operator_identity", False):
            me = str(t.config.get("callsign") or self.core.config.station.get("callsign", "") or "")
        elif t:
            display_getter = getattr(t, "local_display_name", None)
            me = (display_getter() if callable(display_getter) else None) or ""
        else:
            me = str(self.core.config.station.get("callsign", "") or "")
        # Winlink is email-style: the single-line composer can't hold real
        # newlines, so let the operator type the literal escape "\n" to break the
        # body into multiple lines. (Other transports keep the text verbatim.)
        if self.active_transport == "winlink" and "\\n" in text:
            text = text.replace("\\n", "\n")
        # Radio interlock: a send on a continuous radio mode (JS8Call/Mercury)
        # keys the shared HF radio, so refuse while another transport holds it.
        # (Winlink sends only *queue* to Pat's outbox — the radio is keyed by the
        # connect session, which is gated separately in _winlink_connect.)
        il = self.core.radio_interlock
        if self.active_transport in self._continuous_radio_modes():
            dec = il.acquire(self.active_transport)
            if not dec.granted:
                self._log_system(
                    f"\u26d4 radio busy: {dec.blocked_by} is using the radio. "
                    f"Wait for it to finish (or stop it) before transmitting on "
                    f"{self.active_transport}."
                )
                return
        if self.current_target.startswith("@"):
            if not (caps and caps.supports_groups):
                self._log_system(
                    f"groups are not available on '{self.active_transport}'."
                )
                return
            msg = UnifiedMessage.to_group(me, self.current_target, text)
        else:
            msg = UnifiedMessage.direct(me, self.current_target, text)
        # Winlink is email-style: attach the pending subject (set via the Subject
        # button or /subject) and consume it so it doesn't bleed into the next.
        if self.active_transport == "winlink" and self._winlink_subject:
            msg.metadata["subject"] = self._winlink_subject
            self._winlink_subject = ""
            self._update_winlink_bar()
        # Attachments (any transport advertising supports_attachments — Winlink
        # multipart email, Reticulum LXMF file fields). DIRECT-only: a group/
        # broadcast send can't carry files, so keep the queue and warn instead.
        if caps and caps.supports_attachments and self._attach_queue:
            if self.current_target.startswith("@"):
                self._log_system(
                    "Attachments are only supported in direct messages; "
                    "the queue was kept."
                )
            else:
                msg.metadata["attach"] = list(self._attach_queue)
                self._attach_queue = []
                self._update_winlink_bar()
        # Send over the ACTIVE mode only (no auto-selection / fallback).
        ok = await self.core.router.send(msg, force_transport=self.active_transport)
        self._render_message(msg, outgoing=True, ok=ok)
        # Echo into the all-transport Watch feed so a watched conversation
        # (e.g. a MeshCore channel) shows both the messages you receive AND the
        # ones you send, not just inbound traffic.
        self._append_monitor(msg)
        self._refresh_threads()

    # -- rendering (identical everywhere) -------------------------------------
    def _sender_is_replyable(self, msg: UnifiedMessage) -> bool:
        """True when a message's sender is a real, addressable identity.

        Anonymous channel traffic (a MeshCore channel message with no embedded
        sender name) carries a synthetic ``chanN`` placeholder that is not a
        person you can DM — so its name must not be turned into a reply link.
        """
        sender = (msg.sender or "").strip()
        if not sender:
            return False
        if msg.metadata.get("mc_anon"):
            return False
        # Synthetic channel placeholder like 'chan0' (defensive: also covers
        # any persisted message that predates the mc_anon marker).
        if sender.startswith("chan") and sender[4:].isdigit():
            return False
        return True

    def _render_message(
        self, msg: UnifiedMessage, outgoing: bool = False, ok: bool = True
    ) -> None:
        log = self.query_one("#messages", RichLog)
        ts = msg.timestamp.strftime("%H:%M")
        # Treat a message as outbound when the caller says so (live send) OR when
        # its persisted status is anything but RECEIVED - so messages reloaded
        # from the store (e.g. after a restart) still render as "you" and keep
        # their delivery indicator.
        is_out = outgoing or msg.status is not DeliveryStatus.RECEIVED
        via = msg.transport or ("..." if is_out else "?")
        if is_out:
            who = "[bold cyan]you[/bold cyan]"
        else:
            # Inbound senders are clickable: clicking opens a direct reply to
            # that person (handy in a MeshCore channel / JS8 @group where one
            # thread carries many senders). Strip quotes so the value can't
            # break out of the @click action's argument string.
            name = self._display_id(msg.sender)
            ident = (msg.sender or "").replace("'", "").replace("\\", "")
            via_t = (msg.transport or "").replace("'", "").replace("\\", "")
            if ident and self._sender_is_replyable(msg):
                who = (
                    f"[bold][@click=app.reply_to('{ident}', '{via_t}')]"
                    f"{name}[/][/bold]"
                )
            else:
                who = f"[bold]{name}[/bold]"
        lock = " [enc]" if msg.metadata.get("encrypted") else ""
        status = "" if ok else " [red](unsent)[/red]"
        # Outbound delivery indicator (LXMF receipts): show ✓ delivered / ✗ failed
        # from either the live receipt map or the persisted status.
        if is_out and ok:
            dlv = self._delivery_status.get(msg.msg_id)
            if dlv == "delivered" or msg.status is DeliveryStatus.DELIVERED:
                status = " [green]\u2713[/green]"
            elif dlv == "failed" or msg.status is DeliveryStatus.FAILED:
                status = " [red]\u2717 (failed)[/red]"
        tag = ""
        if msg.address_type is AddressType.GROUP and msg.group:
            if msg.group.isdigit() and msg.transport == "meshcore":
                ch_name = self._meshcore_channel_name(int(msg.group))
                tag = f" [magenta]#{ch_name or msg.group}[/magenta]"
            else:
                tag = f" [magenta]@{msg.group}[/magenta]"
        band = msg.metadata.get("band", "")
        band_tag = f" [dim]{band}[/dim]" if band else ""
        log.write(
            f"[dim]{ts}[/dim] [yellow]\\[{via}][/yellow]{tag}{band_tag} {who}: "
            f"{self._subject_md(msg)}{msg.content}{lock}{status}"
            f"{self._attachments_md(msg)}"
        )

    def _log_system(self, text: str) -> None:
        self.query_one("#messages", RichLog).write(f"[dim italic]{text}[/dim italic]")

    def _ident_markup(self) -> str:
        """Build the status-bar ``id:`` fragment for the active transport.

        For anonymous transports (Reticulum) it shows your public display name
        plus your address; the address is wrapped in a ``@click`` action so a
        click copies the FULL value to the clipboard. Identity-carrying
        transports (HF) show the callsign. Returns '' when no active transport.
        """
        if self.core is None:
            return ""
        t = self._active_transport_obj()
        if t is None:
            return ""
        return f"    id: {self._transport_ident(t)}"

    def _all_idents_markup(self) -> str:
        """Build an ``id:`` fragment listing *every* transport's identity.

        Used by the Watch panel, which spans all transports, so the operator can
        see all of their identities at once (callsign on HF, anonymous hash +
        display name on Reticulum, node name on MeshCore). Each address stays
        click-to-copy. Falls back to '' when no transports are configured.
        """
        if self.core is None:
            return ""
        parts = [
            f"{t.name} {self._transport_ident(t)}" for t in self.core.transports
        ]
        if not parts:
            return ""
        return "    id: " + "  |  ".join(parts)

    def _transport_ident(self, t: Transport) -> str:
        """Render one transport's identity as ``name label (sec)`` markup.

        The address/callsign is wrapped in a ``@click`` action so clicking it
        copies the FULL value to the clipboard.
        """
        c = t.capabilities()
        name = ""
        if c.carries_operator_identity:
            full = (self.core.station.callsign or "") if self.core else ""
            label = full or "(no callsign)"
        else:
            full = t.local_identity() or ""
            # local_identity() returns 'rns:anonymous' before RNS is up.
            if not full or full.startswith("rns:"):
                full = ""
            label = self._short(full) if full else "anonymous"
            try:
                name = t.local_display_name()
            except AttributeError:
                name = ""
        sec = "enc" if c.supports_encryption else "plain"
        if full:
            # Clickable: copies the full address/callsign to the clipboard.
            shown = f"[@click=app.copy_address('{full}')][u]{label}[/u][/]"
        else:
            shown = label
        who = f"{name} {shown}" if name else shown
        return f"{who} ({sec})"

    def _log_badge_markup(self) -> str:
        """Status-bar WARN/ERR badge from the in-memory log's peak severity.

        Shows nothing while the Logs surface is open (the operator is already
        looking at them, and opening it clears the peak). Clicking the badge
        jumps straight to the Logs surface.
        """
        if self.view == "logs":
            return ""
        ring = self._log_ring()
        if ring is None:
            return ""
        peak = ring.peak_level()
        if peak >= logging.ERROR:
            label, colour = "ERR", "red"
        elif peak >= logging.WARNING:
            label, colour = "WARN", "yellow"
        else:
            return ""
        return f"    [@click=app.logs()][{colour}]\u26a0 {label}[/{colour}][/]"

    def _update_status(self) -> None:
        if self.core is None:
            return
        up = ", ".join(self.core.running_transports) or "none up"
        target = (
            self._display_id(self.current_target)
            if self.current_target
            else "(none)"
        )
        mode = self.active_transport or "(none)"
        # The Watch panel spans every transport, so show all of your
        # identities there; other views show just the active one.
        ident_part = (
            self._all_idents_markup()
            if self.view == "monitor"
            else self._ident_markup()
        )
        # Announce/traffic tallies intentionally omitted from the status bar:
        # the same "is the transport hearing the network?" signal lives in the
        # Health view, which is the single source of truth for reachability.
        # Favorites-only indicator reflects whichever filter applies to the
        # current surface (Watch stream vs. the active mode's thread list).
        fav_on = (
            (self.view == "monitor" and self._monitor_fav_only)
            or (self.view in ("active", "nomadnet") and self._active_fav_only)
        )
        if self.view == "monitor" and self._monitor_group_filter:
            filt = f"    [group:@{self._monitor_group_filter}]"
        elif fav_on:
            filt = "    [fav-only]"
        else:
            filt = ""
        counter = self._compose_counter_markup()
        badge = self._log_badge_markup()
        self.query_one("#statusbar", Static).update(
            f" view: {self.view}    mode: {mode}{ident_part}    target: {target}    "
            f"up: {up}{filt}{badge}{counter}"
        )

def run(config_path: str | None = None) -> None:
    """Entry point used by the CLI ``radioapp tui`` command."""
    RadioTUI(config_path).run()
