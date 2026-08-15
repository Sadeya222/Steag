"""Payload framing / RS tests."""

import pytest

from stegstr.payload import PayloadCodec, PayloadCorrupt


def test_roundtrip_various_sizes():
    codec = PayloadCodec()
    for n in (0, 1, 100, 190, 191, 192, 500, 4096):
        msg = bytes(i % 251 for i in range(n))
        stream = codec.encode(msg)
        assert len(stream) % 255 == 0
        assert codec.max_message_bytes(len(stream) * 8) >= len(msg)
        out, corrected = codec.decode(stream)
        assert out == msg
        assert corrected == 0


def test_rs_corrects_byte_errors():
    codec = PayloadCodec()
    msg = b"the quick brown fox" * 20
    stream = codec.encode(msg)
    b = bytearray(stream)
    # 20 byte errors spread across 4 codewords (each tolerates 32)
    for i in range(20):
        b[i * 31 % len(b)] ^= 0xFF
    out, corrected = codec.decode(bytes(b))
    assert out == msg
    assert corrected == 20


def test_uncorrectable_raises():
    codec = PayloadCodec()
    msg = b"x" * 300
    stream = codec.encode(msg)
    b = bytearray(stream)
    for i in range(0, len(b)):
        b[i] ^= 0xFF  # destroy everything
    with pytest.raises(PayloadCorrupt):
        codec.decode(bytes(b))


def test_garbage_rejected():
    codec = PayloadCodec()
    import os
    with pytest.raises(PayloadCorrupt):
        codec.decode(os.urandom(510))
    with pytest.raises(PayloadCorrupt):
        codec.decode(b"\x00" * 255)


def test_crc_detects_tampering():
    codec = PayloadCodec()
    msg = b"secret message"
    stream = codec.encode(msg)
    b = bytearray(stream)
    b[5] ^= 0x01
    # exceed RS's 32-byte-per-codeword correction power so CRC must catch it
    for i in range(40, 100):
        b[i] ^= 0xFF
    with pytest.raises(PayloadCorrupt):
        codec.decode(bytes(b))
