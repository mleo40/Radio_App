"""Minimal stdlib WebSocket client for Pat's ``/ws`` live-event stream.

Pat exposes a WebSocket at ``/ws`` that streams JSON events during a session
(``Status``, ``Progress``, ``Notification``, ``LogLine``, ...). We consume it to
give the operator live feedback while a (possibly minutes-long) HF session runs,
instead of a single "connecting..." then "done".

To stay dependency-free we implement just enough of RFC 6455: the client
handshake plus reading server frames. The pure helpers (:func:`accept_key`,
:func:`build_handshake`, :func:`decode_frames`) are unit-tested; the networked
:func:`stream` coroutine is best-effort and never raises into the caller's UI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import Callable

# RFC 6455 magic GUID used to derive the Sec-WebSocket-Accept value.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Frame opcodes we care about.
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8


def accept_key(client_key: str) -> str:
    """Compute the server's expected ``Sec-WebSocket-Accept`` for a client key."""
    digest = hashlib.sha1((client_key + _GUID).encode()).digest()  # noqa: S324
    return base64.b64encode(digest).decode()


def build_handshake(host: str, port: int, path: str, key: str) -> bytes:
    """Build the client HTTP Upgrade request that opens the WebSocket."""
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode()


def decode_frames(buf: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Decode as many complete frames as ``buf`` contains.

    Returns ``(frames, remaining)`` where ``frames`` is a list of
    ``(opcode, payload)`` and ``remaining`` is the unconsumed tail (a partial
    frame to be completed by more bytes). Handles 7/16/64-bit lengths and
    unmasks masked frames (servers normally send unmasked, but we cope either
    way).
    """
    frames: list[tuple[int, bytes]] = []
    i = 0
    n = len(buf)
    while True:
        if n - i < 2:
            break
        b0 = buf[i]
        b1 = buf[i + 1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        j = i + 2
        if length == 126:
            if n - j < 2:
                break
            length = int.from_bytes(buf[j:j + 2], "big")
            j += 2
        elif length == 127:
            if n - j < 8:
                break
            length = int.from_bytes(buf[j:j + 8], "big")
            j += 8
        mask = b""
        if masked:
            if n - j < 4:
                break
            mask = buf[j:j + 4]
            j += 4
        if n - j < length:
            break
        payload = bytearray(buf[j:j + length])
        if masked:
            for k in range(length):
                payload[k] ^= mask[k % 4]
        frames.append((opcode, bytes(payload)))
        i = j + length
    return frames, buf[i:]


async def stream(
    host: str,
    port: int,
    path: str,
    on_text: Callable[[str], None],
    should_stop: Callable[[], bool],
    *,
    connect_timeout: float = 5.0,
) -> None:
    """Open ``ws://host:port/path`` and call ``on_text`` for each text frame.

    Polls ``should_stop`` between reads so the caller can cancel cleanly when a
    session ends. Returns when the socket closes, ``should_stop`` is True, or on
    any connection error (logged by the caller, never raised through the UI).
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), connect_timeout
    )
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(build_handshake(host, port, path, key))
        await writer.drain()
        header = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), connect_timeout
        )
        status_line = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise ConnectionError(f"WebSocket upgrade failed: {status_line!r}")
        buf = b""
        while not should_stop():
            try:
                chunk = await asyncio.wait_for(reader.read(4096), 0.5)
            except TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk
            decoded, buf = decode_frames(buf)
            for opcode, payload in decoded:
                if opcode == _OP_CLOSE:
                    return
                if opcode in (_OP_TEXT, _OP_BINARY):
                    on_text(payload.decode("utf-8", "replace"))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

