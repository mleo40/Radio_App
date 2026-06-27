"""MeshCore transport protocol tests using a fake companion (no hardware).

These exercise the send mapping (direct contact / channel) and the inbound event
handlers (contact message, channel message, advert) without a real MeshCore
device by injecting a fake ``meshcore.MeshCore`` instance.
"""

from __future__ import annotations

import asyncio

import pytest

from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.transports.meshcore_transport import MeshCoreTransport

EventType = pytest.importorskip("meshcore").EventType


class _Event:
    def __init__(self, type_, payload=None):
        self.type = type_
        self.payload = payload or {}
        self.attributes = {}


class _FakeCommands:
    def __init__(self, result_type):
        self._result_type = result_type
        self.sent: list[tuple] = []
        self.chan_sent: list[tuple] = []
        self.adverts: list[bool] = []
        self.channels_set: list[tuple] = []

    async def send_msg(self, dst, msg):
        self.sent.append((dst, msg))
        return _Event(self._result_type)

    async def send_chan_msg(self, chan, msg):
        self.chan_sent.append((chan, msg))
        return _Event(self._result_type)

    async def send_advert(self, flood=False):
        self.adverts.append(flood)
        return _Event(self._result_type)

    async def get_bat(self):
        return _Event(EventType.BATTERY, {"level": 4100})

    async def set_channel(self, index, name, secret=None):
        self.channels_set.append((index, name, secret))
        return _Event(self._result_type)


class _FakeMC:
    def __init__(self, result_type=None, contacts=None, self_pub="aa" * 32):
        self.commands = _FakeCommands(result_type or EventType.MSG_SENT)
        self._contacts = contacts or {}
        self.self_info = {"public_key": self_pub, "name": "MeFi"}

    def get_contact_by_key_prefix(self, prefix):
        for pk, c in self._contacts.items():
            if pk.startswith(prefix) or prefix.startswith(pk[:12]):
                return c
        return None

    def get_contact_by_name(self, name):
        for c in self._contacts.values():
            if c.get("adv_name") == name:
                return c
        return None

    def is_connected(self):
        return True


def _running_transport(**mc_kwargs):
    t = MeshCoreTransport({"connection": "tcp"})
    t._mc = _FakeMC(**mc_kwargs)
    t._running = True
    return t


def _capture(t):
    received: list[UnifiedMessage] = []

    async def _cb(msg):
        received.append(msg)

    t.on_receive(_cb)
    return received


# -- sending -----------------------------------------------------------------


def test_send_direct_uses_contact_then_raw_hex():
    contact = {"public_key": "bb" * 32, "adv_name": "Bob"}
    t = _running_transport(contacts={"bb" * 32: contact})
    # Known contact resolves to the contact object.
    msg = UnifiedMessage.direct("me", "bbbbbbbbbbbb", "hi Bob")
    assert asyncio.run(t.send(msg)) is True
    assert t._mc.commands.sent[-1][0] is contact

    # Unknown but valid hex prefix falls back to the raw string.
    msg2 = UnifiedMessage.direct("me", "cccccccccccc", "hi stranger")
    assert asyncio.run(t.send(msg2)) is True
    assert t._mc.commands.sent[-1][0] == "cccccccccccc"


def test_send_direct_rejects_unusable_recipient():
    t = _running_transport()
    msg = UnifiedMessage.direct("me", "xy", "nope")  # too short / non-hex
    assert asyncio.run(t.send(msg)) is False


def test_capabilities_documented_message_size():
    """MeshCore caps the per-message payload at its documented 134 bytes."""
    caps = MeshCoreTransport({}).capabilities()
    assert caps.max_message_size == 134


def test_send_group_uses_channel_index():
    t = _running_transport()
    msg = UnifiedMessage.to_group("me", "2", "net in 5")
    assert asyncio.run(t.send(msg)) is True
    # Our node name is prepended so channel peers can attribute the message.
    assert t._mc.commands.chan_sent[-1] == (2, "MeFi: net in 5")
    # Non-numeric group falls back to the public channel 0.
    msg2 = UnifiedMessage.to_group("me", "EMS", "hello")
    asyncio.run(t.send(msg2))
    assert t._mc.commands.chan_sent[-1] == (0, "MeFi: hello")


