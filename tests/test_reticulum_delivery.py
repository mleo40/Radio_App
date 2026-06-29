"""Tests for Reticulum delivery receipts and the Reticulum-mode tools.

These exercise the new outbound delivery telemetry and the identity/announce/
path helpers WITHOUT requiring RNS/LXMF or any radio hardware: the transport
module imports cleanly even when RNS is absent (the import is guarded), and the
delivery-emit path only builds a UnifiedMessage and calls the (stubbable)
loop-dispatch hook.
"""

from __future__ import annotations

from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.transports.reticulum_transport import ReticulumTransport


class _FakeLXM:
    """Stand-in for an LXMF.LXMessage (only identity matters for ref lookup)."""


def _capture_transport() -> tuple[ReticulumTransport, list[UnifiedMessage]]:
    t = ReticulumTransport({})
    captured: list[UnifiedMessage] = []
    t._dispatch_to_loop = captured.append  # type: ignore[assignment]
    return t, captured


def test_on_delivered_emits_delivery_telemetry():
    t, captured = _capture_transport()
    lxm = _FakeLXM()
    t._sent_refs[id(lxm)] = {"msg_id": "abc123", "recipient": "dead" * 8}
    t._on_delivered(lxm)
    assert len(captured) == 1
    md = captured[0].metadata
    assert md["kind"] == "delivery"
    assert md["status"] == "delivered"
    assert md["ref_msg_id"] == "abc123"
    assert md["recipient"] == "dead" * 8
    # The pending ref is consumed so a stray second callback is a no-op.
    t._on_delivered(lxm)
    assert len(captured) == 1


def test_on_failed_emits_failed_status():
    t, captured = _capture_transport()
    lxm = _FakeLXM()
    t._sent_refs[id(lxm)] = {"msg_id": "zzz", "recipient": "beef" * 8}
    t._on_failed(lxm)
    assert captured[0].metadata["status"] == "failed"
    assert captured[0].metadata["kind"] == "delivery"


def test_delivery_for_unknown_message_is_ignored():
    t, captured = _capture_transport()
    t._on_delivered(_FakeLXM())  # no matching sent ref
    assert captured == []


def test_reticulum_tools_graceful_when_not_running():
    """announce/path helpers must not raise when the transport isn't up."""
    t = ReticulumTransport({})
    assert t.announce_now() is False
    assert t.has_path("aa" * 16) is False
    assert t.request_path("aa" * 16) is False
    assert t.local_display_name() == ""


def test_local_display_name_reads_config():
    t = ReticulumTransport({"display_name": "lab-node"})
    assert t.local_display_name() == "lab-node"


def test_reticulum_advertises_attachment_support():
    assert ReticulumTransport({}).capabilities().supports_attachments is True


def test_read_attachments_reads_files_and_skips_missing(tmp_path):
    good = tmp_path / "report.txt"
    good.write_text("payload")
    t = ReticulumTransport({})
    files = t._read_attachments([str(good), str(tmp_path / "missing.bin")])
    assert files == [("report.txt", b"payload")]  # missing path skipped


def test_attachments_dir_honours_config(tmp_path):
    target = tmp_path / "rx"
    t = ReticulumTransport({"attachments_dir": str(target)})
    assert t.attachments_dir() == str(target)


def test_save_inbound_attachments_writes_and_deduplicates(tmp_path):
    import os

    import LXMF

    t = ReticulumTransport({"attachments_dir": str(tmp_path / "rx")})

    class _LXM:
        fields = {
            LXMF.FIELD_FILE_ATTACHMENTS: [
                ["a.pdf", b"first"],
                ["a.pdf", b"second"],   # same name -> de-duplicated on disk
            ]
        }

    names, saved = t._save_inbound_attachments(_LXM(), "src123")
    assert names == ["a.pdf", "a.pdf"]
    assert [os.path.basename(p) for p in saved] == ["a.pdf", "a-1.pdf"]
    assert all(os.path.exists(p) for p in saved)
    assert open(saved[0], "rb").read() == b"first"
    assert open(saved[1], "rb").read() == b"second"


def test_save_inbound_attachments_path_traversal_is_neutralised(tmp_path):
    import os

    import LXMF

    t = ReticulumTransport({"attachments_dir": str(tmp_path / "rx")})

    class _LXM:
        fields = {LXMF.FIELD_FILE_ATTACHMENTS: [["../../evil.sh", b"x"]]}

    names, saved = t._save_inbound_attachments(_LXM(), "src")
    # Reduced to a base name inside the attachments dir (no escaping it).
    assert names == ["evil.sh"]
    assert os.path.dirname(saved[0]) == str(tmp_path / "rx")


def test_save_inbound_attachments_none_when_no_field():
    t = ReticulumTransport({})

    class _LXM:
        fields: dict = {}

    assert t._save_inbound_attachments(_LXM(), "src") == ([], [])


def test_router_treats_delivery_as_telemetry(tmp_path):
    """A 'delivery' message must bypass storage/dedup but reach UI callbacks."""
    import asyncio

    from radio_app.core.filters import FilterEngine
    from radio_app.core.groups import GroupRegistry
    from radio_app.core.router import Router
    from radio_app.core.store import MessageStore

    store = MessageStore(str(tmp_path / "t.db"))
    groups = GroupRegistry({})
    router = Router(
        transports=[],
        store=store,
        groups=groups,
        filters=FilterEngine([], groups),
    )
    seen: list[UnifiedMessage] = []
    router.add_ui_callback(lambda m, a: seen.append(m))
    msg = UnifiedMessage(
        sender="reticulum",
        content="delivery delivered",
        address_type=AddressType.BROADCAST,
        transport="reticulum",
        metadata={"kind": "delivery", "status": "delivered", "ref_msg_id": "x1"},
    )
    asyncio.run(router._handle_inbound(msg))
    # Forwarded to the UI...
    assert seen and seen[0].metadata["ref_msg_id"] == "x1"
    # ...but never persisted (it isn't a conversation).
    assert store.threads() == []

