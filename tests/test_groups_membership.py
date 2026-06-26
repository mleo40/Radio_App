"""Cross-mode group membership + reverse lookup (read-side aggregation)."""

from __future__ import annotations

import asyncio

import pytest

from radio_app.config import Config
from radio_app.core.filters import FilterAction, FilterEngine, FilterRule
from radio_app.core.groups import Group, GroupMember, GroupRegistry
from radio_app.core.message import UnifiedMessage
from radio_app.core.router import Router
from radio_app.core.store import MessageStore
from radio_app.transports.base import Transport, TransportCapabilities

# -- member matching --------------------------------------------------------


def test_member_parse_with_transport():
    m = GroupMember.parse("js8call:KE0XYZ")
    assert m.transport == "js8call" and m.identifier == "KE0XYZ"
    assert m.spec == "js8call:KE0XYZ"


def test_member_parse_bare_identifier_matches_any_transport():
    m = GroupMember.parse("KE0XYZ")
    assert m.transport == "" and m.identifier == "KE0XYZ"
    assert m.matches("js8call", "ke0xyz")
    assert m.matches("meshcore", "KE0XYZ")  # any transport


def test_member_transport_scoping():
    m = GroupMember.parse("js8call:KE0XYZ")
    assert m.matches("js8call", "KE0XYZ")
    assert not m.matches("meshcore", "KE0XYZ")  # wrong transport


def test_member_hex_prefix_tolerant():
    full = "ab" * 16
    m = GroupMember.parse(f"reticulum:{full}")
    # stored full, announce surfaced short prefix
    assert m.matches("reticulum", full[:12])
    # stored short, announce full (other direction)
    m2 = GroupMember.parse(f"reticulum:{full[:12]}")
    assert m2.matches("reticulum", full)


def test_member_callsign_case_insensitive_exact():
    m = GroupMember.parse("js8call:KE0XYZ")
    assert m.matches("js8call", "ke0xyz")
    assert not m.matches("js8call", "KE0XY")  # not a hex => exact only


# -- registry reverse lookup ------------------------------------------------


def _registry():
    ttp = Group(
        name="ttp",
        display_name="Tactical Training",
        members=[
            GroupMember.parse("js8call:KE0XYZ"),
            GroupMember.parse("meshcore:a1b2c3d4e5f6"),
            GroupMember.parse("reticulum:" + "ff" * 16),
        ],
        tags=["ttp"],
    )
    return GroupRegistry({"ttp": ttp})


def test_groups_for_sender():
    reg = _registry()
    assert reg.groups_for("js8call", "KE0XYZ") == ["ttp"]
    assert reg.groups_for("meshcore", "a1b2c3d4e5f6") == ["ttp"]
    assert reg.groups_for("reticulum", "ff" * 16) == ["ttp"]
    assert reg.groups_for("js8call", "NOBODY") == []


def test_groups_for_message_by_member():
    reg = _registry()
    msg = UnifiedMessage(sender="KE0XYZ", content="hi", transport="js8call")
    assert reg.groups_for_message(msg) == ["ttp"]


def test_groups_for_message_by_native_group_tag():
    reg = _registry()
    msg = UnifiedMessage.to_group("W1AW", "TTP", "net starting", transport="js8call")
    assert reg.groups_for_message(msg) == ["ttp"]


def test_group_claims_its_own_name_without_explicit_tag():
    # A group with no declared tags still captures native traffic under its name.
    reg = GroupRegistry({"ttp": Group(name="ttp")})
    msg = UnifiedMessage.to_group("W1AW", "TTP", "hi", transport="js8call")
    assert reg.groups_for_message(msg) == ["ttp"]


def test_groups_for_message_by_winlink_metadata_tag():
    reg = _registry()
    msg = UnifiedMessage(
        sender="W1AW", content="email body", transport="winlink",
        metadata={"tag": "ttp"},
    )
    assert reg.groups_for_message(msg) == ["ttp"]