def test_channel_wire_text_prepends_node_name():
    """Outbound channel text carries our node name (MeshCore convention)."""
    t = _running_transport()  # fake self_info name = "MeFi"
    assert t._channel_wire_text("hello") == "MeFi: hello"


def test_channel_wire_text_without_name_sends_raw():
    """With no known node name, channel text is sent unchanged."""
    t = _running_transport()
    t._mc.self_info = {"public_key": "aa" * 32}  # no 'name'
    assert t._channel_wire_text("hello") == "hello"
    # And a round-trip parses back to the same body via the named sender.
    sender, body, named = MeshCoreTransport._split_channel_sender("MeFi: hello")
    assert (sender, body, named) == ("MeFi", "hello", True)


def test_send_returns_false_on_error_event():
    t = _running_transport(result_type=EventType.ERROR)
    msg = UnifiedMessage.direct("me", "bbbbbbbbbbbb", "boom")
    assert asyncio.run(t.send(msg)) is False


def test_send_false_when_not_running():
    t = MeshCoreTransport({"connection": "tcp"})
    msg = UnifiedMessage.direct("me", "bbbbbbbbbbbb", "x")
    assert asyncio.run(t.send(msg)) is False


# -- receiving ---------------------------------------------------------------


def test_inbound_contact_message_maps_to_direct():
    t = _running_transport()
    received = _capture(t)
    ev = _Event(
        EventType.CONTACT_MSG_RECV,
        {"pubkey_prefix": "bbbbbbbbbbbb", "text": "hello there", "SNR": -7},
    )
    asyncio.run(t._on_contact_msg(ev))
    assert len(received) == 1
    m = received[0]
    assert m.address_type is AddressType.DIRECT
    assert m.sender == "bbbbbbbbbbbb"
    assert m.content == "hello there"
    assert m.transport == "meshcore"
    assert m.metadata["snr"] == -7


def test_inbound_channel_message_maps_to_group():
    t = _running_transport()
    received = _capture(t)
    ev = _Event(
        EventType.CHANNEL_MSG_RECV,
        {"channel_idx": 3, "text": "channel chatter"},
    )
    asyncio.run(t._on_channel_msg(ev))
    assert received[0].address_type is AddressType.GROUP
    assert received[0].group == "3"
    assert received[0].content == "channel chatter"
    # No "Name: " prefix -> anonymous channel sender (not a person to DM).
    assert received[0].metadata.get("mc_anon") is True
    assert received[0].sender == "chan3"


def test_inbound_channel_message_parses_embedded_sender_name():
    """MeshCore channels embed the sender as 'Name: text'; parse it out."""
    t = _running_transport()
    received = _capture(t)
    ev = _Event(
        EventType.CHANNEL_MSG_RECV,
        {"channel_idx": 0, "text": "Alice: hello everyone"},
    )
    asyncio.run(t._on_channel_msg(ev))
    m = received[0]
    assert m.address_type is AddressType.GROUP
    assert m.group == "0"
    # The sender is the embedded name (an addressable contact), and the body
    # has the prefix stripped.
    assert m.sender == "Alice"
    assert m.content == "hello everyone"
    assert m.metadata.get("display_name") == "Alice"
    assert "mc_anon" not in m.metadata


def test_split_channel_sender_is_conservative():
    """Only a 'name: ' (colon+space) prefix is treated as a sender."""
    split = MeshCoreTransport._split_channel_sender
    assert split("Alice: hi") == ("Alice", "hi", True)
    assert split("Bob Smith: multi word") == ("Bob Smith", "multi word", True)
    # No colon-space -> no name (URLs are safe).
    assert split("http://example.com")[2] is False
    assert split("just a message")[2] is False


def test_inbound_advert_is_announce_telemetry():
    contact = {"public_key": "dd" * 32, "adv_name": "RepeaterX"}
    t = _running_transport(contacts={"dd" * 32: contact})
    received = _capture(t)
    ev = _Event(EventType.ADVERTISEMENT, {"public_key": "dd" * 32})
    asyncio.run(t._on_advert(ev))
    assert received[0].metadata["kind"] == "announce"
    assert received[0].metadata["display_name"] == "RepeaterX"


