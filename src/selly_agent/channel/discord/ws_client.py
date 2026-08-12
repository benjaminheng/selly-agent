"""A minimal RFC 6455 WebSocket client, hand-rolled: no third-party WebSocket library is
allowlisted (`tests/guard/test_stdlib_only.py`), and the Gateway is the one thing in this codebase
that needs one — long-poll has no Discord equivalent for receiving DMs.

This module is split in two: a pure frame codec (`encode_frame` / `decode_frame_header`, no
sockets — testable with no network at all) and, below it, the socket-owning `connect` handshake and
the `WebSocket` class that reads/writes frames over an already-open (optionally TLS-wrapped) socket.
Client frames are always masked (RFC 6455 §5.1); server frames never are.
"""

from __future__ import annotations

import os
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
