"""The RFC 6455 frame codec — pure, no sockets. `decode_frame_header` is checked against the RFC's
own published masked-"Hello" test vector; `encode_frame` is checked by round-tripping through the
decoder, since encoding uses a random per-frame mask key and so isn't byte-for-byte deterministic.
"""

from __future__ import annotations

from selly_agent.channel.discord.ws_client import (
    OPCODE_BINARY,
    OPCODE_CLOSE,
    OPCODE_TEXT,
    decode_frame_header,
    encode_frame,
)

# RFC 6455 §5.7: a single-frame masked text message "Hello".
_RFC_HELLO_FRAME = bytes.fromhex("818537fa213d7f9f4d5158")


def test_decode_matches_the_rfc_published_vector() -> None:
    frame, consumed = decode_frame_header(_RFC_HELLO_FRAME)
    assert consumed == len(_RFC_HELLO_FRAME)
    assert frame.fin is True
    assert frame.opcode == OPCODE_TEXT
    assert frame.payload == b"Hello"


def test_decode_header_reports_none_on_incomplete_input() -> None:
    # Only the first byte of a well-formed frame — not enough to know the length yet.
    assert decode_frame_header(_RFC_HELLO_FRAME[:1]) is None


def test_encode_round_trips_through_decode() -> None:
    encoded = encode_frame(OPCODE_TEXT, b"Hello")
    frame, consumed = decode_frame_header(encoded)
    assert consumed == len(encoded)
    assert frame.fin is True
    assert frame.opcode == OPCODE_TEXT
    assert frame.payload == b"Hello"


def test_encode_always_masks_client_frames() -> None:
    # Byte 1's high bit (MASK) must be set on every frame a client sends.
    encoded = encode_frame(OPCODE_TEXT, b"x")
    assert encoded[1] & 0x80


def test_encode_extended_length_16_bit() -> None:
    payload = b"x" * 200  # > 125, < 65536 -> the 126 + 2-byte-length case
    encoded = encode_frame(OPCODE_BINARY, payload)
    frame, consumed = decode_frame_header(encoded)
    assert consumed == len(encoded)
    assert frame.payload == payload


def test_encode_extended_length_64_bit() -> None:
    payload = b"x" * 70000  # > 65535 -> the 127 + 8-byte-length case
    encoded = encode_frame(OPCODE_BINARY, payload)
    frame, consumed = decode_frame_header(encoded)
    assert consumed == len(encoded)
    assert frame.payload == payload


def test_close_frame_carries_a_status_code() -> None:
    encoded = encode_frame(OPCODE_CLOSE, (1000).to_bytes(2, "big"))
    frame, _ = decode_frame_header(encoded)
    assert frame.opcode == OPCODE_CLOSE
    assert int.from_bytes(frame.payload[:2], "big") == 1000


def test_fin_zero_is_preserved_for_fragmentation() -> None:
    encoded = encode_frame(OPCODE_TEXT, b"part one", fin=False)
    frame, _ = decode_frame_header(encoded)
    assert frame.fin is False