def test_empty_inbound_text_is_ignored():
    t = _running_transport()
    received = _capture(t)
    asyncio.run(t._on_contact_msg(_Event(EventType.CONTACT_MSG_RECV, {"text": ""})))
    asyncio.run(t._on_channel_msg(_Event(EventType.CHANNEL_MSG_RECV, {"text": ""})))
    assert received == []


def test_local_identity_is_self_pubkey():
    t = _running_transport(self_pub="ee" * 32)
    assert t.local_identity() == "ee" * 32


def test_local_display_name_is_node_name():
    t = _running_transport()
    assert t.local_display_name() == "MeFi"


def test_check_reachable_ok_when_connected():
    from radio_app.transports.base import ReachabilityStatus

    t = _running_transport()
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.OK


def test_self_prefix_truncates(_unused=None):
    t = _running_transport(self_pub="ff" * 32)
    assert t._self_prefix() == "ff" * 6  # first 12 hex chars


# -- channels ----------------------------------------------------------------


class _ChanMC:
    """Fake companion whose ``commands.get_channel`` reports a fixed name map."""

    def __init__(self, names: dict[int, str]):
        names_ = names

        class _Cmds:
            async def get_channel(self, idx):
                if idx in names_:
                    return _Event(
                        EventType.CHANNEL_INFO,
                        {"channel_idx": idx, "channel_name": names_[idx]},
                    )
                return _Event(EventType.ERROR)

        self.commands = _Cmds()


def test_parse_config_channels_accepts_dicts_ints_and_strings():
    t = MeshCoreTransport(
        {"connection": "tcp", "channels": [{"index": 1, "name": "ops"}, "2:net", 5]}
    )
    by = {c["index"]: c["name"] for c in t._config_channels}
    assert by == {1: "ops", 2: "net", 5: ""}


def test_channels_merges_config_and_device_with_public_always():
    t = MeshCoreTransport(
        {"connection": "tcp", "channels": [{"index": 1, "name": "ops"}, "2:net", 5]}
    )
    # Device reports the (unnamed) public channel and a named one.
    t._device_channels = [{"index": 0, "name": ""}, {"index": 3, "name": "field"}]
    chans = t.channels()
    by = {c["index"]: c["name"] for c in chans}
    assert by[0] == ""        # public channel always present
    assert by[1] == "ops"     # config-named
    assert by[2] == "net"     # "<idx>:<name>" string form
    assert by[3] == "field"   # device-discovered
    assert by[5] == ""        # bare int, no name
    # Stable, index-sorted ordering.
    assert [c["index"] for c in chans] == [0, 1, 2, 3, 5]


def test_channels_default_is_just_public():
    t = MeshCoreTransport({"connection": "tcp"})
    assert t.channels() == [{"index": 0, "name": ""}]


def test_fetch_channels_keeps_named_and_public_skips_empty_slots():
    t = MeshCoreTransport({"connection": "tcp"})
    t._mc = _ChanMC({0: "", 2: "ops", 4: "field"})
    found = asyncio.run(t._fetch_channels())
    by = {c["index"]: c["name"] for c in found}
    assert by[0] == ""        # public kept even though unnamed
    assert by[2] == "ops"
    assert by[4] == "field"
    assert 1 not in by        # unconfigured slot (ERROR) skipped
    assert 3 not in by


# -- announce (advert) + device telemetry ------------------------------------


def test_send_advert_zero_hop_and_flood():
    t = _running_transport()
    assert asyncio.run(t.send_advert()) is True
    assert t._mc.commands.adverts == [False]          # zero-hop by default
    assert asyncio.run(t.send_advert(flood=True)) is True
    assert t._mc.commands.adverts == [False, True]    # flood propagation


def test_send_advert_false_when_not_running():
    t = MeshCoreTransport({"connection": "tcp"})
    assert asyncio.run(t.send_advert()) is False


def test_send_advert_false_on_error_event():
    t = _running_transport(result_type=EventType.ERROR)
    assert asyncio.run(t.send_advert()) is False


def test_device_telemetry_reports_radio_and_battery():
    t = _running_transport()
    # Enrich self_info with radio params as the device would report them.
    t._mc.self_info = {
        "public_key": "ab" * 32,
        "name": "FieldNode",
        "radio_freq": 915.0,
        "radio_bw": 250.0,
        "radio_sf": 10,
        "radio_cr": 5,
        "tx_power": 22,
        "max_tx_power": 30,
    }
    tel = asyncio.run(t.device_telemetry())
    assert tel["name"] == "FieldNode"
    assert tel["radio_freq"] == 915.0
    assert tel["radio_sf"] == 10
    assert tel["tx_power"] == 22
    assert tel["battery"] == 4100   # from the fake get_bat