def test_groups_for_message_dedupes_member_and_tag():
    reg = _registry()
    # KE0XYZ is a member AND the message carries the ttp tag — still one entry.
    msg = UnifiedMessage.to_group("KE0XYZ", "TTP", "hi", transport="js8call")
    assert reg.groups_for_message(msg) == ["ttp"]


def test_unmatched_message_has_no_groups():
    reg = _registry()
    msg = UnifiedMessage(sender="ZZ9ZZ", content="x", transport="js8call")
    assert reg.groups_for_message(msg) == []


# -- mutation + persistence -------------------------------------------------


def test_add_and_remove_member_and_save(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")  # empty config
    cfg = Config.load(cfg_path)
    reg = GroupRegistry.from_config(cfg)

    reg.add_member("ttp", "js8call:KE0XYZ")
    reg.add_member("ttp", "meshcore:a1b2c3d4e5f6")
    reg.add_tag("ttp", "ttp")
    reg.save(cfg)

    # Reload from disk and confirm round-trip.
    reloaded = GroupRegistry.from_config(Config.load(cfg_path))
    g = reloaded.get("ttp")
    assert g is not None
    assert set(g.member_specs()) == {"js8call:KE0XYZ", "meshcore:a1b2c3d4e5f6"}
    assert g.tags == ["ttp"]

    # Remove a member, persist, reload.
    assert reloaded.remove_member("ttp", "js8call:KE0XYZ")
    reloaded.save(cfg)
    again = GroupRegistry.from_config(Config.load(cfg_path))
    assert again.get("ttp").member_specs() == ["meshcore:a1b2c3d4e5f6"]


def test_add_member_is_idempotent():
    reg = GroupRegistry({})
    reg.add_member("ttp", "js8call:KE0XYZ")
    reg.add_member("ttp", "js8call:ke0xyz")  # same, different case
    assert len(reg.get("ttp").members) == 1


def test_remove_group():
    reg = _registry()
    assert reg.remove_group("ttp")
    assert reg.get("ttp") is None
    assert not reg.remove_group("ttp")


# -- router stamping --------------------------------------------------------


class _InboundTransport(Transport):
    name = "js8call"

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=0,
            supports_addressing=True,
            supports_groups=True,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: UnifiedMessage) -> bool:
        return True

    async def receive(self, msg: UnifiedMessage) -> None:
        await self._emit(msg)


@pytest.fixture()
def store(tmp_path):
    s = MessageStore(tmp_path / "groups.db")
    yield s
    s.close()


def test_router_stamps_inbound_with_groups(store):
    reg = _registry()
    filters = FilterEngine([FilterRule(action=FilterAction.SHOW)], reg)
    t = _InboundTransport({})
    router = Router([t], store, reg, filters)

    seen: list[UnifiedMessage] = []
    router.add_ui_callback(lambda m, a: seen.append(m))

    async def run():
        await t.start()
        await t.receive(
            UnifiedMessage(sender="KE0XYZ", content="hello net", transport="js8call")
        )

    asyncio.run(run())

    assert seen, "ui callback should have fired"
    assert seen[-1].groups == ["ttp"]
    # And it persisted with the stamp.
    rows = store.read_thread("KE0XYZ")
    assert rows and rows[-1].groups == ["ttp"]


def test_router_leaves_ungrouped_message_unstamped(store):
    reg = _registry()
    filters = FilterEngine([FilterRule(action=FilterAction.SHOW)], reg)
    t = _InboundTransport({})
    router = Router([t], store, reg, filters)
    seen: list[UnifiedMessage] = []
    router.add_ui_callback(lambda m, a: seen.append(m))

    async def run():
        await t.start()
        await t.receive(
            UnifiedMessage(sender="NOBODY", content="x", transport="js8call")
        )

    asyncio.run(run())
    assert seen[-1].groups == []


