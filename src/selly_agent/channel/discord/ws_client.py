"""A minimal RFC 6455 WebSocket client, hand-rolled: no third-party WebSocket library is
allowlisted (`tests/guard/test_stdlib_only.py`), and the Gateway is the one thing in this codebase
that needs one — long-poll has no Discord equivalent for receiving DMs.

This module is split in two: a pure frame codec (`encode_frame` / `decode_frame_header`, no
sockets — testable with no network at all) and, below it, the socket-owning `connect` handshake and
the `WebSocket` class that reads/writes frames over an already-open (optionally TLS-wrapped) socket.
Client frames are always masked (RFC 6455 §5.1); server frames never are.
"""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import ssl
from dataclasses import dataclass

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


@dataclass(frozen=True)
class Frame:
    fin: bool
    opcode: int
    payload: bytes


def _mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def encode_frame(opcode: int, payload: bytes = b"", *, fin: bool = True) -> bytes:
    """One frame ready to write to the socket. Always masked, per the client's obligation."""
    first_byte = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    if length <= 125:
        header = bytes([first_byte, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytes([first_byte, 0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first_byte, 0x80 | 127]) + length.to_bytes(8, "big")
    key = os.urandom(4)
    return header + key + _mask(payload, key)


def decode_frame_header(data: bytes) -> tuple[Frame, int] | None:
    """Parse one frame from the front of `data`. Returns `(frame, bytes_consumed)`, or `None` when
    `data` doesn't yet hold a complete frame (the caller should read more and retry) — pure, so the
    reassembly-from-a-stream loop lives in `_recv_frame` (Task 3), not here."""
    if len(data) < 2:
        return None
    first_byte, second_byte = data[0], data[1]
    fin = bool(first_byte & 0x80)
    opcode = first_byte & 0x0F
    masked = bool(second_byte & 0x80)
    length = second_byte & 0x7F
    offset = 2
    if length == 126:
        if len(data) < offset + 2:
            return None
        length = int.from_bytes(data[offset : offset + 2], "big")
        offset += 2
    elif length == 127:
        if len(data) < offset + 8:
            return None
        length = int.from_bytes(data[offset : offset + 8], "big")
        offset += 8
    mask_key = None
    if masked:
        if len(data) < offset + 4:
            return None
        mask_key = data[offset : offset + 4]
        offset += 4
    if len(data) < offset + length:
        return None
    payload = data[offset : offset + length]
    if masked:
        payload = _mask(payload, mask_key)
    offset += length
    return Frame(fin=fin, opcode=opcode, payload=payload), offset


# Socket-owning parts (Task 3): handshake and WebSocket class

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class HandshakeError(Exception):
    """The opening handshake did not complete — a non-101 response, or a Sec-WebSocket-Accept
    that doesn't match what RFC 6455 says the server must return for our key."""


class ConnectionClosed(Exception):
    """The peer sent a close frame, or the socket dropped mid-read."""


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionClosed("socket closed while reading a frame")
        buf += chunk
    return bytes(buf)


def _recv_frame(sock) -> Frame:
    """Block for exactly one complete frame. Safe to call only once the socket is known-readable
    (see `WebSocket.wait_readable`) — reading the header is cheap, but a frame's payload can still
    arrive across several TCP segments, which `_recv_exact` waits out."""
    header = _recv_exact(sock, 2)
    first_byte, second_byte = header[0], header[1]
    length = second_byte & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(sock, 8), "big")
    masked = bool(second_byte & 0x80)
    mask_key = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = _mask(payload, mask_key)
    return Frame(fin=bool(first_byte & 0x80), opcode=first_byte & 0x0F, payload=payload)


def _read_http_response_headers(sock) -> dict:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            raise HandshakeError("connection closed during the opening handshake")
        buf += chunk
    head, _, _ = buf.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    if " 101 " not in lines[0]:
        raise HandshakeError(f"unexpected handshake status line: {lines[0]!r}")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


class WebSocket:
    """A connected client: send/receive text frames over an already-open socket. Ping frames are
    answered transparently inside `recv_text`; a close frame raises `ConnectionClosed` rather than
    being handed to the caller as data, since neither Discord message type it exists to deliver
    (Gateway payloads, our own outbound JSON) is ever binary or fragmented in a way this needs to
    expose fragment boundaries for."""

    def __init__(self, sock):
        self._sock = sock

    def send_text(self, text: str) -> None:
        self._sock.sendall(encode_frame(OPCODE_TEXT, text.encode("utf-8")))

    def recv_text(self) -> str:
        chunks: list[bytes] = []
        while True:
            frame = _recv_frame(self._sock)
            if frame.opcode == OPCODE_PING:
                self._sock.sendall(encode_frame(OPCODE_PONG, frame.payload))
                continue
            if frame.opcode == OPCODE_PONG:
                continue
            if frame.opcode == OPCODE_CLOSE:
                raise ConnectionClosed(f"peer closed the connection: {frame.payload[:2]!r}")
            chunks.append(frame.payload)
            if frame.fin:
                break
        return b"".join(chunks).decode("utf-8")

    def wait_readable(self, timeout: float) -> bool:
        """True once at least one byte is waiting — lets the Gateway session race "a message
        arrived" against "the heartbeat is due" without ever starting a blocking read that a
        timeout could interrupt mid-frame. Checks SSL's internal buffer first: select() only
        reflects OS-level fd readability of the encrypted stream, so a TLS record that decrypted
        into more application bytes than a prior read consumed can leave data sitting in OpenSSL's
        buffer while the underlying fd goes quiet — select() alone would wrongly say "not
        readable" and delay dispatch of an already-received message."""
        if hasattr(self._sock, "pending") and self._sock.pending() > 0:
            return True
        ready, _, _ = select.select([self._sock], [], [], timeout)
        return bool(ready)

    def close(self, code: int = 1000) -> None:
        try:
            self._sock.sendall(encode_frame(OPCODE_CLOSE, code.to_bytes(2, "big")))
        except OSError:
            pass
        self._sock.close()


def connect(
    host: str, port: int, path: str, *, use_tls: bool = True, timeout: float = 10.0
) -> WebSocket:
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if use_tls else raw
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    headers = _read_http_response_headers(sock)
    digest = hashlib.sha1((key + _GUID).encode("ascii")).digest()
    expected = base64.b64encode(digest).decode("ascii")
    if headers.get("sec-websocket-accept") != expected:
        sock.close()
        raise HandshakeError("Sec-WebSocket-Accept did not match the expected value")
    sock.settimeout(None)  # handshake used the connect timeout; the session loop times reads itself
    return WebSocket(sock)