def test_device_telemetry_empty_when_not_running():
    t = MeshCoreTransport({"connection": "tcp"})
    assert asyncio.run(t.device_telemetry()) == {}


# -- channel management (add / name) -----------------------------------------


def test_name_channel_updates_config_channels_and_display():
    t = MeshCoreTransport({"connection": "tcp"})
    t.name_channel(2, "#ops")          # leading '#' stripped for the label
    assert t.config_channels() == [{"index": 2, "name": "ops"}]
    assert {c["index"]: c["name"] for c in t.channels()}[2] == "ops"
    # Re-naming the same index replaces (not duplicates) it.
    t.name_channel(2, "field")
    assert t.config_channels() == [{"index": 2, "name": "field"}]
    # Empty name removes the local label.
    t.name_channel(2, "")
    assert t.config_channels() == []


def test_create_hashtag_channel_passes_name_verbatim_no_secret():
    t = _running_transport()
    # A hashtag channel needs no secret; the '#' must be preserved so the
    # firmware derives the key from the name.
    assert asyncio.run(t.create_channel(3, "#general")) is True
    assert t._mc.commands.channels_set[-1] == (3, "#general", None)
    # The device-side channel cache now exposes it (display name without '#').
    assert {c["index"]: c["name"] for c in t.channels()}[3] == "general"


def test_create_channel_with_hex_secret():
    t = _running_transport()
    secret = "00112233445566778899aabbccddeeff"  # 16 bytes
    assert asyncio.run(t.create_channel(1, "ops", secret)) is True
    idx, name, passed = t._mc.commands.channels_set[-1]
    assert (idx, name) == (1, "ops")
    assert passed == bytes.fromhex(secret)


def test_create_channel_false_when_not_running():
    t = MeshCoreTransport({"connection": "tcp"})
    assert asyncio.run(t.create_channel(1, "#ops")) is False


def test_create_channel_false_on_error_event():
    t = _running_transport(result_type=EventType.ERROR)
    assert asyncio.run(t.create_channel(1, "#ops")) is False


# -- channel restore after restart -------------------------------------------


def test_name_channel_persists_secret():
    t = MeshCoreTransport({"connection": "tcp"})
    t.name_channel(1, "ops", "00112233445566778899aabbccddeeff")
    assert t.config_channels() == [
        {"index": 1, "name": "ops", "secret": "00112233445566778899aabbccddeeff"}
    ]


def test_parse_config_channels_reads_secret_and_hashtag_flag():
    t = MeshCoreTransport(
        {
            "connection": "tcp",
            "channels": [
                {"index": 1, "name": "ops"},                       # hashtag (default)
                {"index": 2, "name": "sec", "secret": "ab" * 16},  # secret channel
                {"index": 3, "name": "lbl", "hashtag": False},     # label-only
            ],
        }
    )
    by = {c["index"]: c for c in t._config_channels}
    assert by[1] == {"index": 1, "name": "ops"}
    assert by[2]["secret"] == "ab" * 16
    assert by[3]["hashtag"] is False


def test_restore_recreates_hashtag_and_secret_channels_on_device():
    # Saved channels (e.g. loaded from config after a restart).
    t = _running_transport()
    t._config_channels = [
        {"index": 0, "name": ""},                          # public: never recreated
        {"index": 1, "name": "ops"},                       # legacy hashtag (no flag)
        {"index": 2, "name": "sec", "secret": "ab" * 16},  # secret channel
        {"index": 3, "name": "lbl", "hashtag": False},     # label-only: skip
    ]
    asyncio.run(t._restore_config_channels())
    sent = t._mc.commands.channels_set
    # Hashtag channel re-derived from '#name'; secret channel re-applied; the
    # public and label-only entries are left untouched.
    assert (1, "#ops", None) in sent
    assert (2, "ops", bytes.fromhex("ab" * 16)) not in sent  # name is 'sec', not 'ops'
    assert any(idx == 2 and name == "sec" for idx, name, _ in sent)
    assert all(idx not in (0, 3) for idx, _, _ in sent)

