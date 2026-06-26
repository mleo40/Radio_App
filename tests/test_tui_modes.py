"""Headless TUI tests for the mode-workspace surfaces (Milestones 1-2).

These drive the Textual app via its test pilot, so they need the optional
``tui`` extra; they skip cleanly when Textual is not installed. No radio
hardware is involved - transports report DOWN, which is what we assert. Tests
wrap their coroutines in ``asyncio.run`` (the project does not use
pytest-asyncio).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import Button  # noqa: E402

from radio_app.core.message import AddressType, UnifiedMessage  # noqa: E402
from radio_app.transports.base import TRANSPORT_REGISTRY  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402

CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[transports.js8call]
enabled = true
port = 2442
[transports.meshcore]
enabled = true
connection = "tcp"
tcp_port = 5000
[transports.mercury]
enabled = true
port = 7373
"""


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG)
    # Mercury is intentionally not registered (its import is commented out in
    # transports/__init__). Other test modules import the mercury module
    # directly, which would re-register it globally, so drop it here to mirror
    # the production import graph where the mode does not exist.
    TRANSPORT_REGISTRY.pop("mercury", None)
    return str(p)


def _announce(ident="abcd1234", name="lab", transport="reticulum"):
    return UnifiedMessage(
        sender=ident,
        content="hi",
        transport=transport,
        address_type=AddressType.BROADCAST,
        metadata={"kind": "announce", "display_name": name, "rns_dest": ident},
    )


