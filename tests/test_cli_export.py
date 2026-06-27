"""Export command: --thread/--all, --format json|txt|md|maildir, --out."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from radio_app.cli import main
from radio_app.core.message import AddressType, DeliveryStatus, UnifiedMessage
from radio_app.core.store import MessageStore


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db = tmp_path / "export.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndatabase = "{db}"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))

    store = MessageStore(db)
    base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    rows = [
        ("W1AW", "js8call", "TTP", "net starts at 1900", AddressType.GROUP, 0),
        ("KE0XYZ", "js8call", "TTP", "copy 73", AddressType.GROUP, 1),
        ("a1b2c3", "meshcore", None, "mesh relay up", AddressType.DIRECT, 2),
    ]
    for sender, tp, grp, content, at, doff in rows:
        store.save(
            UnifiedMessage(
                sender=sender,
                content=content,
                transport=tp,
                address_type=at,
                group=grp,
                recipient=None if at is AddressType.GROUP else "me",
                status=DeliveryStatus.RECEIVED,
                timestamp=base + timedelta(days=doff),
            )
        )
    store.close()
    return cfg


# -- txt format ----------------------------------------------------------------


def test_export_thread_txt_scopes_to_thread(seeded, capsys):
    assert main(["export", "--thread", "@TTP", "--format", "txt"]) == 0
    out = capsys.readouterr().out
    assert "net starts at 1900" in out
    assert "copy 73" in out
    assert "mesh relay up" not in out


def test_export_all_txt_includes_all_messages(seeded, capsys):
    assert main(["export", "--all", "--format", "txt"]) == 0
    out = capsys.readouterr().out
    assert "net starts at 1900" in out
    assert "copy 73" in out
    assert "mesh relay up" in out


def test_export_all_txt_annotates_thread(seeded, capsys):
    assert main(["export", "--all", "--format", "txt"]) == 0
    out = capsys.readouterr().out
    assert "{@TTP}" in out


def test_export_thread_txt_no_thread_annotation(seeded, capsys):
    assert main(["export", "--thread", "@TTP", "--format", "txt"]) == 0
    out = capsys.readouterr().out
    assert "{@TTP}" not in out


# -- json format ---------------------------------------------------------------


def test_export_thread_json(seeded, capsys):
    assert main(["export", "--thread", "@TTP", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and len(data) == 2
    assert data[0]["sender"] == "W1AW"
    assert data[1]["sender"] == "KE0XYZ"


def test_export_all_json(seeded, capsys):
    assert main(["export", "--all", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 3
    senders = {m["sender"] for m in data}
    assert senders == {"W1AW", "KE0XYZ", "a1b2c3"}


def test_export_json_has_expected_fields(seeded, capsys):
    assert main(["export", "--thread", "@TTP", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    msg = data[0]
    for field in ("msg_id", "sender", "content", "transport", "timestamp", "status"):
        assert field in msg


# -- md format -----------------------------------------------------------------


def test_export_thread_md_has_heading(seeded, capsys):
    assert main(["export", "--thread", "@TTP", "--format", "md"]) == 0
    out = capsys.readouterr().out
    assert "# @TTP" in out
    assert "**W1AW**" in out
    assert "net starts at 1900" in out


def test_export_all_md_has_per_thread_sections(seeded, capsys):
    assert main(["export", "--all", "--format", "md"]) == 0
    out = capsys.readouterr().out
    assert "# @TTP" in out
    assert "mesh relay up" in out


def test_export_md_groups_by_date(seeded, capsys):
    assert main(["export", "--thread", "@TTP", "--format", "md"]) == 0
    out = capsys.readouterr().out
    # Two messages on consecutive days → two ## date headers.
    assert "## 2026-06-01" in out
    assert "## 2026-06-02" in out


# -- maildir format ------------------------------------------------------------


def test_export_maildir_creates_directories(seeded, tmp_path):
    out_dir = tmp_path / "mbox"
    assert main(
        ["export", "--thread", "@TTP", "--format", "maildir", "--out", str(out_dir)]
    ) == 0
    assert (out_dir / "new").is_dir()
    assert (out_dir / "cur").is_dir()
    assert (out_dir / "tmp").is_dir()


def test_export_maildir_thread_writes_correct_count(seeded, tmp_path):
    out_dir = tmp_path / "mbox"
    assert main(
        ["export", "--thread", "@TTP", "--format", "maildir", "--out", str(out_dir)]
    ) == 0
    files = list((out_dir / "new").iterdir())
    assert len(files) == 2


def test_export_maildir_all_writes_all_messages(seeded, tmp_path):
    out_dir = tmp_path / "all_mbox"
    assert main(
        ["export", "--all", "--format", "maildir", "--out", str(out_dir)]
    ) == 0
    files = list((out_dir / "new").iterdir())
    assert len(files) == 3


def test_export_maildir_file_has_rfc_headers(seeded, tmp_path):
    out_dir = tmp_path / "mbox"
    main(["export", "--thread", "@TTP", "--format", "maildir", "--out", str(out_dir)])
    files = sorted((out_dir / "new").iterdir())
    text = files[0].read_text()
    assert text.startswith("From: ")
    assert "To:" in text
    assert "Date:" in text
    assert "Subject:" in text
    assert "Message-ID:" in text
    assert "X-RadioApp-Transport: js8call" in text
    assert "X-RadioApp-Thread: @TTP" in text


def test_export_maildir_requires_out(seeded, capsys):
    assert main(["export", "--all", "--format", "maildir"]) == 2
    assert "required" in capsys.readouterr().err


# -- --out writes to file ------------------------------------------------------


def test_export_txt_to_file(seeded, tmp_path):
    out_file = tmp_path / "history.txt"
    assert main(["export", "--all", "--format", "txt", "--out", str(out_file)]) == 0
    assert out_file.exists()
    content = out_file.read_text()
    assert "net starts at 1900" in content
    assert "mesh relay up" in content


def test_export_json_to_file(seeded, tmp_path):
    out_file = tmp_path / "history.json"
    assert main(
        ["export", "--thread", "@TTP", "--format", "json", "--out", str(out_file)]
    ) == 0
    data = json.loads(out_file.read_text())
    assert len(data) == 2


def test_export_md_to_file(seeded, tmp_path):
    out_file = tmp_path / "history.md"
    assert main(["export", "--all", "--format", "md", "--out", str(out_file)]) == 0
    assert "# @TTP" in out_file.read_text()


def test_export_file_summary_printed_to_stdout(seeded, tmp_path, capsys):
    out_file = tmp_path / "out.txt"
    main(["export", "--all", "--format", "txt", "--out", str(out_file)])
    out = capsys.readouterr().out
    assert "exported" in out and "message" in out


# -- edge cases ----------------------------------------------------------------


def test_export_empty_thread_txt(seeded, capsys):
    assert main(["export", "--thread", "NOBODY", "--format", "txt"]) == 0
    assert capsys.readouterr().out == ""


def test_export_empty_thread_json(seeded, capsys):
    assert main(["export", "--thread", "NOBODY", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_export_thread_and_all_are_mutually_exclusive(seeded):
    with pytest.raises(SystemExit) as exc:
        main(["export", "--thread", "@TTP", "--all"])
    assert exc.value.code != 0


def test_export_requires_thread_or_all(seeded):
    with pytest.raises(SystemExit) as exc:
        main(["export", "--format", "txt"])
    assert exc.value.code != 0
