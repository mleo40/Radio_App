"""Tests for the minimal stdlib WebSocket client used for Pat's /ws stream.

Only the pure helpers (handshake + frame decoding) are tested here; the
networked ``stream`` coroutine is best-effort and exercised against a real Pat.
"""

from __future__ import annotations

import asyncio

from radio_app.transports import winlink_ws


def _text_frame(payload: bytes, masked: bool = False) -> bytes:
    out = bytearray()
    out.append(0x80 | 0x1)  # FIN + text opcode
    n = len(payload)
    flag = 0x80 if masked else 0x00
    if n < 126:
        out.append(flag | n)
    elif n < 65536:
        out.append(flag | 126)
        out += n.to_bytes(2, "big")
    else:
        out.append(flag | 127)
        out += n.to_bytes(8, "big")
    if masked:
        mask = b"\x01\x02\x03\x04"
        out += mask
        out += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return bytes(out)


def test_accept_key_matches_rfc6455_example():
    # The canonical example from RFC 6455 section 1.3.
    assert (
        winlink_ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==")
        == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    )


def test_build_handshake_has_required_headers():
    req = winlink_ws.build_handshake("127.0.0.1", 8080, "/ws", "KEY==").decode()
    assert req.startswith("GET /ws HTTP/1.1\r\n")
    assert "Upgrade: websocket\r\n" in req
    assert "Sec-WebSocket-Key: KEY==\r\n" in req
    assert "Sec-WebSocket-Version: 13\r\n" in req


def test_decode_single_unmasked_text_frame():
    frame = _text_frame(b'{"Ping":true}')
    frames, rest = winlink_ws.decode_frames(frame)
    assert rest == b""
    assert len(frames) == 1
    opcode, payload = frames[0]
    assert opcode == 0x1
    assert payload == b'{"Ping":true}'


def test_decode_masked_frame_is_unmasked():
    frame = _text_frame(b"hello", masked=True)
    frames, rest = winlink_ws.decode_frames(frame)
    assert rest == b""
    assert frames[0][1] == b"hello"


def test_decode_16bit_length_frame():
    payload = b"x" * 300
    frames, rest = winlink_ws.decode_frames(_text_frame(payload))
    assert rest == b""
    assert frames[0][1] == payload


def test_decode_multiple_frames_in_one_buffer():
    buf = _text_frame(b"one") + _text_frame(b"two")
    frames, rest = winlink_ws.decode_frames(buf)
    assert rest == b""
    assert [p for _, p in frames] == [b"one", b"two"]


def test_decode_partial_frame_returns_remaining():
    full = _text_frame(b"abcdef")
    frames, rest = winlink_ws.decode_frames(full[:4])  # truncated mid-frame
    assert frames == []
    assert rest == full[:4]  # nothing consumed; wait for more bytes
    # Feeding the rest completes it.
    frames2, rest2 = winlink_ws.decode_frames(rest + full[4:])
    assert rest2 == b""
    assert frames2[0][1] == b"abcdef"


async def _serve_ws(messages):
    """A throwaway loopback WebSocket server that pushes ``messages`` then closes."""
    async def handle(reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        key = ""
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip().decode()
        accept = winlink_ws.accept_key(key)
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        await writer.drain()
        for m in messages:
            writer.write(_text_frame(m))  # unmasked server->client frames
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_stream_connects_and_reads_server_frames():
    async def run():
        server = await _serve_ws(
            [b'{"Status":{"dialing":true}}', b'{"Progress":{"done":true}}']
        )
        host, port = server.sockets[0].getsockname()
        got: list[str] = []
        async with server:
            await winlink_ws.stream(host, port, "/ws", got.append, lambda: False)
        return got

    got = asyncio.run(run())
    assert got == ['{"Status":{"dialing":true}}', '{"Progress":{"done":true}}']
