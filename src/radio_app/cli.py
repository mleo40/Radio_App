"""Command-line interface (the non-critical CLI subset).

A thin stdlib (``argparse``) frontend over the same core the GUI/TUI use:
send, read history, manage groups/subscriptions, inspect transports & config.
Interactive features (live chat, page browsing) are intentionally left to a TUI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .app import App
from .config import Config, default_config_path
from .core.message import UnifiedMessage
from .core.selector import SelectionMode


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


# -- parser ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radioapp",
        description="Unified messaging over radio, internet and LoRa.",
    )
    parser.add_argument(
        "--version", action="version", version=f"radioapp {__version__}"
    )
    parser.add_argument("--config", help="path to config.toml", default=None)
    sub = parser.add_subparsers(dest="command")

    p_send = sub.add_parser("send", help="send a message")
    target = p_send.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", help="recipient identity/callsign")
    target.add_argument("--group", help="group name, e.g. EMS")
    target.add_argument("--broadcast", action="store_true", help="send to everyone")
    p_send.add_argument("text", help="message body")
    p_send.add_argument(
        "--mode",
        choices=[m.value for m in SelectionMode],
        help="transport-selection mode",
    )
    p_send.add_argument("--transport", help="force a specific transport")
    p_send.add_argument(
        "--encrypt",
        action="store_true",
        help="mark payload as encrypted (refused on HF unless explicitly allowed)",
    )
    p_send.set_defaults(func=_cmd_send)

    p_threads = sub.add_parser("threads", help="list saved conversations")
    p_threads.set_defaults(func=_cmd_threads)

    p_read = sub.add_parser("read", help="print a conversation")
    rtarget = p_read.add_mutually_exclusive_group(required=True)
    rtarget.add_argument("--thread", help="thread key, e.g. @EMS or a callsign")
    rtarget.add_argument("--to", help="direct conversation with this identity")
    p_read.add_argument("--limit", type=int, default=200)
    p_read.set_defaults(func=_cmd_read)

    p_history = sub.add_parser(
        "history", help="browse stored message history (offline, read-only)"
    )
    p_history.add_argument("--thread", help="thread key, e.g. @EMS or a callsign")
    p_history.add_argument("--to", help="alias for --thread (a direct conversation)")
    p_history.add_argument(
        "--mode", help="only this transport (js8call, reticulum, meshcore, winlink)"
    )
    p_history.add_argument(
        "--from", dest="sender", help="only messages from this sender (substring)"
    )
    p_history.add_argument("--group", help="only this group (with or without @)")
    p_history.add_argument("--since", help="on/after this date (YYYY-MM-DD or ISO)")
    p_history.add_argument("--until", help="on/before this date (YYYY-MM-DD or ISO)")
    p_history.add_argument(
        "--limit", type=int, default=200, help="max messages (most recent)"
    )
    p_history.add_argument(
        "--status",
        choices=["pending", "sent", "delivered", "failed", "received"],
        help="only messages with this delivery status",
    )
    p_history.add_argument(
        "--snr-min",
        type=float,
        dest="snr_min",
        metavar="N",
        help="only messages with SNR >= N (JS8Call metadata)",
    )
    p_history.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="shorthand for --since/--until covering one whole day",
    )
    p_history.add_argument(
        "--format", choices=["text", "json"], default="text"
    )
    p_history.set_defaults(func=_cmd_history)

    p_search = sub.add_parser(
        "search", help="search stored message bodies (offline, read-only)"
    )
    p_search.add_argument("text", help="text to find in message bodies")
    p_search.add_argument("--mode", help="only this transport")
    p_search.add_argument(
        "--from", dest="sender", help="only messages from this sender (substring)"
    )
    p_search.add_argument("--group", help="only this group (with or without @)")
    p_search.add_argument("--since", help="on/after this date (YYYY-MM-DD or ISO)")
    p_search.add_argument("--until", help="on/before this date (YYYY-MM-DD or ISO)")
    p_search.add_argument("--limit", type=int, default=100)
    p_search.add_argument(
        "--status",
        choices=["pending", "sent", "delivered", "failed", "received"],
        help="only messages with this delivery status",
    )
    p_search.add_argument(
        "--snr-min",
        type=float,
        dest="snr_min",
        metavar="N",
        help="only messages with SNR >= N (JS8Call metadata)",
    )
    p_search.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="shorthand for --since/--until covering one whole day",
    )
    p_search.add_argument(
        "--format", choices=["text", "json"], default="text"
    )
    p_search.set_defaults(func=_cmd_search)

    p_export = sub.add_parser(
        "export", help="export conversation history to json, txt, md or maildir"
    )
    export_target = p_export.add_mutually_exclusive_group(required=True)
    export_target.add_argument(
        "--thread", help="export a single thread, e.g. @EMS or a callsign"
    )
    export_target.add_argument(
        "--all", action="store_true", help="export every stored thread"
    )
    p_export.add_argument(
        "--format",
        choices=["json", "txt", "md", "maildir"],
        default="txt",
        help="output format (default: txt)",
    )
    p_export.add_argument(
        "--out",
        help="output file path (json/txt/md) or directory (maildir); "
        "default: stdout for text formats",
    )
    p_export.set_defaults(func=_cmd_export)

    p_tmpl = sub.add_parser("templates", help="list or send canned message templates")
    p_tmpl.add_argument(
        "action", choices=["list", "send"], nargs="?", default="list",
        help="list: show all templates; send: transmit a named template",
    )
    p_tmpl.add_argument("name", nargs="?", help="template name for 'send'")
    tmpl_target = p_tmpl.add_mutually_exclusive_group()
    tmpl_target.add_argument("--to", help="recipient identity/callsign")
    tmpl_target.add_argument("--group", help="group name, e.g. EMS")
    p_tmpl.add_argument("--transport", help="force a specific transport")
    p_tmpl.set_defaults(func=_cmd_templates)

    p_listen = sub.add_parser("listen", help="stream incoming messages to stdout")
    p_listen.add_argument("--group", help="only show this group")
    p_listen.set_defaults(func=_cmd_listen)

    p_groups = sub.add_parser("groups", help="list configured groups")
    p_groups.set_defaults(func=_cmd_groups)

    p_group = sub.add_parser(
        "group", help="manage a group's cross-mode membership (incoming)"
    )
    p_group.add_argument("name", help="group name, e.g. ems")
    p_group.add_argument(
        "action",
        choices=["show", "add", "remove", "tag", "untag", "delete"],
        help="show | add/remove a member | tag/untag | delete the group",
    )
    p_group.add_argument(
        "value",
        nargs="?",
        help="member spec 'transport:identifier' (add/remove) or a tag (tag/untag)",
    )
    p_group.set_defaults(func=_cmd_group)

    p_sub = sub.add_parser("sub", help="manage group subscriptions")
    p_sub.add_argument("action", choices=["add", "remove", "list"])
    p_sub.add_argument("group", nargs="?")
    p_sub.set_defaults(func=_cmd_sub)

    p_status = sub.add_parser("status", help="show transport status")
    p_status.set_defaults(func=_cmd_status)

    p_transports = sub.add_parser("transports", help="list transports + capabilities")
    p_transports.set_defaults(func=_cmd_transports)

    p_config = sub.add_parser("config", help="inspect/initialise/edit configuration")
    p_config.add_argument(
        "action", choices=["path", "show", "init", "get", "set"]
    )
    p_config.add_argument(
        "key",
        nargs="?",
        help="dotted key for get/set, e.g. 'storage.download_dir' or "
        "'transports.winlink.enabled'",
    )
    p_config.add_argument(
        "value",
        nargs="?",
        help="new value for 'set' (smart-typed: true/false/int/float, else "
        "string; use --string/--json to force)",
    )
    p_config.add_argument(
        "--string", action="store_true",
        help="for 'set': treat VALUE as a literal string (no smart typing)",
    )
    p_config.add_argument(
        "--json", action="store_true",
        help="for 'set': parse VALUE as JSON (e.g. a list: '[\"telnet\"]')",
    )
    p_config.set_defaults(func=_cmd_config)

    p_tui = sub.add_parser("tui", help="launch the terminal user interface")
    p_tui.set_defaults(func=_cmd_tui)

    p_setup = sub.add_parser(
        "setup", help="interactive setup wizard (all user settings, one file)"
    )
    p_setup.set_defaults(func=_cmd_setup)

    p_db = sub.add_parser(
        "db", help="database maintenance (stats / vacuum / prune)"
    )
    p_db.add_argument(
        "action",
        choices=["stats", "vacuum", "prune", "cache-prune", "cache-clear"],
        help="stats: sizes & counts; vacuum: reclaim space; prune: delete "
        "messages older than --days; cache-prune/cache-clear: trim the "
        "NomadNet page cache",
    )
    p_db.add_argument(
        "--days",
        type=int,
        default=0,
        help="age threshold in days for prune / cache-prune",
    )
    p_db.set_defaults(func=_cmd_db)

    p_backup = sub.add_parser(
        "backup", help="back up config + database to a .tar.gz archive"
    )
    p_backup.add_argument(
        "--out", help="output directory (default: the config file's directory)"
    )
    p_backup.set_defaults(func=_cmd_backup)

    p_restore = sub.add_parser(
        "restore", help="restore config + database from a backup archive"
    )
    p_restore.add_argument("archive", help="path to a backup .tar.gz")
    p_restore.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    p_restore.set_defaults(func=_cmd_restore)



    p_ret = sub.add_parser("reticulum", help="Reticulum / RNode utilities")
    p_ret.add_argument(
        "action", choices=["address", "status", "setup-rnode"]
    )
    p_ret.set_defaults(func=_cmd_reticulum)

    p_wl = sub.add_parser("winlink", help="Winlink / Pat utilities")
    p_wl.add_argument(
        "action",
        choices=[
            "status", "connect", "gateways", "forms", "forms-update",
            "form", "compose-form",
        ],
        nargs="?",
        default="status",
        help="status (default): check Pat; connect: run a session; "
        "gateways: list nearby RMS gateways; forms: list installed Winlink "
        "forms; forms-update: download the latest standard forms; "
        "form <template>: preview a form template + its fields; "
        "compose-form <template>: fill in a form and queue it to the outbox",
    )
    p_wl.add_argument(
        "target",
        nargs="?",
        help="RMS gateway callsign (connect) or form template path "
        "(form / compose-form, e.g. 'ICS/ICS213.txt')",
    )
    p_wl.add_argument(
        "--field",
        action="append",
        metavar="KEY=VALUE",
        help="form response for compose-form (repeatable, e.g. --field city=Boston)",
    )
    p_wl.add_argument(
        "--responses",
        metavar="FILE",
        help="JSON file of {field: value} responses for compose-form",
    )
    p_wl.add_argument("--to", help="override the form's To address")
    p_wl.add_argument("--cc", help="override the form's Cc address")
    p_wl.add_argument("--subject", help="override the form's Subject")
    p_wl.set_defaults(func=_cmd_winlink)

    p_js8 = sub.add_parser(
        "js8", help="JS8Call utilities (inbox / directed commands / relay / sms)"
    )
    p_js8.add_argument(
        "action",
        choices=["inbox", "cmd", "relay", "sms"],
        help="inbox: list JS8Call's stored messages; cmd: send a directed "
        "command (e.g. SNR?) to a station; relay: leave a store-and-forward "
        "message for a station; sms: text a phone via the APRS SMSGTE gateway",
    )
    p_js8.add_argument(
        "target",
        nargs="?",
        help="callsign or @GROUP for 'cmd'/'relay'; phone number for 'sms'",
    )
    p_js8.add_argument(
        "rest",
        nargs="*",
        help="command token for 'cmd' (e.g. SNR?), or message text for "
        "'relay'/'sms'",
    )
    p_js8.set_defaults(func=_cmd_js8)

    p_fav = sub.add_parser(
        "favorites",
        help="manage favorite peers (callsigns / RNS hex hashes) for alerts",
    )
    p_fav.add_argument(
        "action", choices=["add", "remove", "list", "set", "watch", "import-groups"]
    )
    p_fav.add_argument(
        "identity",
        nargs="?",
        help="callsign or RNS destination hash (required for add/remove/set)",
    )
    p_fav.add_argument("--label", default="", help="optional friendly label")
    p_fav.add_argument("--name", default=None, help="contact's name")
    p_fav.add_argument(
        "--grid", "--gridsquare", dest="grid", default=None,
        help="Maidenhead grid square, e.g. FN31pr",
    )
    p_fav.add_argument("--power", default=None, help="typical TX power, e.g. 5W")
    p_fav.add_argument("--notes", default=None, help="free-form notes")
    p_fav.add_argument(
        "--meta", action="append", default=[], metavar="KEY=VALUE",
        help="set an arbitrary metadata field (repeatable); empty value clears it",
    )
    p_fav.set_defaults(func=_cmd_favorites)

    p_browse = sub.add_parser(
        "browse", help="view a NomadNet page (read-only)"
    )
    p_browse.add_argument(
        "address",
        help="node address, e.g. <hash> or <hash>:/page/index.mu (|a=1|b=2 for vars)",
    )
    p_browse.add_argument(
        "--raw", action="store_true", help="print raw micron source instead of rendered"
    )
    p_browse.add_argument(
        "--timeout", type=float, default=20.0, help="seconds to wait for the page"
    )
    p_browse.add_argument(
        "--offline",
        action="store_true",
        help="serve the cached copy without touching the network",
    )
    p_browse.add_argument(
        "--live",
        action="store_true",
        help="force a fresh fetch over the air (default is cache-first)",
    )
    p_browse.set_defaults(func=_cmd_browse)

    p_nodes = sub.add_parser(
        "nodes", help="list discovered NomadNet nodes/sites (from announces)"
    )
    p_nodes.add_argument(
        "--wait", type=float, default=0.0, help="seconds to listen for announces first"
    )
    p_nodes.set_defaults(func=_cmd_nodes)

    p_peers = sub.add_parser(
        "peers", help="list discovered LXMF peers (messageable identities)"
    )
    p_peers.add_argument(
        "--wait", type=float, default=0.0, help="seconds to listen for announces first"
    )
    p_peers.set_defaults(func=_cmd_peers)

    p_nomad = sub.add_parser(
        "nomad", help="NomadNet maintenance (offline page cache)"
    )
    p_nomad.add_argument(
        "action", choices=["sync"], help="sync: cache favorite nodes' pages now"
    )
    p_nomad.add_argument(
        "--follow-links",
        action="store_true",
        help="also cache same-node /page/*.mu links (one level deep)",
    )
    p_nomad.add_argument(
        "--timeout", type=float, default=20.0, help="seconds to wait per page"
    )
    p_nomad.set_defaults(func=_cmd_nomad)

    p_pos = sub.add_parser("position", help="show current position (GPS or config)")
    p_pos.add_argument(
        "--gps",
        action="store_true",
        help="query gpsd for a live fix (requires gpsd running)",
    )
    p_pos.add_argument(
        "--beacon",
        action="store_true",
        help="send a position beacon on all supporting transports",
    )
    p_pos.set_defaults(func=_cmd_position)

    p_time = sub.add_parser("time", help="show current UTC and NTP clock offset")
    p_time.set_defaults(func=_cmd_time)

    p_bands = sub.add_parser("bands", help="offline band-plan and EmComm frequency reference")
    p_bands.add_argument("--band", metavar="BAND", help="filter by band, e.g. 40m")
    p_bands.add_argument("--mode", metavar="MODE", help="filter by mode: JS8, SSB, WINLINK, FM, CW")
    p_bands.add_argument("--region", metavar="REGION", default="US",
                         help="US (default) or INTL")
    p_bands.add_argument("--transport", metavar="TRANSPORT",
                         help="filter by transport: js8call, winlink")
    p_bands.set_defaults(func=_cmd_bands)

    p_sched = sub.add_parser("schedule", help="manage scheduled / windowed message sends")
    sched_sub = p_sched.add_subparsers(dest="sched_action")

    p_sched_add = sched_sub.add_parser("add", help="schedule a message for later")
    p_sched_add.add_argument("message", help="message text to send")
    sched_target = p_sched_add.add_mutually_exclusive_group(required=True)
    sched_target.add_argument("--to", help="recipient callsign/identity")
    sched_target.add_argument("--group", help="group name, e.g. EMS")
    p_sched_add.add_argument(
        "--at", metavar="HH:MM|YYYY-MM-DDTHH:MM",
        help="fire at this UTC time today (HH:MM) or a full ISO datetime",
    )
    p_sched_add.add_argument(
        "--delay", metavar="NmNh",
        help="fire after this delay, e.g. 30m, 1h, 90m",
    )
    p_sched_add.add_argument("--transport", dest="force_transport",
                             help="force a specific transport")

    sched_sub.add_parser("list", help="show pending scheduled messages")
    p_sched_cancel = sched_sub.add_parser("cancel", help="cancel a scheduled message")
    p_sched_cancel.add_argument("id", help="message id (from schedule list)")

    p_sched.set_defaults(func=_cmd_schedule)

    p_roster = sub.add_parser("roster", help="show recently-heard callsigns across all transports")
    p_roster.add_argument("--transport", help="filter to a specific transport")
    p_roster.add_argument(
        "--since", metavar="Nh", default="24h",
        help="look back N hours (default: 24h)",
    )
    p_roster.add_argument("--limit", type=int, default=50, help="max entries (default 50)")
    p_roster.set_defaults(func=_cmd_roster)

    return parser


# -- helpers -----------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


async def _with_app(config_path, func):
    app = App.from_config_path(config_path)
    # Install logging early so transport startup messages are captured.
    from .logging_setup import configure_logging

    configure_logging(app.config, stderr=True)
    await app.start()
    try:
        return await func(app)
    finally:
        await app.stop()


# -- commands ----------------------------------------------------------------


def _cmd_send(args: argparse.Namespace) -> int:
    mode = SelectionMode(args.mode) if args.mode else None
    cfg = Config.load(args.config)
    name = cfg.display_name
    if not cfg.is_station_configured():
        print(
            "note: no callsign configured. Run 'radioapp setup' before using HF.",
            file=sys.stderr,
        )

    if args.group:
        msg = UnifiedMessage.to_group(name, args.group, args.text)
    elif args.broadcast:
        msg = UnifiedMessage.broadcast(name, args.text)
    else:
        msg = UnifiedMessage.direct(name, args.to, args.text)
    if getattr(args, "encrypt", False):
        msg.encrypt = True

    async def run(app: App) -> int:
        # Wire an explicit terminal confirmation for encrypted-HF attempts.
        app.compliance.set_confirm(_confirm_encrypted_hf)
        ok = await app.router.send(msg, mode=mode, force_transport=args.transport)
        print(f"{'sent' if ok else 'FAILED'}: {msg.msg_id} via {msg.transport or '-'}")
        return 0 if ok else 1

    return _run(_with_app(args.config, run))


def _confirm_encrypted_hf(warning: str) -> bool:
    """Terminal confirmation for the regulatory guard. Defaults to NO."""
    print("\n" + warning + "\n", file=sys.stderr)
    answer = input("Type 'I ACCEPT' to transmit encrypted over HF: ").strip()
    return answer == "I ACCEPT"


def _cmd_threads(args: argparse.Namespace) -> int:
    async def run(app: App) -> int:
        rows = app.store.threads()
        if not rows:
            print("(no conversations yet)")
            return 0
        for key, count, last in rows:
            print(f"{key:<24} {count:>5} msgs   last: {last}")
        return 0

    return _run(_with_app(args.config, run))


def _cmd_read(args: argparse.Namespace) -> int:
    thread = args.thread or args.to

    async def run(app: App) -> int:
        for m in app.store.read_thread(thread, limit=args.limit):
            ts = m.timestamp.strftime("%Y-%m-%d %H:%M")
            via = f"[{m.transport}]" if m.transport else ""
            print(f"{ts} {via} {m.sender}: {m.content}")
        return 0

    return _run(_with_app(args.config, run))


def _parse_when(value: str, *, end: bool) -> datetime:
    """Parse a --since/--until value to an aware-UTC datetime.

    Accepts a full ISO-8601 timestamp or a bare ``YYYY-MM-DD`` date; a date-only
    value expands to the start of the day for ``--since`` and the end of the day
    for ``--until`` (so the whole day is inclusive). Naive values are treated as
    UTC. Raises ``ValueError`` on anything unparseable.
    """
    from datetime import time

    raw = value.strip()
    date_only = "T" not in raw and " " not in raw and len(raw) <= 10
    dt = datetime.fromisoformat(raw)
    if date_only:
        dt = datetime.combine(dt.date(), time.max if end else time.min)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _format_message_line(m, *, show_thread: bool = False) -> str:
    ts = m.timestamp.strftime("%Y-%m-%d %H:%M")
    via = f"[{m.transport or '?'}]"
    tgt = f"@{m.group}" if m.group else (m.recipient or "")
    arrow = f" -> {tgt}" if tgt else ""
    where = f" {{{m.thread_key}}}" if show_thread else ""
    groups = m.groups
    gtag = f" ({', '.join('@' + g for g in groups)})" if groups else ""
    return f"{ts} {via} {m.sender}{arrow}{where}{gtag}: {m.content}"


def _render_messages(msgs, fmt: str, *, show_thread: bool = False) -> None:
    """Print a list of UnifiedMessages as text lines or a JSON array."""
    import json

    if fmt == "json":
        print(json.dumps([m.to_dict() for m in msgs], indent=2))
        return
    if not msgs:
        print("(no matching messages)")
        return
    for m in msgs:
        print(_format_message_line(m, show_thread=show_thread))


def _history_window(args) -> tuple[datetime | None, datetime | None] | None:
    """Resolve --since/--until args to datetimes; None on a parse error."""
    try:
        since = _parse_when(args.since, end=False) if args.since else None
        until = _parse_when(args.until, end=True) if args.until else None
    except ValueError:
        print(
            "error: --since/--until must be YYYY-MM-DD or ISO 8601",
            file=sys.stderr,
        )
        return None
    return since, until


def _cmd_history(args: argparse.Namespace) -> int:
    """Browse stored history with optional filters (offline, no transports)."""
    from .core.store import MessageStore

    if getattr(args, "date", None):
        try:
            since = _parse_when(args.date, end=False)
            until = _parse_when(args.date, end=True)
        except ValueError:
            print("error: --date must be YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        window = _history_window(args)
        if window is None:
            return 2
        since, until = window
    thread = args.thread or args.to
    store = MessageStore(Config.load(args.config).database_path())
    try:
        msgs = store.query(
            thread=thread,
            transport=args.mode,
            sender=args.sender,
            group=args.group,
            since=since,
            until=until,
            status=getattr(args, "status", None),
            snr_min=getattr(args, "snr_min", None),
            limit=args.limit,
            newest_first=False,  # read like a conversation (oldest-first)
        )
        # Without a specific thread, annotate each line with its thread so a
        # cross-mode history stays legible.
        _render_messages(msgs, args.format, show_thread=not thread)
    finally:
        store.close()
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    """Search stored message bodies with optional filters (offline)."""
    from .core.store import MessageStore

    if getattr(args, "date", None):
        try:
            since = _parse_when(args.date, end=False)
            until = _parse_when(args.date, end=True)
        except ValueError:
            print("error: --date must be YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        window = _history_window(args)
        if window is None:
            return 2
        since, until = window
    store = MessageStore(Config.load(args.config).database_path())
    try:
        msgs = store.query(
            text=args.text,
            transport=args.mode,
            sender=args.sender,
            group=args.group,
            since=since,
            until=until,
            status=getattr(args, "status", None),
            snr_min=getattr(args, "snr_min", None),
            limit=args.limit,
            newest_first=True,  # most relevant = most recent first
        )
        _render_messages(msgs, args.format, show_thread=True)
    finally:
        store.close()
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """Export conversation history (offline, read-only) to json/txt/md/maildir."""
    from .core.store import MessageStore

    cfg = Config.load(args.config)
    store = MessageStore(cfg.database_path())
    try:
        thread = args.thread
        fmt = args.format
        out_path = Path(args.out) if args.out else None

        if thread:
            msgs_by_thread = [
                (thread, store.query(thread=thread, limit=100_000, newest_first=False))
            ]
        else:
            keys = [k for k, _, _ in store.threads()]
            msgs_by_thread = [
                (k, store.query(thread=k, limit=100_000, newest_first=False))
                for k in keys
            ]

        all_msgs = [m for _, ms in msgs_by_thread for m in ms]
        all_msgs.sort(key=lambda m: m.timestamp)

        if fmt == "maildir":
            if not out_path:
                print(
                    "error: --out <dir> is required for maildir format",
                    file=sys.stderr,
                )
                return 2
            _export_as_maildir(all_msgs, out_path)
            print(f"exported {len(all_msgs)} message(s) to maildir: {out_path}")
        elif fmt == "json":
            _export_write(
                json.dumps([m.to_dict() for m in all_msgs], indent=2), out_path
            )
            if out_path:
                print(f"exported {len(all_msgs)} message(s) to {out_path}")
        elif fmt == "txt":
            show_thread = not thread
            lines = [_format_message_line(m, show_thread=show_thread) for m in all_msgs]
            _export_write("\n".join(lines) + ("\n" if lines else ""), out_path)
            if out_path:
                print(f"exported {len(all_msgs)} message(s) to {out_path}")
        else:  # md
            _export_write(_export_as_md(msgs_by_thread), out_path)
            if out_path:
                print(f"exported {len(all_msgs)} message(s) to {out_path}")
    finally:
        store.close()
    return 0


def _export_write(text: str, out_path: Path | None) -> None:
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _export_as_md(msgs_by_thread: list[tuple[str, list]]) -> str:
    import io

    buf = io.StringIO()
    for thread_key, msgs in msgs_by_thread:
        buf.write(f"# {thread_key}\n\n")
        if not msgs:
            buf.write("_(no messages)_\n\n")
            buf.write("---\n\n")
            continue
        current_date: str | None = None
        for m in msgs:
            date_str = m.timestamp.strftime("%Y-%m-%d")
            if date_str != current_date:
                current_date = date_str
                buf.write(f"## {date_str}\n\n")
            ts = m.timestamp.strftime("%H:%M UTC")
            via = m.transport or "?"
            tgt = f"@{m.group}" if m.group else (m.recipient or "")
            addr = f" -> {tgt}" if tgt else ""
            buf.write(f"**{m.sender}**{addr} [{via}] {ts}\n\n")
            buf.write(f"{m.content}\n\n")
        buf.write("---\n\n")
    return buf.getvalue()


def _export_as_maildir(msgs: list, out_path: Path) -> None:
    import email.utils

    for sub in ("new", "cur", "tmp"):
        (out_path / sub).mkdir(parents=True, exist_ok=True)
    for m in msgs:
        ts_int = int(m.timestamp.timestamp())
        fname = f"{ts_int}.{m.msg_id[:16]}.radioapp"
        tgt = f"@{m.group}" if m.group else (m.recipient or "(broadcast)")
        subject_src = m.metadata.get("subject") or m.content
        subject = subject_src[:72].replace("\n", " ").replace("\r", "")
        date_str = email.utils.format_datetime(m.timestamp)
        text = (
            f"From: {m.sender}\n"
            f"To: {tgt}\n"
            f"Date: {date_str}\n"
            f"Subject: {subject}\n"
            f"Message-ID: <{m.msg_id}@radioapp>\n"
            f"X-RadioApp-Transport: {m.transport or ''}\n"
            f"X-RadioApp-Thread: {m.thread_key}\n"
            f"X-RadioApp-Status: {m.status.value}\n"
            f"\n"
            f"{m.content}\n"
        )
        (out_path / "new" / fname).write_text(text, encoding="utf-8")


def _cmd_templates(args: argparse.Namespace) -> int:
    """List or send canned message templates from [templates] in config."""
    from .core.templates import Templates

    cfg = Config.load(args.config)
    tmpls = Templates.from_config(cfg)

    if args.action == "list" or not args.name:
        items = tmpls.all()
        if not items:
            print(
                "(no templates configured — add [templates] to your config.toml)"
            )
            return 0
        for name, text in sorted(items.items()):
            print(f"  {name:<16} {text}")
        return 0

    text = tmpls.get(args.name)
    if text is None:
        names = tmpls.names()
        hint = ", ".join(names) if names else "(none configured)"
        print(f"error: template '{args.name}' not found. Available: {hint}", file=sys.stderr)
        return 1

    if not args.to and not args.group:
        print("error: --to or --group is required for 'send'", file=sys.stderr)
        return 2

    name = cfg.display_name
    if args.group:
        msg = UnifiedMessage.to_group(name, args.group, text)
    else:
        msg = UnifiedMessage.direct(name, args.to, text)

    async def run(app: App) -> int:
        ok = await app.router.send(msg, force_transport=args.transport)
        print(f"{'sent' if ok else 'FAILED'}: {msg.msg_id} via {msg.transport or '-'}")
        return 0 if ok else 1

    return _run(_with_app(args.config, run))


def _cmd_position(args: argparse.Namespace) -> int:
    """Show current position from GPS or config; optionally send a beacon."""
    from .core.position import GPSReader, position_from_config

    cfg = Config.load(args.config)
    pos = None

    if args.gps:
        gpsd_host = cfg.position.get("gpsd_host", "127.0.0.1")
        gpsd_port = int(cfg.position.get("gpsd_port", 2947))
        print(f"Querying gpsd at {gpsd_host}:{gpsd_port}...")
        reader = GPSReader(host=gpsd_host, port=gpsd_port)
        pos = reader.read()
        if pos is None:
            print(
                "No GPS fix available. Check that gpsd is running and the "
                "receiver has a signal.",
                file=sys.stderr,
            )
            return 1
    else:
        pos = position_from_config(cfg)
        if pos is None:
            print(
                "No position configured. Add to config.toml:\n"
                "  [position]\n"
                "  lat = 42.3601\n"
                "  lon = -71.0589\n"
                "Or use --gps to query gpsd.",
                file=sys.stderr,
            )
            return 1

    print(f"position : {pos.lat:+.6f}°  {pos.lon:+.6f}°")
    print(f"grid     : {pos.grid}")
    if pos.alt_m is not None:
        print(f"altitude : {pos.alt_m:.0f} m")
    print(f"source   : {pos.source}")

    if args.beacon:
        async def run(app: App) -> int:
            sent = 0
            for t in app.transports:
                if hasattr(t, "send_position_beacon") and t.running:
                    ok = await t.send_position_beacon(pos)
                    if ok:
                        print(f"  beacon sent via {t.name}")
                        sent += 1
            if sent == 0:
                print("No running transports support position beacons.")
            return 0

        return _run(_with_app(args.config, run))

    return 0


def _cmd_time(args: argparse.Namespace) -> int:
    """Show current UTC time and best available clock offset."""
    from .core.timesource import TimeConsensus, TimeSourceKind

    print("Checking time sources (GPS → local NTP → internet NTP)...")
    tc = TimeConsensus(timeout=3.0)
    reading = tc.best_reading()
    ts = reading.utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"time   : {ts}")
    if reading.offset_ms is not None:
        off = reading.offset_ms
        warn = ""
        if abs(off) > 1000:
            warn = "  ** CLOCK SEVERELY OUT OF SYNC — HF modes may not work **"
        elif abs(off) > 100:
            warn = "  (offset > 100 ms — consider NTP sync)"
        src_label = {
            TimeSourceKind.GPS: "GPS (gpsd)",
            TimeSourceKind.LOCAL_NTP: "local NTP daemon (chrony/ntpd)",
            TimeSourceKind.NTP: "internet NTP (pool.ntp.org)",
        }.get(reading.source, reading.source.value)
        print(f"offset : {off:+.1f} ms{warn}")
        print(f"source : {src_label}")
    elif reading.error:
        print(f"source : system clock ({reading.error})")
    else:
        print("source : system")
    return 0


def _cmd_bands(args: argparse.Namespace) -> int:
    """Print the offline band-plan / EmComm frequency reference."""
    from .core.bandplan import format_mhz, lookup

    entries = lookup(
        band=getattr(args, "band", None),
        mode=getattr(args, "mode", None),
        region=getattr(args, "region", "US"),
        transport=getattr(args, "transport", None),
    )
    if not entries:
        print("No entries match those filters.")
        return 0
    current_band = None
    for e in entries:
        if e.band != current_band:
            if current_band is not None:
                print()
            print(f"  [{e.band}]")
            current_band = e.band
        freq = format_mhz(e.freq_khz)
        notes = f"  {e.notes}" if e.notes else ""
        tp = f"  [{e.transport}]" if e.transport else ""
        print(f"    {freq:<14} {e.mode:<8} {e.region:<6}{tp}{notes}")
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    """Manage scheduled / windowed message sends."""
    from datetime import UTC, datetime, timedelta

    from .config import Config
    from .core.message import UnifiedMessage
    from .core.store import MessageStore

    cfg = Config.load(args.config)
    store = MessageStore(cfg.database_path())

    action = getattr(args, "sched_action", None) or "list"

    try:
        if action == "list":
            entries = store.schedule_pending()
            if not entries:
                print("No pending scheduled messages.")
                return 0
            for e in entries:
                ts = e.fire_at.strftime("%Y-%m-%d %H:%M UTC")
                tp = f" [{e.transport}]" if e.transport else ""
                print(f"  {e.id[:8]}  {ts}{tp}  {e.message.content[:60]!r}")
            return 0

        if action == "cancel":
            ok = store.schedule_cancel(args.id)
            print("Cancelled." if ok else f"No pending message with id '{args.id}'.")
            return 0 if ok else 1

        if action == "add":
            now = datetime.now(UTC)
            if getattr(args, "at", None):
                raw = args.at.strip()
                if "T" in raw or (len(raw) > 5 and "-" in raw):
                    try:
                        fire_at = datetime.fromisoformat(raw).astimezone(UTC)
                    except ValueError:
                        print("error: --at must be HH:MM or ISO datetime", file=sys.stderr)
                        return 2
                else:
                    try:
                        hh, mm = raw.split(":")
                        fire_at = now.replace(
                            hour=int(hh), minute=int(mm), second=0, microsecond=0
                        )
                        if fire_at <= now:
                            fire_at += timedelta(days=1)
                    except (ValueError, AttributeError):
                        print("error: --at time must be HH:MM", file=sys.stderr)
                        return 2
            elif getattr(args, "delay", None):
                raw = args.delay.strip().lower()
                try:
                    minutes = 0
                    if "h" in raw and "m" in raw:
                        h_part, rest = raw.split("h")
                        minutes = int(h_part) * 60 + int(rest.rstrip("m"))
                    elif "h" in raw:
                        minutes = int(raw.rstrip("h")) * 60
                    else:
                        minutes = int(raw.rstrip("m"))
                    fire_at = now + timedelta(minutes=minutes)
                except ValueError:
                    print("error: --delay must be like 30m, 1h, 90m", file=sys.stderr)
                    return 2
            else:
                print("error: --at or --delay is required", file=sys.stderr)
                return 2

            name = cfg.display_name
            if getattr(args, "group", None):
                msg = UnifiedMessage.to_group(name, args.group, args.message)
            else:
                msg = UnifiedMessage.direct(name, args.to, args.message)

            force_tp = getattr(args, "force_transport", None)
            store.schedule_add(msg, fire_at, transport=force_tp)
            ts = fire_at.strftime("%Y-%m-%d %H:%M UTC")
            print(f"Scheduled {msg.msg_id[:8]} for {ts}")
            return 0

    finally:
        store.close()

    print(f"Unknown action '{action}'. Use: add | list | cancel", file=sys.stderr)
    return 2


def _cmd_roster(args: argparse.Namespace) -> int:
    """Show recently-heard callsigns (presence roster)."""
    from datetime import UTC, datetime, timedelta

    from .config import Config
    from .core.roster import get_roster
    from .core.store import MessageStore

    cfg = Config.load(args.config)
    store = MessageStore(cfg.database_path())

    raw = getattr(args, "since", "24h").lower().rstrip("h")
    try:
        hours = int(raw)
    except ValueError:
        print("error: --since must be like 24h, 48h", file=sys.stderr)
        store.close()
        return 2

    since = datetime.now(UTC) - timedelta(hours=hours)
    entries = get_roster(
        store,
        since=since,
        transport=getattr(args, "transport", None),
        limit=args.limit,
    )
    store.close()

    if not entries:
        print(f"No stations heard in the last {hours}h.")
        return 0

    print(
        f"  {'CALLSIGN':<16} {'TRANSPORT':<12} {'LAST SEEN':<20} "
        f"{'SNR':>6}  {'MSG':>4}  PREVIEW"
    )
    print("  " + "-" * 75)
    for e in entries:
        ts = e.last_seen.strftime("%m-%d %H:%M UTC")
        snr = f"{e.last_snr:+.0f}" if e.last_snr is not None else "  —"
        preview = e.last_content[:30]
        print(
            f"  {e.callsign:<16} {e.transport:<12} {ts:<20} "
            f"{snr:>6}  {e.message_count:>4}  {preview!r}"
        )
    return 0


def _cmd_listen(args: argparse.Namespace) -> int:
    async def run(app: App) -> int:
        print("Listening for messages (Ctrl-C to stop)...")

        def on_msg(msg: UnifiedMessage, action) -> None:
            kind = msg.metadata.get("kind")
            # Telemetry (RNS announces, JS8 traffic beacons) isn't a
            # conversation - surface it as a compact heartbeat, not a message.
            if kind in ("announce", "traffic"):
                ts = msg.timestamp.strftime("%H:%M:%S")
                label = "ANN" if kind == "announce" else "RX "
                detail = msg.metadata.get("event") or msg.sender or ""
                print(f"{ts} [{msg.transport}] {label} {detail}")
                return
            if args.group and msg.group != args.group.lstrip("@"):
                return
            ts = msg.timestamp.strftime("%H:%M:%S")
            via = f"[{msg.transport}]"
            tgt = f"@{msg.group}" if msg.group else (msg.recipient or "")
            print(f"{ts} {via} {msg.sender} -> {tgt}: {msg.content}")

        app.router.add_ui_callback(on_msg)
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0

    return _run(_with_app(args.config, run))


def _cmd_groups(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    from .core.groups import GroupRegistry

    reg = GroupRegistry.from_config(cfg)
    groups = reg.all()
    if not groups:
        print("(no groups configured — add one with 'radioapp group <name> add ...')")
        return 0
    for g in groups:
        subscribed = "*" if reg.is_subscribed(g.name) else " "
        where = ", ".join(g.transports) or "-"
        extra = []
        if g.members:
            extra.append(f"{len(g.members)} member(s)")
        if g.tags:
            extra.append(f"tags: {', '.join('@' + t for t in g.tags)}")
        suffix = f"  [{'; '.join(extra)}]" if extra else ""
        print(
            f"[{subscribed}] {g.tag:<12} {g.display_name:<20} via {where}{suffix}"
        )
    return 0


def _print_group(reg, name: str) -> int:
    g = reg.get(name)
    if g is None:
        print(f"no such group @{name.lstrip('@')}", file=sys.stderr)
        return 1
    print(f"@{g.name}  ({g.display_name})")
    print(f"  outbound transports: {', '.join(g.transports) or '-'}")
    print("  members (incoming):")
    if g.members:
        for m in g.members:
            who = m.transport or "any"
            print(f"    - {m.identifier}   [{who}]")
    else:
        print("    (none)")
    print(
        "  tags (incoming): "
        + (", ".join("@" + t for t in g.tags) if g.tags else "(none)")
    )
    return 0


def _cmd_group(args: argparse.Namespace) -> int:
    """Manage a group's cross-mode incoming membership (members + tags)."""
    cfg = Config.load(args.config)
    from .core.groups import GroupRegistry

    reg = GroupRegistry.from_config(cfg)
    name = args.name.lstrip("@")
    action = args.action

    if action == "show":
        return _print_group(reg, name)
    if action == "delete":
        if reg.remove_group(name):
            reg.save(cfg)
            print(f"removed group @{name}")
            return 0
        print(f"no such group @{name}", file=sys.stderr)
        return 1

    if not args.value:
        need = "a member spec 'transport:identifier'" if action in (
            "add",
            "remove",
        ) else "a tag"
        print(f"error: {action} needs {need}", file=sys.stderr)
        return 2

    if action == "add":
        m = reg.add_member(name, args.value)
        reg.save(cfg)
        print(f"@{name}: added member {m.spec}")
    elif action == "remove":
        if not reg.remove_member(name, args.value):
            print(f"@{name}: '{args.value}' is not a member", file=sys.stderr)
            return 1
        reg.save(cfg)
        print(f"@{name}: removed member {args.value}")
    elif action == "tag":
        t = reg.add_tag(name, args.value)
        reg.save(cfg)
        print(f"@{name}: now claims tag @{t}")
    elif action == "untag":
        if not reg.remove_tag(name, args.value):
            print(f"@{name}: no such tag '{args.value}'", file=sys.stderr)
            return 1
        reg.save(cfg)
        print(f"@{name}: removed tag")
    return 0


