"""Command-line interface (the non-critical CLI subset).

A thin stdlib (``argparse``) frontend over the same core the GUI/TUI use:
send, read history, manage groups/subscriptions, inspect transports & config.
Interactive features (live chat, page browsing) are intentionally left to a TUI.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import UTC
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
    target.add_argument("--group", help="group name, e.g. TTP")
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
    rtarget.add_argument("--thread", help="thread key, e.g. @TTP or a callsign")
    rtarget.add_argument("--to", help="direct conversation with this identity")
    p_read.add_argument("--limit", type=int, default=200)
    p_read.set_defaults(func=_cmd_read)

    p_listen = sub.add_parser("listen", help="stream incoming messages to stdout")
    p_listen.add_argument("--group", help="only show this group")
    p_listen.set_defaults(func=_cmd_listen)

    p_groups = sub.add_parser("groups", help="list configured groups")
    p_groups.set_defaults(func=_cmd_groups)

    p_sub = sub.add_parser("sub", help="manage group subscriptions")
    p_sub.add_argument("action", choices=["add", "remove", "list"])
    p_sub.add_argument("group", nargs="?")
    p_sub.set_defaults(func=_cmd_sub)

    p_status = sub.add_parser("status", help="show transport status")
    p_status.set_defaults(func=_cmd_status)

    p_transports = sub.add_parser("transports", help="list transports + capabilities")
    p_transports.set_defaults(func=_cmd_transports)

    p_config = sub.add_parser("config", help="inspect/initialise configuration")
    p_config.add_argument("action", choices=["path", "show", "init"])
    p_config.set_defaults(func=_cmd_config)

    p_tui = sub.add_parser("tui", help="launch the terminal user interface")
    p_tui.set_defaults(func=_cmd_tui)

    p_setup = sub.add_parser(
        "setup", help="interactive setup wizard (all user settings, one file)"
    )
    p_setup.set_defaults(func=_cmd_setup)


    p_ret = sub.add_parser("reticulum", help="Reticulum / RNode utilities")
    p_ret.add_argument(
        "action", choices=["address", "status", "setup-rnode"]
    )
    p_ret.set_defaults(func=_cmd_reticulum)

    p_fav = sub.add_parser(
        "favorites",
        help="manage favorite peers (callsigns / RNS hex hashes) for alerts",
    )
    p_fav.add_argument(
        "action", choices=["add", "remove", "list", "watch", "import-groups"]
    )
    p_fav.add_argument(
        "identity",
        nargs="?",
        help="callsign or RNS destination hash (required for add/remove)",
    )
    p_fav.add_argument("--label", default="", help="optional friendly label")
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
    for g in reg.all():
        subscribed = "*" if reg.is_subscribed(g.name) else " "
        where = ", ".join(g.transports) or "-"
        print(f"[{subscribed}] {g.tag:<12} {g.display_name:<20} via {where}")
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
        for t in app.transports:
            state = "UP" if t.running else "down"
            caps = t.capabilities()
            ident_kind = "anon" if not caps.carries_operator_identity else "callsign"
            print(f"  - {t.name:<12} {state:<5} id={ident_kind}")
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
    return 1


def _cmd_tui(args: argparse.Namespace) -> int:
    from .ui import run_tui

    run_tui(args.config)
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


def _cmd_setup(args: argparse.Namespace) -> int:
    """Interactive wizard: capture all user settings into the one config file.

    Radio_App is a single pane of glass over disparate transports, so this asks
    once for everything (identity, Reticulum/rnsd, JS8Call, MeshCore) and writes
    it to the single TOML config. The radio is driven by the transport app
    (JS8Call), so there are no rig/CAT questions here.
    """
    from .core.js8call_query import query_groups, query_station
    from .core.station import Station

    cfg = Config.load(args.config)
    print(f"\nRadio_App setup  ->  {cfg.path}")
    print("Press Enter to keep the current/[]-shown value.\n")

    # -- general -------------------------------------------------------------
    print("General")
    display_name = _ask("Display name", cfg.display_name)

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

    # -- compliance (advanced) ----------------------------------------------
    allow_enc = _ask_bool(
        "\nAllow encrypted payloads on HF? (advanced; usually NO)",
        cfg.allow_encrypted_on_hf(),
    )

    # -- write it all to the single config file ------------------------------
    cfg.set("general", "display_name", display_name)
    cfg.set("station", "callsign", station.callsign)
    cfg.set("station", "grid_square", station.grid_square)
    cfg.set("compliance", "allow_encrypted_on_hf", allow_enc)
    transports = dict(cfg.transports)
    transports["reticulum"] = ret
    transports["js8call"] = js8
    transports["meshcore"] = mc
    cfg.data["transports"] = transports
    cfg.save()

    print(f"\nSaved configuration to {cfg.path}")
    print(f"  station   : {station.callsign or '(none)'}  {station.grid_square}")
    enabled = [
        n for n in ("reticulum", "js8call", "meshcore")
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
        return 0

    if not args.identity:
        print("error: identity is required for add/remove", file=sys.stderr)
        return 2

    if args.action == "add":
        fav = favs.add(args.identity, args.label)
        favs.save(cfg)
        label = f"  ({fav.label})" if fav.label else ""
        print(f"added favorite: {fav.id}{label}")
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
            dest, path, field_data=fields or None, timeout=args.timeout
        )
        if not res.ok:
            print(f"error: {res.error}", file=sys.stderr)
            return 1
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

