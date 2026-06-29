"""Tests for inbound Winlink MIME body decoding (decode_winlink_body)."""

from __future__ import annotations

import base64

from radio_app.transports.winlink_transport import decode_winlink_body


def test_plain_text_passes_through_unchanged():
    assert decode_winlink_body("Just arrived") == "Just arrived"
    assert decode_winlink_body("see attached") == "see attached"
    assert decode_winlink_body("") == ""


def test_prose_mentioning_content_type_is_not_treated_as_mime():
    prose = "We discussed the content-type yesterday.\nIt was fine."
    assert decode_winlink_body(prose) == prose


def test_base64_single_part_is_decoded():
    payload = base64.b64encode(b"Hello from Winlink Express").decode()
    raw = (
        "Content-Type: text/plain; charset=utf-8\n"
        "Content-Transfer-Encoding: base64\n\n" + payload
    )
    assert decode_winlink_body(raw) == "Hello from Winlink Express"


def test_quoted_printable_is_decoded_with_charset():
    raw = (
        "Content-Type: text/plain; charset=utf-8\n"
        "Content-Transfer-Encoding: quoted-printable\n\n"
        "Caf=C3=A9 time=21"
    )
    assert decode_winlink_body(raw) == "Café time!"


def test_multipart_prefers_plain_text_over_html():
    raw = (
        "Content-Type: multipart/alternative; boundary=BB\n\n"
        "--BB\n"
        "Content-Type: text/plain\n\n"
        "Plain body wins\n"
        "--BB\n"
        "Content-Type: text/html\n\n"
        "<p>HTML body</p>\n"
        "--BB--\n"
    )
    assert decode_winlink_body(raw) == "Plain body wins"


def test_multipart_falls_back_to_stripped_html():
    raw = (
        "Content-Type: multipart/alternative; boundary=BB\n\n"
        "--BB\n"
        "Content-Type: text/html\n\n"
        "<html><body><b>Bold</b> &amp; <i>text</i></body></html>\n"
        "--BB--\n"
    )
    assert decode_winlink_body(raw) == "Bold & text"


def test_malformed_mime_returns_raw():
    # Looks like a header block but parsing yields no text -> return raw.
    raw = "Content-Transfer-Encoding: base64\n\n!!!not-valid-base64!!!"
    out = decode_winlink_body(raw)
    assert "!!!not-valid-base64!!!" in out