def _cmd_sub(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    subs = list(cfg.subscriptions.get("groups", []))
    if args.action == "list":
        for s in subs:
            print(f"@{s.lstrip('@')}")
        return 0
    if not args.group:
        print("error: a group name is required", file=sys.stderr)
        return 2
    g = args.group.lstrip("@")
    if args.action == "add" and g not in subs:
        subs.append(g)
    elif args.action == "remove" and g in subs:
        subs.remove(g)
    cfg.set("subscriptions", "groups", subs)
    cfg.save()
    print(f"subscriptions: {', '.join('@' + s for s in subs) or '(none)'}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    async def run(app: App) -> int:
        print(f"display name : {app.config.display_name}")
        st = app.station
        ident = st.callsign or "(not set - run 'radioapp setup')"
        print(f"callsign     : {ident}")
        print(f"grid square  : {st.grid_square or '-'}")
        print(f"default mode : {app.config.default_mode}")
        enc = "ALLOWED" if app.config.allow_encrypted_on_hf() else "blocked"
        print(f"encrypt-on-HF: {enc}")
        print(f"database     : {app.config.database_path()}")
        print("transports   :")
        from .transports.base import ReachabilityStatus

        for t in app.transports:
            # `running` is just "the adapter loaded"; it does NOT mean the
            # backing service (Pat, the JS8Call API, rnsd, ...) is actually
            # reachable. Probe the control endpoint too so this agrees with the
            # Health panel instead of always reporting UP.
            state = "UP" if t.running else "down"
            try:
                reach = await t.check_reachable()
            except Exception:  # noqa: BLE001 - any failure means "down"
                reach = ReachabilityStatus.DOWN
            reach_txt = {
                ReachabilityStatus.OK: "reachable",
                ReachabilityStatus.DOWN: "UNREACHABLE",
                ReachabilityStatus.NOT_APPLICABLE: "n/a",
            }.get(reach, str(reach.value))
            caps = t.capabilities()
            # Show the *actual* identity this transport uses, not just its kind.
            # Callsign-carrying media (HF: js8call/winlink/mercury) identify with
            # a callsign — from the transport's own config if set, else the
            # station callsign. Anonymous media (Reticulum/MeshCore) expose a
            # non-identifying address via local_identity().
            if caps.carries_operator_identity:
                cfg = getattr(t, "config", None)
                own = ""
                if isinstance(cfg, dict):
                    own = str(cfg.get("callsign", "") or "").strip()
                callsign = own or app.station.callsign
                ident = f"callsign={callsign}" if callsign else "callsign=(unset)"
            else:
                anon = None
                getter = getattr(t, "local_identity", None)
                if callable(getter):
                    try:
                        anon = getter()
                    except Exception:  # noqa: BLE001
                        anon = None
                ident = f"anon={anon}" if anon else "anon"
            print(
                f"  - {t.name:<12} {state:<5} {reach_txt:<11} {ident}"
            )
        return 0

    return _run(_with_app(args.config, run))


def _cmd_transports(args: argparse.Namespace) -> int:
    from .transports import TRANSPORT_REGISTRY, discover_plugin_transports

    discover_plugin_transports()
    for name, cls in sorted(TRANSPORT_REGISTRY.items()):
        caps = cls.__new__(cls).capabilities()  # capabilities are static here
        print(
            f"{name:<12} mtu={caps.max_message_size:<8} "
            f"enc={'Y' if caps.supports_encryption else 'N'} "
            f"groups={'Y' if caps.supports_groups else 'N'} "
            f"realtime={'Y' if caps.is_realtime else 'N'} "
            f"latency~{caps.typical_latency_s}s"
        )
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.action == "path":
        print(path)
        return 0
    if args.action == "show":
        if not path.exists():
            print(f"(no config at {path}; run 'radioapp config init')")
            return 1
        print(path.read_text())
        return 0
    if args.action == "init":
        if path.exists():
            print(f"config already exists at {path}")
            return 0
        example = Path(__file__).resolve().parents[2] / "config.example.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if example.exists():
            shutil.copyfile(example, path)
        else:  # fall back to writing current defaults
            Config.load(path).save()
        print(f"wrote starter config to {path}")
        return 0
    if args.action == "get":
        if not args.key:
            print("usage: radioapp config get <section.key>", file=sys.stderr)
            return 2
        cfg = Config.load(args.config)
        found, value = _config_lookup(cfg.data, args.key)
        if not found:
            print(f"{args.key} is not set", file=sys.stderr)
            return 1
        print(_format_config_value(value))
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            print(
                "usage: radioapp config set <section.key> <value>",
                file=sys.stderr,
            )
            return 2
        if "." not in args.key:
            print(
                "error: key must be dotted, e.g. 'storage.download_dir'",
                file=sys.stderr,
            )
            return 2
        try:
            value = _parse_config_value(
                args.value, json_mode=args.json, string_mode=args.string
            )
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON value: {exc}", file=sys.stderr)
            return 2
        cfg = Config.load(args.config)
        _config_assign(cfg.data, args.key, value)
        cfg.save()
        print(f"set {args.key} = {_format_config_value(value)}")
        print(f"  ({cfg.path})")
        return 0
    return 1


def _config_lookup(data: dict, dotted: str) -> tuple[bool, object]:
    """Resolve a dotted ``section.key[...]`` path; (found, value)."""
    cur: object = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _config_assign(data: dict, dotted: str, value: object) -> None:
    """Set a dotted ``section.key[...]`` path, creating intermediate tables."""
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _parse_config_value(
    raw: str, *, json_mode: bool = False, string_mode: bool = False
) -> object:
    """Convert a CLI string into a typed config value.

    ``--string`` forces a literal string; ``--json`` parses JSON (lists/objects).
    Otherwise it's smart-typed: ``true``/``false`` -> bool, an integer or float
    where the text parses cleanly, else the string verbatim.
    """
    if string_mode:
        return raw
    if json_mode:
        return json.loads(raw)
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _format_config_value(value: object) -> str:
    """Render a config value for display (TOML-ish booleans, JSON containers)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _cmd_tui(args: argparse.Namespace) -> int:
    from .ui import run_tui

    run_tui(args.config)
    return 0


def _cmd_db(args: argparse.Namespace) -> int:
    """Database maintenance: stats, vacuum, and pruning (history + page cache)."""
    from .core.nomad_cache import NomadPageCache
    from .core.store import MessageStore
    from .core.syshealth import format_bytes

    cfg = Config.load(args.config)
    db_path = cfg.database_path()
    store = MessageStore(db_path)
    cache = NomadPageCache(db_path)
    try:
        if args.action == "stats":
            _print_db_stats(cfg, store, cache)
            return 0
        if args.action == "vacuum":
            freed = store.vacuum()
            print(f"VACUUM complete; reclaimed {format_bytes(freed)}.")
            return 0
        if args.action in ("prune", "cache-prune"):
            days = args.days or int(
                cfg.general.get("history_retention_days", 0) or 0
            )
            if days <= 0:
                print(
                    "error: specify --days N (no retention configured)",
                    file=sys.stderr,
                )
                return 2
            if args.action == "prune":
                n = store.purge_older_than(days)
                print(f"pruned {n} message(s) older than {days} day(s).")
                if n:
                    print("  tip: run 'radioapp db vacuum' to reclaim disk space.")
            else:
                n = cache.prune(days)
                print(f"pruned {n} cached page(s) older than {days} day(s).")
            return 0
        if args.action == "cache-clear":
            n = cache.clear()
            print(f"cleared {n} cached NomadNet page(s).")
            return 0
    finally:
        store.close()
        cache.close()
    return 1


def _print_db_stats(cfg: Config, store, cache) -> None:
    from .core.syshealth import collect, format_bytes

    db_path = cfg.database_path()
    s = store.stats()
    c = cache.stats()
    sys_h = collect(str(db_path))
    print(f"database : {db_path}")
    print(f"  size       : {format_bytes(s['size_bytes'])}")
    span = f"   ({s['oldest'][:10]} … {s['newest'][:10]})" if s["oldest"] else ""
    print(f"  messages   : {s['messages']} in {s['threads']} thread(s){span}")
    print(
        f"  page cache : {c['pages']} page(s), "
        f"{format_bytes(c['content_bytes'])} of content"
    )
    retention = int(cfg.general.get("history_retention_days", 0) or 0)
    print(
        "  retention  : "
        + (f"{retention} days" if retention > 0 else "keep forever (no pruning)")
    )
    if sys_h.disk_free is not None:
        print(
            f"  disk free  : {format_bytes(sys_h.disk_free)} "
            f"of {format_bytes(sys_h.disk_total)}"
        )


def _cmd_backup(args: argparse.Namespace) -> int:
    """Back up the config file + database (page cache included) to a .tar.gz."""
    from .core.backup import create_backup
    from .core.syshealth import format_bytes

    cfg = Config.load(args.config)
    res = create_backup(cfg.path, cfg.database_path(), args.out)
    print(f"backup written: {res.path}  ({format_bytes(res.size_bytes)})")
    print(
        f"  config: {'yes' if res.config_included else 'no'}   "
        f"database: {'yes' if res.db_included else 'no'}"
    )
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    """Restore the config + database from a backup archive (with confirmation)."""
    from .core.backup import read_manifest, restore_backup

    cfg = Config.load(args.config)
    manifest = read_manifest(args.archive)
    if manifest:
        print(
            f"archive created: {manifest.get('created', '?')}  "
            f"(config={manifest.get('config')}, database={manifest.get('database')})"
        )
    if not args.yes:
        print(f"This will OVERWRITE:\n  {cfg.path}\n  {cfg.database_path()}")
        if not _ask_bool("Proceed with restore?", False):
            print("aborted.")
            return 1
    res = restore_backup(args.archive, cfg.path, cfg.database_path())
    print(f"restored — config: {res.config_restored}, database: {res.db_restored}")
    for sc in res.safety_copies:
        print(f"  safety copy of previous file: {sc}")
    print("Restart Radio_App to load the restored data.")
    return 0


def _ask(prompt: str, default: str = "") -> str:
    """Prompt with an optional default shown in brackets; Enter keeps it."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _ask_bool(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    answer = input(f"  {prompt} [{d}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "true", "1")


def _probe_pat(url: str) -> bool:
    """Best-effort check that a Pat HTTP API answers ``/api/status``.

    Used only to give the operator a friendly "Pat is/ isn't reachable" hint
    during setup; Winlink works regardless (Pat can be started later).
    """
    import urllib.request

    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/status", method="GET"
        )
        with urllib.request.urlopen(req, timeout=2.0):  # noqa: S310
            return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable now"
        return False


def _cmd_setup(args: argparse.Namespace) -> int:
    """Interactive wizard: capture all user settings into the one config file.

    Radio_App is a single pane of glass over disparate transports, so this asks
    once for everything (identity, Reticulum/rnsd, JS8Call, MeshCore, Winlink)
    and writes it to the single TOML config. The radio is driven by the transport
    app (JS8Call), so there are no rig/CAT questions here.
    """
    from .core.js8call_query import query_groups, query_station
    from .core.station import Station

    cfg = Config.load(args.config)
    print(f"\nRadio_App setup  ->  {cfg.path}")
    print("Press Enter to keep the current/[]-shown value.\n")

    # -- general -------------------------------------------------------------
    print("General")
    display_name = _ask("Display name", cfg.display_name)
    # History retention: messages and cached pages accumulate in the SQLite DB
    # over time. 0 keeps everything forever (simplest, but the file grows without
    # bound); a positive number auto-prunes anything older on each startup.
    print(
        "\n  Message history is kept in a local database that grows over time.\n"
        "  Set how many days to keep (0 = keep everything forever; you can\n"
        "  always prune later with 'radioapp db prune')."
    )
    retention_default = str(int(cfg.general.get("history_retention_days", 0) or 0))
    retention_raw = _ask("History retention in days", retention_default)
    try:
        retention_days = max(0, int(retention_raw))
    except ValueError:
        print("  (not a number; keeping everything — 0 days)")
        retention_days = 0
    # One folder where ALL downloaded content (attachments etc.) is saved, used
    # by every mode. Blank keeps the default app data dir.
    print(
        "\n  Downloaded files (attachments, saved content) from every mode are\n"
        "  saved in one folder. Leave blank for the default app data directory."
    )
    download_dir = _ask(
        "Download folder", str(cfg.storage.get("download_dir", "") or "")
    )

    # -- Reticulum -----------------------------------------------------------
    print("\nReticulum (encrypted internet / LoRa / serial via rnsd)")
    ret = dict(cfg.transports.get("reticulum", {}))
    ret_enabled = _ask_bool("Enable Reticulum?", ret.get("enabled", True))
    if ret_enabled:
        ret["config_path"] = _ask(
            "rnsd config dir (blank = ~/.reticulum)", ret.get("config_path", "")
        )
        ret["shared_instance"] = _ask_bool(
            "Connect to a running rnsd (shared instance)?",
            ret.get("shared_instance", True),
        )
        # Public LXMF name shown to peers when you message them. Distinct from
        # the general display name; Reticulum is anonymous by default, so this is
        # blank unless set. NEVER put a callsign here (this medium is anonymous).
        ret["display_name"] = _ask(
            "Public Reticulum name shown to peers (blank = anonymous; "
            "do NOT use your callsign)",
            ret.get("display_name", ""),
        )
    ret["enabled"] = ret_enabled

    # -- JS8Call -------------------------------------------------------------
    print("\nJS8Call (HF weak-signal; JS8Call drives the radio)")
    js8 = dict(cfg.transports.get("js8call", {}))
    js8_enabled = _ask_bool("Enable JS8Call?", js8.get("enabled", False))
    js8_info = None
    import_groups = False
    if js8_enabled:
        js8["host"] = _ask("JS8Call API host", js8.get("host", "127.0.0.1"))
        js8["port"] = int(_ask("JS8Call API port", str(js8.get("port", 2442))))
        # Ask JS8Call for the operator identity so the user need not retype it.
        print("  querying JS8Call for callsign/grid...")
        js8_info = query_station(js8["host"], js8["port"])
        if js8_info.any_found:
            print(
                f"  JS8Call reports: callsign={js8_info.callsign or '-'} "
                f"grid={js8_info.grid or '-'}"
            )
        else:
            print("  (JS8Call did not answer; you can enter these manually)")
        # Offer to import the operator's JS8Call @groups as favorites now.
        import_groups = _ask_bool(
            "Import your JS8Call groups as favorites now?", True
        )
    js8["enabled"] = js8_enabled

    # -- MeshCore ------------------------------------------------------------
    print("\nMeshCore (license-free ISM LoRa mesh; USB serial and/or TCP companion)")
    mc = dict(cfg.transports.get("meshcore", {}))
    mc_enabled = _ask_bool("Enable MeshCore?", mc.get("enabled", False))
    if mc_enabled:
        # The companion can be reached two ways; ask which (one or both fields
        # get filled either way, so switching later is just a config edit).
        conn = _ask(
            "Connection (serial=USB, tcp=network)",
            mc.get("connection", "serial"),
        ).strip().lower()
        if conn not in ("serial", "tcp"):
            print("  (unrecognized; defaulting to 'serial')")
            conn = "serial"
        mc["connection"] = conn
        if conn == "serial":
            mc["port"] = _ask(
                "USB serial device", mc.get("port", "/dev/ttyACM0")
            )
            mc["baud"] = int(_ask("Serial baud rate", str(mc.get("baud", 115200))))
        else:  # tcp
            mc["host"] = _ask("Companion TCP host", mc.get("host", "127.0.0.1"))
            mc["tcp_port"] = int(
                _ask("Companion TCP port", str(mc.get("tcp_port", 5000)))
            )
    mc["enabled"] = mc_enabled

    # -- station identity (prefilled from JS8Call when available) ------------
    print("\nStation identity (used on HF; Reticulum stays anonymous)")
    s = cfg.station
    call_default = (js8_info.callsign if js8_info else "") or s.get("callsign", "")
    grid_default = (js8_info.grid if js8_info else "") or s.get("grid_square", "")
    callsign = _ask("Callsign (e.g. N0CALL)", call_default)
    grid = _ask("Grid square (e.g. FN31pr)", grid_default)
    station = Station(callsign=callsign, grid_square=grid)
    problems = station.validate()
    if problems:
        print("\nPlease fix:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    # -- Winlink (store-and-forward email via a separately-installed Pat) -----
    print("\nWinlink (email over radio/internet; a separately-installed Pat does"
          " the work)")
    wl = dict(cfg.transports.get("winlink", {}))
    wl_enabled = _ask_bool("Enable Winlink?", wl.get("enabled", False))
    if wl_enabled:
        wl["pat_url"] = _ask(
            "Pat HTTP API URL", wl.get("pat_url", "http://127.0.0.1:8080")
        )
        wl["callsign"] = _ask(
            "Winlink callsign", wl.get("callsign", "") or station.callsign
        )
        # Sensible out-of-the-box defaults: auto-fallback telnet -> Mercury/VARA
        # -> ARDOP, queue to the outbox, poll the inbox every minute. Only set
        # what's missing so an existing hand-tuned block is preserved.
        wl.setdefault("connect", "auto")
        wl.setdefault("connect_order", ["telnet", "varahf", "ardop"])
        wl.setdefault("poll_interval", 60)
        wl.setdefault("auto_connect", False)
        # The Winlink password and the modem both live INSIDE Pat, not here.
        print("  checking whether Pat is reachable...")
        if _probe_pat(wl["pat_url"]):
            print(f"  Pat answered at {wl['pat_url']} \u2713")
        else:
            print(
                f"  (no answer at {wl['pat_url']} yet - start it later with "
                "'pat http')"
            )
        print(
            "  note: configure your Winlink account password INSIDE Pat "
            "(Radio_App never stores it)."
        )
    wl["enabled"] = wl_enabled

    # -- compliance (advanced) ----------------------------------------------
    allow_enc = _ask_bool(
        "\nAllow encrypted payloads on HF? (advanced; usually NO)",
        cfg.allow_encrypted_on_hf(),
    )

    # -- write it all to the single config file ------------------------------
    cfg.set("general", "display_name", display_name)
    cfg.set("general", "history_retention_days", retention_days)
    cfg.set("storage", "download_dir", download_dir)
    cfg.set("station", "callsign", station.callsign)
    cfg.set("station", "grid_square", station.grid_square)
    cfg.set("compliance", "allow_encrypted_on_hf", allow_enc)
    transports = dict(cfg.transports)
    transports["reticulum"] = ret
    transports["js8call"] = js8
    transports["meshcore"] = mc
    transports["winlink"] = wl
    cfg.data["transports"] = transports
    cfg.save()

    print(f"\nSaved configuration to {cfg.path}")
    print(f"  station   : {station.callsign or '(none)'}  {station.grid_square}")
    enabled = [
        n for n in ("reticulum", "js8call", "meshcore", "winlink")
        if transports[n].get("enabled")
    ]
    print(f"  transports: {', '.join(enabled) or '(none enabled)'}")

    # Import JS8Call groups as favorites if the operator asked us to.
    if import_groups:
        from .core.favorites import Favorites

        print("  querying JS8Call for your groups...")
        groups = query_groups(js8["host"], js8["port"])
        if groups:
            favs = Favorites.from_config(cfg)
            added = sum(
                1 for g in groups if not favs.is_favorite(f"@{g}")
            )
            for g in groups:
                favs.add(f"@{g}", kind="group")
            favs.save(cfg)
            joined = ", ".join(f"@{g}" for g in groups)
            print(
                f"  favorites : imported {len(groups)} group(s) "
                f"({added} new): {joined}"
            )
        else:
            print(
                "  favorites : no JS8Call groups found "
                "(is JS8Call running with the API enabled?)"
            )

    print("  Run 'radioapp status' to check, or 'radioapp tui' to start.")
    return 0




def _cmd_reticulum(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    ret_cfg = cfg.transports.get("reticulum", {})

    if args.action == "setup-rnode":
        from .transports.reticulum_transport import ensure_rnode_interface

        raw_dir = ret_cfg.get("config_path") or None
        configdir = (
            str(Path(raw_dir).expanduser()) if raw_dir else None
        )
        print("Configure the RNode LoRa interface (Enter keeps the default).\n")
        rnode = dict(ret_cfg.get("rnode", {}))
        port = input(f"  Serial port  [{rnode.get('port', '/dev/ttyUSB0')}]: ").strip()
        freq = input(f"  Frequency Hz [{rnode.get('frequency', 915000000)}]: ").strip()
        bw = input(f"  Bandwidth Hz [{rnode.get('bandwidth', 125000)}]: ").strip()
        sf = input(f"  Spreading f. [{rnode.get('spreadingfactor', 8)}]: ").strip()
        cr = input(f"  Coding rate  [{rnode.get('codingrate', 5)}]: ").strip()
        tx = input(f"  TX power dBm [{rnode.get('txpower', 7)}]: ").strip()
        if port:
            rnode["port"] = port
        if freq:
            rnode["frequency"] = int(freq)
        if bw:
            rnode["bandwidth"] = int(bw)
        if sf:
            rnode["spreadingfactor"] = int(sf)
        if cr:
            rnode["codingrate"] = int(cr)
        if tx:
            rnode["txpower"] = int(tx)
        cfg.set(
            "transports",
            "reticulum",
            {**ret_cfg, "rnode": rnode, "manage_interface": True},
        )
        cfg.save()
        path = ensure_rnode_interface(configdir, rnode)
        if path:
            print(f"\nWrote RNode interface to {path}")
        else:
            print(
                "\nA Reticulum config already exists; not overwriting it.\n"
                "Edit ~/.reticulum/config by hand to add/adjust the RNode interface."
            )
        return 0

    async def run(app: App) -> int:
        ret = next((t for t in app.transports if t.name == "reticulum"), None)
        if ret is None or not ret.running:
            print("Reticulum transport is not running (enable it / install extras).")
            return 1
        print(f"local LXMF address : {ret.local_identity()}")
        if args.action == "status":
            print(f"running            : {ret.running}")
            # Surface the diagnostics that explain an empty Monitor: whether we
            # attached to rnsd (shared instance) and how many RNS interfaces we
            # actually have. Zero interfaces + not-shared == we hear nothing.
            shared_cfg = bool(ret_cfg.get("shared_instance", False))
            print(f"shared_instance cfg: {shared_cfg}")
            try:
                import RNS  # type: ignore

                rns = getattr(ret, "_reticulum", None)
                connected_shared = bool(
                    getattr(rns, "is_connected_to_shared_instance", False)
                )
                ifaces = list(getattr(RNS.Transport, "interfaces", []) or [])
                print(f"connected to rnsd  : {connected_shared}")
                print(f"RNS interfaces     : {len(ifaces)}")
                for iface in ifaces:
                    print(f"  - {iface}")
                if not connected_shared and not ifaces:
                    print(
                        "\nNo interfaces and not attached to rnsd: the Monitor "
                        "will stay empty. If rnsd is running, set "
                        "[transports.reticulum] shared_instance = true in your "
                        "config."
                    )
                # Path table size hints at how many peers we've heard.
                try:
                    paths = len(getattr(RNS.Transport, "destination_table", {}) or {})
                    print(f"known paths        : {paths}")
                except Exception:  # noqa: BLE001
                    pass
            except ModuleNotFoundError:
                print("(RNS not importable; cannot show transport internals)")
        return 0

    return _run(_with_app(args.config, run))


def _cmd_winlink(args: argparse.Namespace) -> int:
    """Winlink/Pat utilities: check reachability, run a session, list gateways.

    The radio session, modem and the Winlink account password all live inside
    Pat (a separately-installed program). This command only talks to Pat's HTTP
    API; it never sees or stores your Winlink password.
    """
    async def run(app: App) -> int:
        t = next((x for x in app.transports if x.name == "winlink"), None)
        if t is None:
            print(
                "Winlink transport is not enabled. Add a [transports.winlink] "
                "block (run 'radioapp setup')."
            )
            return 1

        pat_url = str(t.config.get("pat_url", ""))
        if args.action == "status":
            reachable = _probe_pat(pat_url)
            summary = t.connect_summary() if hasattr(t, "connect_summary") else "?"
            print(f"pat url          : {pat_url}")
            print(f"pat reachable    : {'yes' if reachable else 'no'}")
            print(f"callsign         : {t.config.get('callsign', '') or '(unset)'}")
            print(f"connect method   : {summary}")
            gw = t.config.get("gateway", "") or "(default CMS)"
            print(f"gateway          : {gw}")
            for warning in (
                t.validate_config() if hasattr(t, "validate_config") else []
            ):
                print(f"config warning   : {warning}")
            if not reachable:
                print(
                    "\nPat is not answering. Start it with 'pat http' and make "
                    "sure pat_url points at it."
                )
            print(
                "\nCredentials: your Winlink password is configured INSIDE Pat "
                "(secure-login); Radio_App never stores it."
            )
            return 0 if reachable else 1

        if args.action == "gateways":
            if not hasattr(t, "list_gateways"):
                print("This transport build cannot list gateways.")
                return 1
            try:
                gateways = await t.list_gateways()
            except Exception as exc:  # noqa: BLE001
                print(f"Gateway list failed: {exc}")
                return 1
            if not gateways:
                print("No gateways returned (telnet mode, or none cached/in range).")
                return 0
            print(f"Nearby RMS gateways ({len(gateways)}):")
            for gw in gateways[:30]:
                call = gw.get("callsign") or gw.get("Callsign") or "?"
                mode = gw.get("mode") or gw.get("Mode") or ""
                dist = gw.get("distance") or gw.get("Distance") or ""
                extra = " ".join(str(x) for x in (mode, dist) if x)
                print(f"  {call}  {extra}")
            return 0

        if args.action == "forms":
            if not hasattr(t, "list_forms"):
                print("This transport build cannot list forms.")
                return 1
            try:
                forms = await t.list_forms()
            except Exception as exc:  # noqa: BLE001
                print(f"Form catalog failed: {exc}")
                return 1
            if not forms:
                print(
                    "No Winlink forms installed. Run "
                    "'radioapp winlink forms-update' to download them."
                )
                return 0
            print(f"Installed Winlink forms ({len(forms)}):")
            last_folder = None
            for f in forms:
                folder = f.get("folder") or "(root)"
                if folder != last_folder:
                    print(f"\n  {folder}/")
                    last_folder = folder
                print(f"    {f['name']}   {f['path']}")
            return 0

        if args.action == "forms-update":
            if not hasattr(t, "update_forms"):
                print("This transport build cannot update forms.")
                return 1
            print("Asking Pat to download the latest Winlink standard forms ...")
            try:
                result = await t.update_forms()
            except Exception as exc:  # noqa: BLE001
                print(f"Forms update failed: {exc}")
                return 1
            if not result:
                print("Forms update failed (is Pat online and reachable?).")
                return 1
            action, version = result.get("action", ""), result.get("version", "")
            if action == "update":
                print(f"Updated to standard forms version {version}.")
            else:
                print(f"Already up to date (version {version}).")
            return 0

        if args.action == "form":
            if not hasattr(t, "get_form_template"):
                print("This transport build cannot read forms.")
                return 1
            if not args.target:
                print(
                    "error: give a template path, e.g. "
                    "radioapp winlink form ICS/ICS213.txt "
                    "(see 'radioapp winlink forms')",
                    file=sys.stderr,
                )
                return 2
            try:
                text = await t.get_form_template(args.target)
            except Exception as exc:  # noqa: BLE001
                print(f"Template fetch failed: {exc}")
                return 1
            if not text.strip():
                print(
                    f"No template text for '{args.target}'. Check the path "
                    "from 'radioapp winlink forms' (and that Pat is reachable)."
                )
                return 1
            print(f"Template: {args.target}\n")
            print(text.rstrip())
            fields = _winlink_form_fields(text)
            if fields:
                print("\nDetected fields (use --field NAME=VALUE):")
                for name in fields:
                    print(f"  {name}")
            else:
                print(
                    "\n(No prompt fields auto-detected — this form may have no "
                    "inputs, or use a layout we can't introspect. You can still "
                    "pass --field NAME=VALUE for any prompt the template asks for.)"
                )
            return 0

        if args.action == "compose-form":
            return await _winlink_compose_form(t, args)

        # connect
        if not hasattr(t, "connect_now"):
            print("This transport build cannot start a session.")
            return 1
        url = None
        if args.target:
            method = getattr(t, "_method", None)
            scheme = method.scheme if method is not None else "telnet"
            url = f"{scheme}://{args.target}"
        target = url or (
            t.connect_summary() if hasattr(t, "connect_summary") else "default"
        )
        print(f"Connecting via {target} ...")
        try:
            received = await t.connect_now(url)
        except Exception as exc:  # noqa: BLE001
            print(f"Session failed: {exc}")
            return 1
        print(f"Session complete — {received} message(s) received.")
        return 0

    return _run(_with_app(args.config, run))


def _winlink_form_fields(template_text: str) -> list[str]:
    """Discover a form's prompt field names (delegates to the transport helper)."""
    from .transports.winlink_transport import detect_form_fields

    return detect_form_fields(template_text)


def _parse_field_args(field_args, responses_file) -> tuple[dict[str, str], str | None]:
    """Merge ``--responses FILE`` + repeated ``--field K=V`` into one dict.

    Returns ``(responses, error)``; ``error`` is a message (and the caller should
    exit non-zero) when a ``--field`` lacks ``=`` or the JSON file is unreadable.
    ``--field`` values win over the file on key collisions.
    """
    import json

    responses: dict[str, str] = {}
    if responses_file:
        try:
            with open(responses_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return {}, f"could not read --responses file: {exc}"
        if not isinstance(data, dict):
            return {}, "--responses file must be a JSON object of field: value"
        responses.update({str(k): str(v) for k, v in data.items()})
    for raw in field_args or []:
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            return {}, f"--field must be KEY=VALUE (got '{raw}')"
        responses[key.strip()] = value
    return responses, None


async def _winlink_compose_form(t, args) -> int:
    """Build a Winlink form from --field/--responses and queue it to the outbox."""
    if not hasattr(t, "compose_form"):
        print("This transport build cannot compose forms.")
        return 1
    if not args.target:
        print(
            "error: give a template path, e.g. "
            "radioapp winlink compose-form ICS/ICS213.txt --field city=Boston "
            "(see 'radioapp winlink forms' / 'radioapp winlink form <path>')",
            file=sys.stderr,
        )
        return 2
    responses, err = _parse_field_args(args.field, args.responses)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if not getattr(t, "running", False):
        print("Winlink transport is not running (is Pat reachable?).")
        return 1
    try:
        built = await t.compose_form(
            args.target,
            responses,
            to=args.to,
            cc=args.cc,
            subject=args.subject,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Form build failed: {exc}")
        return 1
    if built is None:
        print(
            "Form build failed. Check the template path and that Pat is "
            "reachable; 'radioapp winlink forms' lists installed templates."
        )
        return 1
    print("Form queued to Pat's outbox for review:\n")
    print(f"  To      : {built.get('to') or '(none)'}")
    if built.get("cc"):
        print(f"  Cc      : {built['cc']}")
    print(f"  Subject : {built.get('subject') or '(none)'}")
    print("  Body    :")
    for line in (built.get("body") or "").splitlines() or ["(empty)"]:
        print(f"    {line}")
    print(
        "\nNothing has been transmitted yet — it sits in Pat's outbox. "
        "Run 'radioapp winlink connect' to send it (or delete it from Pat)."
    )
    return 0


def _cmd_js8(args: argparse.Namespace) -> int:
    """JS8Call utilities: read the inbox, send directed commands, leave relays.

    Requires a running JS8Call with its TCP/JSON API enabled (default
    127.0.0.1:2442). Everything here is passive except 'cmd'/'relay', which
    transmit on the air per JS8Call's normal turn-taking.
    """
    import asyncio as _asyncio

    async def run(app: App) -> int:
        t = next((x for x in app.transports if x.name == "js8call"), None)
        if t is None:
            print(
                "JS8Call transport is not enabled. Add a [transports.js8call] "
                "block (run 'radioapp setup')."
            )
            return 1
        if not getattr(t, "running", False):
            print(
                "JS8Call API is not reachable. Start JS8Call and enable its API "
                "(File → Settings → Reporting → API)."
            )
            return 1

        if args.action == "inbox":
            await t.request_inbox()
            await _asyncio.sleep(1.5)  # let the async INBOX.MESSAGES reply land
            msgs = t.inbox_messages()
            if not msgs:
                print("JS8Call inbox is empty.")
                return 0
            print(f"JS8Call inbox ({len(msgs)}):")
            for m in msgs:
                who = f"{m['from'] or '?'} -> {m['to'] or '?'}"
                print(f"  [{who}] {m['text']}")
            return 0

        if args.action == "cmd":
            if not args.target or not args.rest:
                print("usage: radioapp js8 cmd <CALL|@GROUP> <SNR?|GRID?|...>")
                return 1
            command = args.rest[0]
            ok = await t.send_directed_command(args.target, command)
            if not ok:
                print(
                    f"Could not send '{command}'. Known commands: "
                    + ", ".join(sorted(_js8_known_commands()))
                )
                return 1
            print(f"Sent directed command: {args.target.upper()} {command.upper()}")
            return 0

        if args.action == "sms":
            if not args.target or not args.rest:
                print("usage: radioapp js8 sms <phone> <message text>")
                return 1
            phone, text = args.target, " ".join(args.rest)
            ok = await t.send_sms(phone, text)
            if not ok:
                print(
                    "Could not send the SMS. Check the phone number and that "
                    "JS8Call's APRS gateway is enabled."
                )
                return 1
            print(
                f"Sent SMS to {phone} via APRS (SMSGTE) — JS8Call will transmit "
                "it on the next cycle."
            )
            return 0

        # relay
        if not args.target or not args.rest:
            print("usage: radioapp js8 relay <CALL> <message text>")
            return 1
        text = " ".join(args.rest)
        ok = await t.store_relay_message(args.target, text)
        if not ok:
            print("Could not store the relay message.")
            return 1
        print(
            f"Stored relay message for {args.target.upper()} — JS8Call will "
            "forward it when it next hears that station."
        )
        return 0

    return _run(_with_app(args.config, run))


def _js8_known_commands() -> frozenset[str]:
    from .transports.js8call_transport import JS8_DIRECTED_COMMANDS

    return JS8_DIRECTED_COMMANDS


def _cmd_favorites(args: argparse.Namespace) -> int:
    from datetime import datetime

    from .core.favorites import Favorites

    def _ago(when) -> str:
        if when is None:
            return "never"
        secs = int(max(0, (datetime.now(UTC) - when).total_seconds()))
        if secs < 60:
            return f"{secs}s ago"
        mins, secs = divmod(secs, 60)
        if mins < 60:
            return f"{mins}m ago"
        hrs, mins = divmod(mins, 60)
        if hrs < 24:
            return f"{hrs}h ago"
        return f"{hrs // 24}d ago"

    # 'watch' runs the live app so favorite sightings can stream to stdout.
    if args.action == "watch":
        async def run(app: App) -> int:
            print("Watching for favorites (Ctrl-C to stop)...")

            def on_msg(msg: UnifiedMessage, action) -> None:
                if msg.metadata.get("kind") != "announce":
                    return
                ident = msg.metadata.get("rns_dest") or msg.sender or ""
                sighting = app.favorites.note_sighting(
                    ident,
                    display_name=msg.metadata.get("display_name", ""),
                    when=msg.timestamp,
                )
                if sighting is None:
                    return
                ts = msg.timestamp.strftime("%H:%M:%S")
                tag = "ONLINE" if sighting.is_back_online else "heard "
                print(
                    f"{ts} [{msg.transport}] {tag}: "
                    f"{sighting.favorite.display}"
                )

            app.router.add_ui_callback(on_msg)
            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                return 0

        return _run(_with_app(args.config, run))

    cfg = Config.load(args.config)
    favs = Favorites.from_config(cfg)

    if args.action == "import-groups":
        from .core.js8call_query import query_groups

        js8 = cfg.transports.get("js8call", {})
        groups = query_groups(
            js8.get("host", "127.0.0.1"), int(js8.get("port", 2442))
        )
        if not groups:
            print(
                "no JS8Call groups found "
                "(is JS8Call running with the TCP API enabled?)"
            )
            return 0
        added = sum(1 for g in groups if not favs.is_favorite(f"@{g}"))
        for g in groups:
            favs.add(f"@{g}", kind="group")
        favs.save(cfg)
        joined = ", ".join(f"@{g}" for g in groups)
        print(f"imported {len(groups)} group(s) ({added} new): {joined}")
        return 0

    if args.action == "list":
        items = favs.all()
        if not items:
            print("(no favorites yet — add one with 'radioapp favorites add <id>')")
            return 0
        for f in items:
            label = f"  ({f.label})" if f.label else ""
            print(f"  {f.id}{label}   last seen: {_ago(f.last_seen)}")
            for key, value in f.meta.items():
                print(f"      {key}: {value}")
        return 0

    if not args.identity:
        print("error: identity is required for add/remove/set", file=sys.stderr)
        return 2

    # Collect contact metadata from the convenience flags and any --meta KEY=VALUE
    # pairs. A flag left unset (None) is ignored; an explicit empty string clears.
    meta: dict[str, str] = {}
    for flag, key in (
        (args.name, "name"),
        (args.grid, "gridsquare"),
        (args.power, "power"),
        (args.notes, "notes"),
    ):
        if flag is not None:
            meta[key] = flag
    for pair in args.meta:
        if "=" not in pair:
            print(f"error: --meta expects KEY=VALUE, got {pair!r}", file=sys.stderr)
            return 2
        key, value = pair.split("=", 1)
        key = key.strip()
        if key:
            meta[key] = value.strip()

    if args.action == "add":
        fav = favs.add(args.identity, args.label, meta=meta or None)
        favs.save(cfg)
        label = f"  ({fav.label})" if fav.label else ""
        print(f"added favorite: {fav.id}{label}")
        for key, value in fav.meta.items():
            print(f"  {key}: {value}")
        return 0

    if args.action == "set":
        if not meta and not args.label:
            print(
                "error: 'set' needs at least one of "
                "--name/--grid/--power/--notes/--meta/--label",
                file=sys.stderr,
            )
            return 2
        if args.label:
            favs.set_label(args.identity, args.label)
        fav = favs.set_meta(args.identity, meta) if meta else favs.match(args.identity)
        favs.save(cfg)
        if fav is None:
            print("not found")
            return 1
        label = f"  ({fav.label})" if fav.label else ""
        print(f"updated favorite: {fav.id}{label}")
        for key, value in fav.meta.items():
            print(f"  {key}: {value}")
        return 0

    if args.action == "remove":
        ok = favs.remove(args.identity)
        favs.save(cfg)
        print("removed" if ok else "not found")
        return 0 if ok else 1
    return 1


def _cmd_browse(args: argparse.Namespace) -> int:
    """Fetch and render a NomadNet page (read-only)."""
    from .core.micron import parse_address, render_micron

    dest, path, fields = parse_address(args.address)
    if not dest:
        print(
            "error: give a node hash, e.g. radioapp browse <hash>:/page/index.mu",
            file=sys.stderr,
        )
        return 2

    async def run(app: App) -> int:
        res = await app.browser.fetch(
            dest,
            path,
            field_data=fields or None,
            timeout=args.timeout,
            prefer_cache=args.offline,
            cache_first=not args.live and not args.offline,
        )
        if not res.ok:
            print(f"error: {res.error}", file=sys.stderr)
            return 1
        if res.from_cache:
            from datetime import datetime

            secs = max(0.0, (datetime.now(UTC) - res.fetched_at).total_seconds())
            if secs < 90:
                age = f"{secs:.0f}s ago"
            elif secs < 5400:
                age = f"{secs / 60:.0f}m ago"
            elif secs < 172800:
                age = f"{secs / 3600:.0f}h ago"
            else:
                age = f"{secs / 86400:.0f}d ago"
            print(f"(cached copy — fetched {age}; may be stale)", file=sys.stderr)
        if args.raw:
            print(res.content)
            return 0
        page = render_micron(res.content, base_dest=dest)
        print(page.plain)
        if page.links:
            print("\nLinks:")
            for i, lk in enumerate(page.links, 1):
                tgt = (lk.dest or dest)[:16]
                extra = f"  {lk.fields}" if lk.fields else ""
                print(f"  [{i}] {lk.path}  ({tgt}){extra}")
        return 0

    return _run(_with_app(args.config, run))


def _cmd_nodes(args: argparse.Namespace) -> int:
    """List NomadNet nodes (sites) discovered from announces."""

    async def run(app: App) -> int:
        ret = next((t for t in app.transports if t.name == "reticulum"), None)
        if ret is None or not ret.running:
            print("Reticulum transport is not running.", file=sys.stderr)
            return 1
        if args.wait > 0:
            print(f"Listening {args.wait:.0f}s for node announces...")
            await asyncio.sleep(args.wait)
        nodes = ret.known_nodes()
        if not nodes:
            print("(no nodes/sites heard yet — try 'radioapp nodes --wait 30')")
            return 0
        for n in nodes:
            name = n["name"] or "(unnamed)"
            print(f"  {n['dest']}  {name}")
        return 0

    return _run(_with_app(args.config, run))


def _cmd_nomad(args: argparse.Namespace) -> int:
    """NomadNet maintenance commands (currently: cache sync)."""

    async def run(app: App) -> int:
        node_favs = [f for f in app.favorites.all() if f.kind == "node"]
        if not node_favs:
            print(
                "(no NomadNet node favorites yet — save one with the TUI's "
                "'Save node' action or 'radioapp favorites add <hash>')"
            )
            return 0
        if not app.browser.available:
            print(
                "Reticulum transport is not running; cannot refresh pages "
                "(start rnsd, then retry).",
                file=sys.stderr,
            )
            return 1
        print(f"Syncing {len(node_favs)} favorite node(s)...")
        res = await app.browser.sync_favorites(
            node_favs,
            timeout=args.timeout,
            follow_links=args.follow_links,
        )
        for label, status in res.pages:
            print(f"  {status:>12}  {label}")
        print(
            f"done: {res.ok} cached, {res.failed} failed, {res.skipped} skipped"
        )
        return 0 if res.failed == 0 else 1

    return _run(_with_app(args.config, run))


def _cmd_peers(args: argparse.Namespace) -> int:
    """List LXMF peers (messageable identities) discovered from announces."""

    async def run(app: App) -> int:
        ret = next((t for t in app.transports if t.name == "reticulum"), None)
        if ret is None or not ret.running:
            print("Reticulum transport is not running.", file=sys.stderr)
            return 1
        if args.wait > 0:
            print(f"Listening {args.wait:.0f}s for peer announces...")
            await asyncio.sleep(args.wait)
        peers = ret.known_peers()
        if not peers:
            print("(no peers heard yet — try 'radioapp peers --wait 30')")
            return 0
        for p in peers:
            name = p["name"] or "(anonymous)"
            print(f"  {p['dest']}  {name}")
        return 0

    return _run(_with_app(args.config, run))


if __name__ == "__main__":
    raise SystemExit(main())