def test_mode_selector_has_nomadnet_and_no_mercury(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ids = [b.id for b in app.query_one("#modebar").query(Button)]
            assert "mode-js8call" in ids
            assert "mode-meshcore" in ids       # MeshCore (Mesh) mode present
            assert "mode-nomadnet" in ids       # virtual mode present
            assert "mode-mercury" not in ids    # mercury disabled
            assert "view-watch" in ids and "view-health" in ids
            assert "view-favorites" in ids       # favorites page chip

    asyncio.run(run())


def test_favorites_view_add_classify_and_remove(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Add offline favorites of each kind (no peer online required).
            app.action_favorites_view()
            await pilot.pause()
            assert app.query_one("#main").current == "favorites-view"
            app._add_favorite_from_input("KD2ABC Bob")          # callsign
            app._add_favorite_from_input("a1b2c3d4e5f60718 lab")  # hash (peer)
            # Explicit node-kind: a NomadNet server hash saved while offline.
            app._add_favorite_from_input("node ffeeddccbbaa9988 HomeNode")
            await pilot.pause()
            assert app.core.favorites.is_favorite("KD2ABC")
            assert app.core.favorites.is_favorite("a1b2c3d4e5f60718")
            assert app.core.favorites.is_favorite("ffeeddccbbaa9988")
            # Classification: non-hex -> callsign, hex (no known node) -> hash,
            # explicit node -> node even though it's offline/unheard.
            assert app._favorite_kind_by_id("KD2ABC") == "callsign"
            assert app._favorite_kind_by_id("a1b2c3d4e5f60718") == "hash"
            assert app._favorite_kind_by_id("ffeeddccbbaa9988") == "node"
            # The node kind persists across a reload of favorites from config.
            from radio_app.core.favorites import Favorites
            reloaded = Favorites.from_config(app.core.config)
            assert reloaded.match("ffeeddccbbaa9988").kind == "node"
            # Rows include both section headers ("") and the ids.
            assert "KD2ABC" in app._fav_keys
            assert "a1b2c3d4e5f60718" in app._fav_keys
            # Remove one via the selection + action path.
            lst = app.query_one("#favorites-list")
            lst.index = app._fav_keys.index("KD2ABC")
            app.action_remove_favorite()
            await pilot.pause()
            assert not app.core.favorites.is_favorite("KD2ABC")

    asyncio.run(run())


def test_nomad_bar_buttons_visible_on_home(config_path):
    """The NomadNet home screen shows its action-bar buttons immediately.

    Regression: the buttons were clipped (no #nomad-bar Button height rule) and
    only appeared after opening a server forced a relayout.
    """
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_nomadnet()
            await pilot.pause()
            sync = app.query_one("#nomad-sync", Button)
            fav = app.query_one("#nomad-fav", Button)
            for btn in (sync, fav):
                assert btn.display is True
                assert btn.region.width > 0
                assert btn.region.height > 0

    asyncio.run(run())


def test_view_switching(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            assert app.query_one("#main").current == "active-view"
            app._show_nomadnet()
            await pilot.pause()
            assert app.view == "nomadnet"
            assert app.query_one("#main").current == "nomadnet-view"
            app._show_watch()
            await pilot.pause()
            assert app.query_one("#main").current == "monitor-view"
            app.action_health()
            await pilot.pause()
            assert app.query_one("#main").current == "health-view"

    asyncio.run(run())


def test_f4_fav_only_active_in_chat_and_watch_not_readonly(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # In a chat mode F4 is enabled and toggles the per-mode filter
            # in place (no view switch).
            app._select_mode("js8call")
            await pilot.pause()
            assert app.check_action("toggle_fav_only", ()) is True
            app.action_toggle_fav_only()
            await pilot.pause()
            assert app._active_fav_only is True
            assert app.query_one("#main").current == "active-view"
            app.action_toggle_fav_only()  # toggles back off
            assert app._active_fav_only is False
            # On Watch it toggles the Watch stream filter.
            app._show_watch()
            await pilot.pause()
            assert app.check_action("toggle_fav_only", ()) is True
            app.action_toggle_fav_only()
            assert app._monitor_fav_only is True
            # NomadNet is an operating mode too: F4 is enabled there.
            app._show_nomadnet()
            await pilot.pause()
            assert app.check_action("toggle_fav_only", ()) is True
            # On a read-only surface F4 is hidden/inert.
            app._show_favorites()
            await pilot.pause()
            assert app.check_action("toggle_fav_only", ()) is False

    asyncio.run(run())


def test_active_fav_only_filters_threads(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            # Two stored js8call conversations; only one is a favorite.
            app.core.favorites.add("W1AW")
            for call, text in (("W1AW", "hi"), ("N0CALL", "yo")):
                msg = UnifiedMessage(
                    sender=call,
                    content=text,
                    transport="js8call",
                    address_type=AddressType.DIRECT,
                    recipient="me",
                )
                msg.transport = "js8call"
                await app.core.router._handle_inbound(msg)
            app._refresh_threads()
            await pilot.pause()
            assert "W1AW" in app._thread_keys
            assert "N0CALL" in app._thread_keys
            # Filter on -> only the favorite conversation remains.
            app._toggle_active_fav_only()
            await pilot.pause()
            assert "W1AW" in app._thread_keys
            assert "N0CALL" not in app._thread_keys

    asyncio.run(run())


def test_js8_group_favorite_classify_and_open(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.action_favorites_view()
            await pilot.pause()
            # A '@'-prefixed favorite is classified as a JS8Call group, and a
            # callsign as a callsign - even without an explicit type keyword.
            app._add_favorite_from_input("@TTP tactical net")
            app._add_favorite_from_input("KD2ABC Bob")
            await pilot.pause()
            assert app._favorite_kind_by_id("@TTP") == "group"
            assert app._favorite_kind_by_id("KD2ABC") == "callsign"
            # The group kind persists to config.
            from radio_app.core.favorites import Favorites
            reloaded = Favorites.from_config(app.core.config)
            assert reloaded.match("@TTP").kind == "group"
            # Opening the group favorite switches to js8call and targets @TTP.
            app._open_favorite("@TTP")
            await pilot.pause()
            assert app.active_transport == "js8call"
            assert app.current_target == "@TTP"

    asyncio.run(run())


def test_import_js8_groups_button_and_apply(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.action_favorites_view()
            await pilot.pause()
            # The Favorites bar offers an import button.
            ids = [b.id for b in app.query_one("#fav-bar").query(Button)]
            assert "fav-import-groups" in ids
            # Applying fetched groups adds them as group-kind favorites.
            app._apply_imported_groups(["TTP", "TTPNE"])
            await pilot.pause()
            assert app.core.favorites.is_favorite("@TTP")
            assert app.core.favorites.is_favorite("@TTPNE")
            assert app._favorite_kind_by_id("@TTP") == "group"
            # Persisted with the group kind.
            from radio_app.core.favorites import Favorites
            reloaded = Favorites.from_config(app.core.config)
            assert reloaded.match("@TTPNE").kind == "group"

    asyncio.run(run())


def test_active_fav_only_shows_banner(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            banner = app.query_one("#active-banner")
            assert banner.display is False
            app._toggle_active_fav_only()
            await pilot.pause()
            assert banner.display is True
            assert "FAVORITES only" in str(banner.render())
            app._toggle_active_fav_only()
            await pilot.pause()
            assert banner.display is False

    asyncio.run(run())



def test_last_seen_updates_from_broadcast_watch_traffic(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.core.favorites.add("w1aw")  # stored lowercase
            assert app.core.favorites.match("W1AW").last_seen is None
            # A BROADCAST (not directed to us) heard on the air from the
            # favorite callsign must still bump its "last seen".
            msg = UnifiedMessage(
                sender="W1AW",
                content="CQ CQ",
                transport="js8call",
                address_type=AddressType.BROADCAST,
                metadata={"snr": -5},
            )
            msg.transport = "js8call"
            await app.core.router._handle_inbound(msg)
            await pilot.pause()
            assert app.core.favorites.match("W1AW").last_seen is not None

    asyncio.run(run())


def test_watch_pause_and_clear(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_watch()
            await pilot.pause()
            app._toggle_watch_pause()
            assert app._watch_paused is True
            # A normal inbound message is buffered but not rendered while paused.
            msg = UnifiedMessage(
                sender="peer1",
                content="ping",
                transport="reticulum",
                address_type=AddressType.DIRECT,
                recipient="me",
            )
            await app.core.router._handle_inbound(msg)
            await pilot.pause()
            assert len(app._monitor_msgs) == 1
            app._clear_watch()
            assert app._monitor_msgs == []

    asyncio.run(run())


def test_f3_cycles_modes(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Cycle order = transports (js8call, meshcore) then virtual nomadnet.
            seen = []
            for _ in range(6):
                app.action_choose_mode()
                await pilot.pause()
                seen.append(app._current_mode_key())
            assert seen == [
                "js8call", "meshcore", "nomadnet",
                "js8call", "meshcore", "nomadnet",
            ]

    asyncio.run(run())


def test_f5_cycles_watch_health_favorites(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # From a chat mode, F5 enters the cycle at Watch.
            app._select_mode("js8call")
            app.action_cycle_utility()
            await pilot.pause()
            assert app.query_one("#main").current == "monitor-view"
            app.action_cycle_utility()  # -> Health
            await pilot.pause()
            assert app.query_one("#main").current == "health-view"
            app.action_cycle_utility()  # -> Logs
            await pilot.pause()
            assert app.query_one("#main").current == "logs-view"
            app.action_cycle_utility()  # -> Favorites
            await pilot.pause()
            assert app.query_one("#main").current == "favorites-view"
            app.action_cycle_utility()  # -> back to Watch
            await pilot.pause()
            assert app.query_one("#main").current == "monitor-view"

    asyncio.run(run())


def test_logs_surface_renders_and_filters(config_path):
    import logging as _logging

    from radio_app.logging_setup import get_ring_handler

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ring = get_ring_handler()
            assert ring is not None
            # Emit one INFO and one WARNING record through the root logger.
            log = _logging.getLogger("radio_app.test")
            log.info("hello info")
            log.warning("careful warning")
            await pilot.pause()
            # Opening the Logs surface shows the feed and clears the peak badge.
            app._show_logs()
            await pilot.pause()
            assert app.query_one("#main").current == "logs-view"
            assert ring.peak_level() == 0  # reset on view
            # /loglevel error hides the INFO/WARNING rows.
            app._set_log_level("error")
            await pilot.pause()
            assert app._logs_min_level == _logging.ERROR

    asyncio.run(run())


def test_log_badge_appears_for_warning(config_path):
    import logging as _logging

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # No badge initially (peak below WARNING after startup view).
            app._select_mode("js8call")
            _logging.getLogger("radio_app.test").error("boom")
            await pilot.pause()
            assert "ERR" in app._log_badge_markup()

    asyncio.run(run())


def test_delivery_receipt_annotates_sent_message(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # A previously-sent message lives in the store as 'sent'.
            sent = UnifiedMessage(
                sender="me",
                content="hi there",
                transport="reticulum",
                address_type=AddressType.DIRECT,
                recipient="abcdef0123456789",
                msg_id="m-123",
            )
            from radio_app.core.message import DeliveryStatus
            sent.status = DeliveryStatus.SENT
            app.core.store.save(sent)
            # A delivery receipt (LXMF-style telemetry) routed through the core
            # records the per-message status AND persists it to the store.
            receipt = UnifiedMessage(
                sender="reticulum",
                content="delivery delivered",
                transport="reticulum",
                address_type=AddressType.BROADCAST,
                metadata={
                    "kind": "delivery",
                    "status": "delivered",
                    "ref_msg_id": "m-123",
                    "recipient": "abcdef0123456789",
                },
            )
            await app.core.router._handle_inbound(receipt)
            await pilot.pause()
            assert app._delivery_status.get("m-123") == "delivered"
            # Persisted so it survives a restart and is verifiable later.
            stored = app.core.store.read_thread("abcdef0123456789")
            assert stored and stored[0].status is DeliveryStatus.DELIVERED

    asyncio.run(run())


def test_reticulum_tools_hidden_when_not_reticulum_mode(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # The Reticulum-only tools are inert/hidden outside Reticulum mode
            # (this config has no reticulum transport enabled).
            app._select_mode("js8call")
            await pilot.pause()
            assert app.check_action("identity", ()) is False
            assert app.check_action("announce", ()) is False
            assert app.check_action("find_path", ()) is False
            # Invoking them is a graceful no-op (logs, never raises).
            app.action_identity()
            app.action_announce()
            app.action_find_path()
            await pilot.pause()

    asyncio.run(run())


def test_friendly_name_truncates_hash_in_left_pane(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            full = "abc123def456789a0b1c2d3e4f5a6b7c8"
            # With no friendly name, a long hash is truncated for display...
            assert app._display_id(full) == full[:10] + "\u2026"
            # ...callsigns/@groups are shown unchanged.
            assert app._display_id("KD2ABC") == "KD2ABC"
            assert app._display_id("@TTP") == "@TTP"

    asyncio.run(run())


def test_name_command_sets_friendly_name(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            full = "abc123def456789a0b1c2d3e4f5a6b7c8"
            app.current_target = full
            # Name the open conversation; it should replace the hash for display
            # and persist as the favorite label.
            await app._handle_command("/name Alice")
            await pilot.pause()
            assert app._display_id(full) == "Alice"
            assert app.core.favorites.match(full).label == "Alice"
            # Clearing reverts to the truncated hash.
            await app._handle_command("/name -")
            await pilot.pause()
            assert app._display_id(full) == full[:10] + "\u2026"

    asyncio.run(run())


def test_announce_learns_favorite_kind(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # A saved hash with no known kind...
            site = "aa" * 16
            app.core.favorites.add(site)
            assert app.core.favorites.match(site).kind == ""
            # ...is taught 'node' when we hear its NomadNet site announce.
            await app.core.router._handle_inbound(
                UnifiedMessage(
                    sender=site,
                    content="x",
                    transport="reticulum",
                    address_type=AddressType.BROADCAST,
                    metadata={
                        "kind": "announce",
                        "aspect": "site",
                        "rns_dest": site,
                        "display_name": "SiteX",
                    },
                )
            )
            await pilot.pause()
            assert app.core.favorites.match(site).kind == "node"
            # An LXMF peer announce teaches 'peer' instead.
            peer = "bb" * 16
            app.core.favorites.add(peer)
            await app.core.router._handle_inbound(
                UnifiedMessage(
                    sender=peer,
                    content="x",
                    transport="reticulum",
                    address_type=AddressType.BROADCAST,
                    metadata={
                        "kind": "announce",
                        "aspect": "peer",
                        "rns_dest": peer,
                        "display_name": "Alice",
                    },
                )
            )
            await pilot.pause()
            assert app.core.favorites.match(peer).kind == "peer"

    asyncio.run(run())


def test_learn_does_not_override_callsign_or_group(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # An explicit group/callsign classification must never be flipped by
            # a (spoofable) announce aspect.
            app.core.favorites.add("@TTP", kind="group")
            app._learn_favorite_kind("@TTP", "node")
            assert app.core.favorites.match("@TTP").kind == "group"

    asyncio.run(run())


def test_close_chat_removes_conversation(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            # Seed a stored js8call conversation, open it, then close it.
            msg = UnifiedMessage(
                sender="W1AW",
                content="hi",
                transport="js8call",
                address_type=AddressType.DIRECT,
                recipient="me",
            )
            msg.transport = "js8call"
            await app.core.router._handle_inbound(msg)
            app.current_target = "W1AW"
            app._refresh_threads()
            await pilot.pause()
            assert "W1AW" in app._thread_keys
            # Close it via the command path; the thread disappears and the open
            # conversation is cleared.
            await app._handle_command("/close")
            await pilot.pause()
            assert app.current_target is None
            assert "W1AW" not in app._thread_keys
            assert app.core.store.read_thread("W1AW") == []

    asyncio.run(run())


def test_close_chat_binding_gated_to_open_conversation(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            # No conversation open -> close is inert/hidden.
            app.current_target = None
            assert app.check_action("close_chat", ()) is False
            app.current_target = "W1AW"
            assert app.check_action("close_chat", ()) is True

    asyncio.run(run())


def test_reticulum_banner_shows_display_name(config_path):
    """The status bar 'id:' shows your public display name + clickable address.

    We stub the active-transport accessor so the test never starts a real RNS
    instance (RNS may be installed); only the status-bar logic is exercised.
    """
    class _Caps:
        carries_operator_identity = False
        supports_encryption = True

    class _FakeRet:
        def capabilities(self):
            return _Caps()

        def local_display_name(self):
            return "LabRadio"

        def local_identity(self):
            return "abcd1234ef567890aa"

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            app._active_transport_obj = lambda: _FakeRet()
            app.active_transport = "reticulum"
            markup = app._ident_markup()
            assert "LabRadio" in markup
            # The address is wired to the copy action.
            assert "copy_address('abcd1234ef567890aa')" in markup
            # And it renders in the status bar.
            app._update_status()
            assert "LabRadio" in str(app.query_one("#statusbar").render())

    asyncio.run(run())


def test_reticulum_status_anonymous_when_no_name(config_path):
    class _Caps:
        carries_operator_identity = False
        supports_encryption = True

    class _FakeRet:
        def capabilities(self):
            return _Caps()

        def local_display_name(self):
            return ""

        def local_identity(self):
            return "rns:anonymous"  # RNS not up yet

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            app._active_transport_obj = lambda: _FakeRet()
            app.active_transport = "reticulum"
            markup = app._ident_markup()
            assert "anonymous" in markup
            # No real address -> not clickable.
            assert "copy_address" not in markup

    asyncio.run(run())


def test_watch_panel_lists_all_identities(config_path):
    """In the Watch view the status bar 'id:' lists every transport's identity.

    Watch spans all transports, so the operator should see all of their
    identities at once (each transport prefixed by name, addresses still
    click-to-copy) rather than only the active mode's identity.
    """
    class _Caps:
        carries_operator_identity = False
        supports_encryption = True

    class _Anon:
        def __init__(self, name, ident, display):
            self.name = name
            self._ident = ident
            self._display = display
            self.running = True

        def capabilities(self):
            return _Caps()

        def local_display_name(self):
            return self._display

        def local_identity(self):
            return self._ident

        async def stop(self):
            self.running = False

    fakes = [
        _Anon("reticulum", "abcd1234ef567890aa", "LabRadio"),
        _Anon("meshcore", "ff00aa11bb22cc33dd", "MeshNode"),
    ]

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(180, 30)) as pilot:
            await pilot.pause()
            app.core.transports = fakes  # type: ignore[attr-defined]
            # The all-identities fragment names every transport + its identity.
            markup = app._all_idents_markup()
            assert "reticulum" in markup and "LabRadio" in markup
            assert "meshcore" in markup and "MeshNode" in markup
            assert "copy_address('abcd1234ef567890aa')" in markup
            assert "copy_address('ff00aa11bb22cc33dd')" in markup
            # When the Watch (monitor) view is active, the status bar uses it.
            app.view = "monitor"
            app._update_status()
            shown = str(app.query_one("#statusbar").render())
            assert "LabRadio" in shown and "MeshNode" in shown

    asyncio.run(run())


def test_copy_address_action_copies_value(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            copied = {}
            app.copy_to_clipboard = lambda v: copied.setdefault("v", v)
            app.action_copy_address("deadbeefcafe")
            assert copied.get("v") == "deadbeefcafe"
            # Empty value is a no-op (no crash).
            app.action_copy_address("")

    asyncio.run(run())


def test_meshcore_inbound_chat_appears_in_watch(config_path):
    """Inbound MeshCore chats (direct + channel) show in the Watch feed."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_watch()
            await pilot.pause()
            direct = UnifiedMessage(
                sender="bbbbbbbbbbbb",
                content="hi over mesh",
                transport="meshcore",
                address_type=AddressType.DIRECT,
                recipient="me",
                metadata={"encrypted": True},
            )
            await app.core.router._handle_inbound(direct)
            chan = UnifiedMessage(
                sender="cccccccccccc",
                content="net in 5",
                transport="meshcore",
                address_type=AddressType.GROUP,
                group="0",
                metadata={"encrypted": True},
            )
            await app.core.router._handle_inbound(chan)
            await pilot.pause()
            contents = [m.content for m in app._monitor_msgs]
            assert "hi over mesh" in contents
            assert "net in 5" in contents

    asyncio.run(run())


def test_meshcore_outbound_chat_echoed_to_watch(config_path):
    """Messages you SEND on MeshCore are echoed into the Watch feed too.

    The transport isn't running in CI so the send fails, but the outbound
    message must still surface in Watch (marked as 'you') so a watched
    conversation shows both sides.
    """
    async def run():
        from textual.widgets import Label

        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            app.current_target = "@0"          # the public channel
            app._show_watch()
            await pilot.pause()
            app._send("hello channel")
            await pilot.pause()
            from radio_app.core.message import DeliveryStatus
            echoed = [m for m in app._monitor_msgs if m.content == "hello channel"]
            assert echoed, "sent message was not echoed into the Watch feed"
            # It is recorded as outbound (non-RECEIVED status), which drives the
            # 'you ->' rendering.
            assert echoed[0].status is not DeliveryStatus.RECEIVED
            # Rendered row marks it as outgoing.
            mlist = app.query_one("#monitor")
            texts = [
                str(item.query_one(Label).render())
                for item in mlist.children
            ]
            assert any("hello channel" in t and "you" in t for t in texts)

    asyncio.run(run())


def test_meshcore_announce_bar_and_action(config_path):
    """The Mesh panel exposes an Announce affordance bound to MeshCore.

    The action bar shows only in Mesh mode, the announce binding is enabled
    there (and identity stays Reticulum-only), and triggering it sends a
    zero-hop or flood advert via the transport.
    """
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            mc = next(t for t in app.core.transports if t.name == "meshcore")
            mc._running = True
            calls = []

            async def fake_advert(flood=False):
                calls.append(flood)
                return True

            mc.send_advert = fake_advert
            # In Mesh mode the action bar is visible and Announce is enabled.
            app._select_mode("meshcore")
            await pilot.pause()
            assert app.query_one("#mesh-bar").display is True
            assert app.check_action("announce", ()) is True
            assert app.check_action("identity", ()) is False  # Reticulum-only
            # Zero-hop then flood adverts dispatch to the transport.
            app._meshcore_announce(flood=False)
            await pilot.pause()
            assert calls == [False]
            app._meshcore_announce(flood=True)
            await pilot.pause()
            assert calls == [False, True]
            # Switching to a non-Mesh chat mode hides the bar.
            app._select_mode("js8call")
            await pilot.pause()
            assert app.query_one("#mesh-bar").display is False

    asyncio.run(run())


def test_meshcore_health_shows_device_telemetry(config_path):
    """The Health board renders MeshCore battery + radio telemetry."""
    from radio_app.transports.base import ReachabilityStatus

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.action_health()
            await pilot.pause()
            # Inject an OK probe + telemetry AFTER the passive refresh worker
            # has run (it would otherwise overwrite meshcore with DOWN).
            app._health["meshcore"] = ReachabilityStatus.OK
            app._device_telemetry["meshcore"] = {
                "name": "FieldNode",
                "public_key": "ab" * 32,
                "battery": 4100,
                "radio_freq": 915.0,
                "radio_bw": 250.0,
                "radio_sf": 10,
                "radio_cr": 5,
                "tx_power": 22,
            }
            # Capture what the Health board renders.
            log = app.query_one("#health-log")
            written = []
            orig = log.write
            log.write = lambda *a, **k: written.append(a[0] if a else "")
            try:
                app._render_health()
            finally:
                log.write = orig
            joined = "\n".join(str(w) for w in written)
            assert "FieldNode" in joined
            assert "915.000 MHz" in joined
            assert "SF10" in joined
            assert "batt 4100 mV" in joined

    asyncio.run(run())


def test_mesh_battery_format_handles_mv_percent_and_garbage(config_path):
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert "mV" in app._format_mesh_battery(4100)   # millivolts + est %
            assert app._format_mesh_battery(85) == "batt 85%"
            assert app._format_mesh_battery(None) == ""

    asyncio.run(run())


def test_channel_command_adds_hashtag_channel(config_path):
    """/channel add creates a hashtag channel, persists it, and opens it."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            mc = next(t for t in app.core.transports if t.name == "meshcore")
            mc._running = True
            created = []

            async def fake_create(index, name, secret=None):
                created.append((index, name, secret))
                mc.name_channel(index, name)  # mirror real device-cache update
                return True

            mc.create_channel = fake_create
            app._select_mode("meshcore")
            await pilot.pause()
            # Add a hashtag channel from the panel.
            await app._handle_command("/channel add 2 #ops")
            await pilot.pause()
            # The '#' is preserved when sent to the device (key derivation).
            assert created == [(2, "#ops", None)]
            # It is persisted to the config file (as a display label, no '#').
            saved = app.core.config.transports["meshcore"]["channels"]
            assert {"index": 2, "name": "ops"} in saved
            # And it becomes the open conversation, shown as '#ops'.
            assert app.current_target == "@2"
            assert "@2" in app._thread_keys
            assert "#ops" in app._display_id("@2")

    asyncio.run(run())


def test_channel_command_requires_meshcore_mode(config_path):
    """/channel is a no-op (with guidance) outside MeshCore mode."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            # Should not raise; just logs a hint to switch modes.
            await app._handle_command("/channel add 2 #ops")
            await pilot.pause()

    asyncio.run(run())


def test_meshcore_defaults_to_public_channel(config_path):
    """Selecting MeshCore opens the public channel (@0) by default."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # A non-channel transport opens with no conversation selected.
            app._select_mode("js8call")
            await pilot.pause()
            assert app.current_target is None
            # Switching to MeshCore defaults the open conversation to channel 0,
            # so the panel is ready to chat immediately.
            app._select_mode("meshcore")
            await pilot.pause()
            assert app.current_target == "@0"
            assert "@0" in app._thread_keys
            assert "#public" in app._display_id("@0")

    asyncio.run(run())


def test_meshcore_channels_listed_in_panel(config_path):
    """MeshCore mode's thread list surfaces its channels as conversations.

    Channels are addressed as '@<index>' (matching the group/channel send &
    receive mapping) and rendered with a friendly '#name' label.
    """
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            mc = next(t for t in app.core.transports if t.name == "meshcore")
            # Simulate channels the device reported at startup.
            mc._device_channels = [
                {"index": 0, "name": ""},       # public
                {"index": 3, "name": "ops"},    # named channel
            ]
            app._select_mode("meshcore")
            app._refresh_threads()
            await pilot.pause()
            # Both channels appear in the panel, before any traffic arrives.
            assert "@0" in app._thread_keys
            assert "@3" in app._thread_keys
            # And render with friendly '#name' labels.
            assert "#public" in app._display_id("@0")
            assert "#ops" in app._display_id("@3")
            # A non-channel transport leaves '@'-tags untouched.
            app._select_mode("js8call")
            await pilot.pause()
            assert app._display_id("@3") == "@3"

    asyncio.run(run())


def test_health_board_shows_system_section(config_path):
    """The Health board includes a host System section + database size line."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_health()
            await pilot.pause()
            log = app.query_one("#health-log")
            written = []
            orig = log.write
            log.write = lambda *a, **k: written.append(a[0] if a else "")
            try:
                app._render_health()
            finally:
                log.write = orig
            joined = "\n".join(str(w) for w in written)
            assert "System" in joined          # host resources header
            assert "disk" in joined            # free disk space line
            assert "data" in joined            # database size / counts line

    asyncio.run(run())


def test_health_board_shows_power_line(config_path, monkeypatch):
    """The Health board shows a battery/power line when a battery is present."""
    from radio_app.core import syshealth

    def fake_collect(disk_path=None):
        return syshealth.SystemHealth(
            disk_total=100, disk_free=50, disk_used_percent=50.0,
            battery_percent=42.0, power_plugged=False,
            battery_secs_left=3 * 3600 + 12 * 60, has_battery=True,
        )

    monkeypatch.setattr(syshealth, "collect", fake_collect)

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_health()
            await pilot.pause()
            log = app.query_one("#health-log")
            written = []
            orig = log.write
            log.write = lambda *a, **k: written.append(a[0] if a else "")
            try:
                app._render_health()
            finally:
                log.write = orig
            joined = "\n".join(str(w) for w in written)
            assert "power" in joined            # battery/power line present
            assert "42%" in joined              # charge percentage
            assert "on battery" in joined       # discharging state
            assert "3h12m" in joined            # runtime estimate

    asyncio.run(run())


def test_health_renders_with_no_rns_interface_stats(config_path):
    """Health view must render even when Reticulum can't be queried (no rnsd).

    The board now embeds an RNS interface-stats subsection sourced from
    ``ReticulumTransport.interface_stats()``. In CI Reticulum isn't running, so
    that call returns None - the renderer must degrade gracefully (no crash,
    no traceback) and still show the per-transport reachability lines.
    """
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.action_health()
            await pilot.pause()
            # Just assert we landed on Health without raising.
            assert app.query_one("#main").current == "health-view"

    asyncio.run(run())


CONFIG_WITH_GROUPS = """\
[general]
display_name = "Tester"
[logging]
file = ""
[transports.js8call]
enabled = true
port = 2442
[groups.TTP]
display_name = "TTP Net"
transports = ["js8call"]
[groups.TTPNE]
display_name = "TTP NE"
transports = ["js8call"]
"""


@pytest.fixture()
def groups_config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_WITH_GROUPS)
    TRANSPORT_REGISTRY.pop("mercury", None)
    return str(p)


def test_opened_conversation_persists_across_mode_switch(groups_config_path):
    """A conversation opened with no stored messages survives leaving the mode."""
    async def run():
        app = RadioTUI(groups_config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            # Open a direct conversation but never send/receive (nothing stored).
            await app._handle_command("/to KE7XYZ")
            await pilot.pause()
            assert "KE7XYZ" in app._thread_keys
            # Leave to NomadNet, then come back to JS8.
            app._show_nomadnet()
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            # The opened conversation is still in the left pane.
            assert "KE7XYZ" in app._thread_keys

    asyncio.run(run())


def test_js8_query_bar_sends_directed_query(groups_config_path):
    """The bottom query bar sends 'SNR?' to the open conversation in JS8 mode."""
    async def run():
        app = RadioTUI(groups_config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            ids = [b.id for b in app.query_one("#js8-query-bar").query(Button)]
            assert ids == [
                "js8-query-SNR",
                "js8-query-HEARING",
                "js8-query-STATUS",
                "js8-query-INFO",
            ]
            await app._handle_command("/to @TTP")
            await pilot.pause()
            sent: list[str] = []
            app._send = lambda text: sent.append(text)  # type: ignore[assignment]
            app._js8_send_query("SNR")
            assert sent == ["SNR?"]

    asyncio.run(run())


def test_left_pane_sorted_groups_then_callsigns(groups_config_path):
    """Left pane lists @groups first (alpha), then callsigns (alpha)."""
    async def run():
        app = RadioTUI(groups_config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            # Open several conversations out of order (groups + callsigns).
            for tgt in ("W1AW", "@ZULU", "KD2ABC", "@ALPHA", "AA1AA"):
                await app._handle_command(f"/to {tgt}")
            await pilot.pause()
            keys = app._thread_keys
            groups = [k for k in keys if k.startswith("@")]
            calls = [k for k in keys if not k.startswith("@")]
            # All groups precede all callsigns.
            assert keys == groups + calls
            # Each section is alphabetical (case-insensitive).
            assert groups == sorted(groups, key=str.lower)
            assert calls == sorted(calls, key=str.lower)
            # Spot-check the relative order we seeded.
            assert groups.index("@ALPHA") < groups.index("@ZULU")
            assert calls.index("AA1AA") < calls.index("KD2ABC") < calls.index("W1AW")

    asyncio.run(run())


def test_group_favorites_always_show_in_left_pane(config_path):
    """Saved @group favorites appear in the JS8 left pane even with no dialog."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.core.favorites.add("@TTP", kind="group")
            app.core.favorites.add("@EMCOMM", kind="group")
            app._select_mode("js8call")
            await pilot.pause()
            assert "@TTP" in app._thread_keys
            assert "@EMCOMM" in app._thread_keys
            # They also survive the favorites-only filter (groups always show).
            app.current_target = None
            app._toggle_active_fav_only()
            await pilot.pause()
            assert "@TTP" in app._thread_keys
            assert "@EMCOMM" in app._thread_keys

    asyncio.run(run())


def test_left_pane_three_tier_sort_groups_dialog_then_others(config_path):
    """Left pane: @groups, then contacts with dialog, then others — each alpha."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            # A stored conversation (dialog) with ZULU, plus opened-only contacts.
            msg = UnifiedMessage(
                sender="ZULU",
                content="hi",
                transport="js8call",
                address_type=AddressType.DIRECT,
                recipient="me",
            )
            msg.transport = "js8call"
            await app.core.router._handle_inbound(msg)
            # Group favorite + opened-only contacts (no dialog).
            app.core.favorites.add("@TTP", kind="group")
            for tgt in ("AA1AA", "MMM1M"):
                await app._handle_command(f"/to {tgt}")
            app.current_target = None
            app._refresh_threads()
            await pilot.pause()
            keys = app._thread_keys
            # Tier 0: groups first.
            assert keys[0] == "@TTP"
            # Tier 1: the contact we have dialog with (ZULU) precedes tier-2
            # opened-only contacts even though 'Z' sorts after 'A'/'M'.
            assert keys.index("ZULU") < keys.index("AA1AA")
            assert keys.index("ZULU") < keys.index("MMM1M")
            # Tier 2 alphabetical among themselves.
            assert keys.index("AA1AA") < keys.index("MMM1M")

    asyncio.run(run())


def test_watch_sort_by_mode_groups_transports(config_path):
    """The Watch 'By mode' toggle groups rows by transport."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_watch()
            await pilot.pause()
            for transport, body in (
                ("reticulum", "r1"),
                ("js8call", "j1"),
                ("reticulum", "r2"),
                ("js8call", "j2"),
            ):
                m = UnifiedMessage(
                    sender="x",
                    content=body,
                    transport=transport,
                    address_type=AddressType.BROADCAST,
                )
                m.transport = transport
                await app.core.router._handle_inbound(m)
            await pilot.pause()
            app._toggle_watch_sort()
            await pilot.pause()
            assert app._watch_sort_by_mode is True
            entries = [tr for (_thread, tr) in app._monitor_entries]
            assert entries == ["js8call", "js8call", "reticulum", "reticulum"]
            app._toggle_watch_sort()
            await pilot.pause()
            entries = [tr for (_thread, tr) in app._monitor_entries]
            assert entries == ["reticulum", "js8call", "reticulum", "js8call"]

    asyncio.run(run())


def test_watch_row_shows_mode_tag(config_path):
    """Each Watch row carries a transport (mode) tag."""
    from textual.widgets import Label

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_watch()
            await pilot.pause()
            m = UnifiedMessage(
                sender="KE7XYZ",
                content="hello",
                transport="js8call",
                address_type=AddressType.BROADCAST,
            )
            m.transport = "js8call"
            await app.core.router._handle_inbound(m)
            await pilot.pause()
            texts = [
                str(item.query_one(Label).render())
                for item in app.query_one("#monitor").children
            ]
            assert any("js8call" in t and "hello" in t for t in texts)

    asyncio.run(run())


def test_fav_only_always_shows_configured_groups(groups_config_path):
    """Favorites-only keeps every configured @group, even with no dialog."""
    async def run():
        app = RadioTUI(groups_config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            # A non-favorite direct conversation should be filtered out...
            await app._handle_command("/to N0CALL")
            await pilot.pause()
            app.current_target = None  # so it isn't kept as the open thread
            app._toggle_active_fav_only()
            await pilot.pause()
            assert "@TTP" in app._thread_keys
            assert "@TTPNE" in app._thread_keys
            assert "N0CALL" not in app._thread_keys

    asyncio.run(run())


def test_reply_to_opens_direct_thread_with_sender_in_meshcore(config_path):
    """Clicking a sender in a MeshCore channel opens a 1:1 reply to them."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            # MeshCore opens on the public channel ('@0'), a shared thread.
            assert app.current_target == "@0"
            # Click a participant's username: peel off into a direct reply.
            app.action_reply_to("a1b2c3d4e5f6", "meshcore")
            await pilot.pause()
            assert app.active_transport == "meshcore"
            assert app.current_target == "a1b2c3d4e5f6"
            assert app.query_one("#main").current == "active-view"

    asyncio.run(run())


def test_reply_to_switches_mode_when_sender_is_on_another_transport(config_path):
    """Replying to a sender hops to their transport before opening the thread."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            app.action_reply_to("KE7XYZ", "meshcore")
            await pilot.pause()
            # Mode followed the sender; the 1:1 thread is open in it.
            assert app.active_transport == "meshcore"
            assert app.current_target == "KE7XYZ"

    asyncio.run(run())


def test_reply_to_ignores_empty_ident(config_path):
    """A click with no usable sender id is a no-op (keeps the open thread)."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            before = app.current_target
            app.action_reply_to("", "meshcore")
            await pilot.pause()
            assert app.current_target == before

    asyncio.run(run())


def test_meshcore_channel_favorite_classify_and_open(config_path):
    """A MeshCore channel can be favorited by name and reopens in the mode."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            mc = next(t for t in app.core.transports if t.name == "meshcore")
            mc._device_channels = [
                {"index": 0, "name": ""},
                {"index": 3, "name": "ops"},
            ]
            app.action_favorites_view()
            await pilot.pause()
            # Add a channel favorite via the add-bar parser ('channel' keyword).
            app._add_favorite_from_input("channel ops Ops Net")
            await pilot.pause()
            assert app.core.favorites.is_favorite("ops")
            assert app._favorite_kind_by_id("ops") == "mc_channel"
            # Persisted with the mc_channel kind.
            from radio_app.core.favorites import Favorites
            reloaded = Favorites.from_config(app.core.config)
            assert reloaded.match("ops").kind == "mc_channel"
            # Opening it switches to MeshCore and targets the channel by index.
            app._open_favorite("ops")
            await pilot.pause()
            assert app.active_transport == "meshcore"
            assert app.current_target == "@3"

    asyncio.run(run())


def test_meshcore_contact_favorite_classify_and_open(config_path):
    """A MeshCore user (pubkey prefix) can be favorited and reopened direct."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.action_favorites_view()
            await pilot.pause()
            app._add_favorite_from_input("contact a1b2c3d4e5f6 Bob")
            await pilot.pause()
            # A hex pubkey would otherwise look like a Reticulum hash; the
            # explicit mc_peer kind keeps it a MeshCore contact.
            assert app._favorite_kind_by_id("a1b2c3d4e5f6") == "mc_peer"
            from radio_app.core.favorites import Favorites
            reloaded = Favorites.from_config(app.core.config)
            assert reloaded.match("a1b2c3d4e5f6").kind == "mc_peer"
            app._open_favorite("a1b2c3d4e5f6")
            await pilot.pause()
            assert app.active_transport == "meshcore"
            assert app.current_target == "a1b2c3d4e5f6"

    asyncio.run(run())


def test_fav_here_saves_open_meshcore_channel_and_contact(config_path):
    """'/fav here' favorites the open MeshCore channel or contact correctly."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            mc = next(t for t in app.core.transports if t.name == "meshcore")
            mc._device_channels = [
                {"index": 0, "name": ""},
                {"index": 3, "name": "ops"},
            ]
            app._select_mode("meshcore")
            await pilot.pause()
            # In a channel: saved by #name as an mc_channel.
            app.current_target = "@3"
            app._favorite_current_conversation()
            await pilot.pause()
            chan = app.core.favorites.match("ops")
            assert chan is not None
            assert chan.kind == "mc_channel"
            assert chan.label == "#ops"
            # In a direct chat: saved by pubkey prefix as an mc_peer.
            app.current_target = "ff00aa11bb22"
            app._favorite_current_conversation("Carol")
            await pilot.pause()
            peer = app.core.favorites.match("ff00aa11bb22")
            assert peer is not None
            assert peer.kind == "mc_peer"
            assert peer.label == "Carol"

    asyncio.run(run())


def test_fav_here_requires_open_conversation(config_path):
    """'/fav here' with nothing open is a graceful no-op."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")  # non-channel mode opens with no target
            await pilot.pause()
            assert app.current_target is None
            app._favorite_current_conversation()
            await pilot.pause()
            assert app.core.favorites.all() == []

    asyncio.run(run())


def test_meshcore_favorites_render_in_their_own_sections(config_path):
    """The Favorites view groups MeshCore channels and contacts separately."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            mc = next(t for t in app.core.transports if t.name == "meshcore")
            mc._device_channels = [
                {"index": 0, "name": ""},
                {"index": 3, "name": "ops"},
            ]
            app.core.favorites.add("ops", "Ops Net", kind="mc_channel")
            app.core.favorites.add("a1b2c3d4e5f6", "Bob", kind="mc_peer")
            app.action_favorites_view()
            await pilot.pause()
            app._render_favorites()
            await pilot.pause()
            # Both ids are selectable rows (headers are empty-string keys).
            assert "ops" in app._fav_keys
            assert "a1b2c3d4e5f6" in app._fav_keys
            from textual.widgets import Label
            rows = [
                str(item.query_one(Label).render())
                for item in app.query_one("#favorites-list").children
            ]
            joined = "\n".join(rows)
            assert "MeshCore channels" in joined
            assert "MeshCore contacts" in joined

    asyncio.run(run())


def test_anonymous_channel_sender_is_not_clickable(config_path):
    """A channel placeholder ('chanN') is not turned into a reply link."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # A named channel sender is addressable; the placeholder is not.
            named = UnifiedMessage(
                sender="Alice",
                content="hi",
                transport="meshcore",
                address_type=AddressType.GROUP,
                group="0",
                metadata={"display_name": "Alice"},
            )
            anon = UnifiedMessage(
                sender="chan0",
                content="raw",
                transport="meshcore",
                address_type=AddressType.GROUP,
                group="0",
                metadata={"mc_anon": True},
            )
            assert app._sender_is_replyable(named) is True
            assert app._sender_is_replyable(anon) is False

    asyncio.run(run())


def _home_config_path(tmp_path, home):
    cfg = (
        "[general]\ndisplay_name = \"Tester\"\n[logging]\nfile = \"\"\n"
        f"[ui]\nhome = \"{home}\"\n"
        "[transports.meshcore]\nenabled = true\nconnection = \"tcp\"\n"
        "tcp_port = 5000\n"
        "[transports.js8call]\nenabled = true\nport = 2442\n"
    )
    p = tmp_path / "config.toml"
    p.write_text(cfg)
    TRANSPORT_REGISTRY.pop("mercury", None)
    return str(p)


def test_home_view_config_opens_health(tmp_path):
    """[ui].home = 'health' lands on the Health surface at startup."""
    path = _home_config_path(tmp_path, "health")

    async def run():
        app = RadioTUI(path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._open_home_view()
            await pilot.pause()
            assert app.query_one("#main").current == "health-view"

    asyncio.run(run())


def test_home_view_config_opens_named_transport(tmp_path):
    """[ui].home = a transport name opens that chat mode."""
    path = _home_config_path(tmp_path, "meshcore")

    async def run():
        app = RadioTUI(path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.active_transport = None
            app.view = "active"
            app._open_home_view()
            await pilot.pause()
            assert app.active_transport == "meshcore"
            assert app.query_one("#main").current == "active-view"

    asyncio.run(run())


def test_home_view_config_unknown_falls_back_to_first_mode(tmp_path):
    """An unrecognized [ui].home falls back to the first configured mode."""
    path = _home_config_path(tmp_path, "bogus")

    async def run():
        app = RadioTUI(path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.active_transport = None
            app.view = "active"
            app._open_home_view()
            await pilot.pause()
            # Falls back to a chat mode (the first configured transport).
            assert app.query_one("#main").current == "active-view"
            assert app.active_transport is not None

    asyncio.run(run())


def _js8_log_text(app):
    from textual.widgets import RichLog
    return "\n".join(s.text for s in app.query_one("#messages", RichLog).lines)


def test_js8_window_shows_all_messages_when_no_target(config_path):
    """JS8Call with no callsign/@group selected shows every js8call message."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Seed two separate js8call conversations.
            for call, body in (("W1AW", "hi there"), ("N0CALL", "yoyo")):
                m = UnifiedMessage(
                    sender=call,
                    content=body,
                    transport="js8call",
                    address_type=AddressType.DIRECT,
                    recipient="me",
                )
                m.transport = "js8call"
                await app.core.router._handle_inbound(m)
            app._select_mode("js8call")
            await pilot.pause()
            # No conversation is auto-selected for JS8...
            assert app.current_target is None
            # ...and the firehose shows BOTH conversations + a header.
            text = _js8_log_text(app)
            assert "all js8call messages" in text
            assert "hi there" in text and "yoyo" in text

    asyncio.run(run())


def test_js8_firehose_appends_live_messages(config_path):
    """New js8call traffic appends live to the no-conversation firehose."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            assert app.current_target is None
            m = UnifiedMessage(
                sender="KE7XYZ",
                content="CQ CQ",
                transport="js8call",
                address_type=AddressType.BROADCAST,
            )
            m.transport = "js8call"
            await app.core.router._handle_inbound(m)
            await pilot.pause()
            assert "CQ CQ" in _js8_log_text(app)

    asyncio.run(run())


def test_closing_conversation_returns_to_firehose(config_path):
    """Closing the open chat drops back to the all-messages firehose."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            m = UnifiedMessage(
                sender="W1AW",
                content="hello",
                transport="js8call",
                address_type=AddressType.DIRECT,
                recipient="me",
            )
            m.transport = "js8call"
            await app.core.router._handle_inbound(m)
            app.current_target = "W1AW"
            app._load_thread("W1AW")
            await pilot.pause()
            await app._handle_command("/close")
            await pilot.pause()
            assert app.current_target is None
            # The pane shows the firehose header again.
            assert "all js8call messages" in _js8_log_text(app)

    asyncio.run(run())


def test_to_command_preserves_case_per_transport(config_path):
    """/to uppercases HF callsigns but keeps MeshCore/Reticulum ids verbatim."""
    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # HF: callsigns are conventionally upper-cased.
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/to w1aw")
            assert app.current_target == "W1AW"
            # MeshCore: contact names + hex pubkey prefixes are case-sensitive.
            app._select_mode("meshcore")
            await pilot.pause()
            await app._handle_command("/to Alice")
            assert app.current_target == "Alice"
            await app._handle_command("/to a1b2C3d4e5f6")
            assert app.current_target == "a1b2C3d4e5f6"

    asyncio.run(run())


def _click_metas(app):
    """Collect every @click action attached to the last rendered message line."""
    from textual.widgets import RichLog
    log = app.query_one("#messages", RichLog)
    metas = []
    for seg in log.lines[-1]:
        meta = getattr(seg.style, "meta", None) or {}
        if "@click" in meta:
            metas.append((seg.text, meta["@click"]))
    return metas


def test_inbound_sender_renders_clickable_reply_link(config_path):
    """An inbound sender name carries a working @click=reply_to in the log."""
    from radio_app.core.message import DeliveryStatus

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            app.query_one("#messages").clear()
            app._render_message(UnifiedMessage(
                sender="a1b2c3d4e5f6",
                content="hi",
                transport="meshcore",
                address_type=AddressType.DIRECT,
                recipient="me",
                status=DeliveryStatus.RECEIVED,
            ))
            await pilot.pause()
            metas = _click_metas(app)
            # The sender segment is clickable and wired to reply_to(sender, mode).
            assert metas == [
                ("a1b2c3d4e5f6",
                 ("app.reply_to", ("a1b2c3d4e5f6", "meshcore"))),
            ]

    asyncio.run(run())


def test_outbound_and_anonymous_senders_are_not_clickable(config_path):
    """Your own ('you') and anonymous channel senders get no reply link."""
    from radio_app.core.message import DeliveryStatus

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("meshcore")
            await pilot.pause()
            messages = app.query_one("#messages")
            # Outbound (status != RECEIVED) renders as 'you' — not clickable.
            messages.clear()
            app._render_message(UnifiedMessage(
                sender="me", content="hello", transport="meshcore",
                address_type=AddressType.GROUP, group="0",
                status=DeliveryStatus.SENT,
            ))
            await pilot.pause()
            assert _click_metas(app) == []
            # Anonymous channel placeholder — not clickable.
            messages.clear()
            app._render_message(UnifiedMessage(
                sender="chan0", content="raw", transport="meshcore",
                address_type=AddressType.GROUP, group="0",
                recipient="me", status=DeliveryStatus.RECEIVED,
                metadata={"mc_anon": True},
            ))
            await pilot.pause()
            assert _click_metas(app) == []

    asyncio.run(run())


def test_watch_group_filter_cycles_and_filters(groups_config_path):
    """The Watch [g] filter cycles off -> each group -> off and filters rows."""
    from radio_app.core.message import DeliveryStatus

    async def run():
        app = RadioTUI(groups_config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_watch()
            await pilot.pause()
            # @TTP net, @TTPNE net, and an unrelated direct message.
            msgs = [
                UnifiedMessage.to_group(
                    "W1AW", "TTP", "ttp net", transport="js8call"
                ),
                UnifiedMessage.to_group(
                    "K2ABC", "TTPNE", "ne net", transport="js8call"
                ),
                UnifiedMessage(
                    sender="N0CALL", content="hi", transport="js8call",
                    recipient="me",
                ),
            ]
            for m in msgs:
                m.status = DeliveryStatus.RECEIVED
                app._append_monitor(m)
            await pilot.pause()
            assert len(app._monitor_entries) == 3  # no filter: all shown

            app._cycle_watch_group()                # -> first group (TTP)
            await pilot.pause()
            assert app._monitor_group_filter == "TTP"
            assert app._monitor_entries == [("@TTP", "js8call")]

            app._cycle_watch_group()                # -> TTPNE
            assert app._monitor_group_filter == "TTPNE"
            assert app._monitor_entries == [("@TTPNE", "js8call")]

            app._cycle_watch_group()                # -> off again
            assert app._monitor_group_filter is None
            assert len(app._monitor_entries) == 3

    asyncio.run(run())


def test_watch_group_and_fav_filters_mutually_exclusive(groups_config_path):
    """Selecting a group clears favorites-only and vice-versa."""
    async def run():
        app = RadioTUI(groups_config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_watch()
            await pilot.pause()
            # Favorites on, then pick a group -> favorites clears.
            app._toggle_fav_only()
            assert app._monitor_fav_only is True
            app._cycle_watch_group()
            assert app._monitor_group_filter == "TTP"
            assert app._monitor_fav_only is False
            # Favorites on again -> the group filter clears.
            app._toggle_fav_only()
            assert app._monitor_fav_only is True
            assert app._monitor_group_filter is None

    asyncio.run(run())


def test_cycle_watch_group_action_only_on_watch(groups_config_path):
    """The [g] group-filter action is enabled only on the Watch surface."""
    async def run():
        app = RadioTUI(groups_config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._show_watch()
            await pilot.pause()
            assert app.check_action("cycle_watch_group", ()) is True
            app._select_mode("js8call")
            await pilot.pause()
            assert app.check_action("cycle_watch_group", ()) is False

    asyncio.run(run())





