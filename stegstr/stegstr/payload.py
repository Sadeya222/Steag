"""Payload framing: magic + version + length + CRC32, protected by Reed-Solomon.

Design points (mirrors the architecture doc):
- FEC *before* embedding: the byte stream that goes into the image is already
  RS(255, 191, 64) encoded, so the stego layer only has to survive bit errors
  that recompression introduces; RS fixes up to 32 wrong bytes per codeword.
- The stream is padded to whole codewords *and* whole 255-byte RS blocks, so
  the decoder can slice it into codewords purely from the image geometry
  (no framing metadata inside the image payload region).
- The 8-byte magic + CRC32 let the receiver confirm a clean decode vs.
  corrupted bits, and reject false positives (e.g. a plain photo).
"""

from __future__ import annotations

import struct
import zlib

from reedsolo import RSCodec, ReedSolomonError

MAGIC = b"STEGSTR1"  # 8 bytes
VERSION = 1
NSYM = 64
NSIZE = 255
DATA_CHUNK = NSIZE - NSYM  # 191
OVERHEAD = len(MAGIC) + 1 + 4 + 4  # magic + version + length + crc = 17


class PayloadError(Exception):
    """Raised when a payload stream cannot be framed or deframed."""


class PayloadTooLarge(PayloadError):
    """The message does not fit in the requested carrier capacity."""


class PayloadCorrupt(PayloadError):
    """The stream decoded but failed integrity checks (magic/CRC/RS)."""


class PayloadCodec:
    """Frames and deframes messages with Reed-Solomon protection."""

    def __init__(self, nsym: int = NSYM):
        self.nsym = nsym
        self.rs = RSCodec(nsym)

    # -- framing -------------------------------------------------------------
    def encode(self, message: bytes) -> bytes:
        """Message -> RS-protected byte stream (length is a multiple of 255)."""
        body = (
            struct.pack(">I", len(message))
            + struct.pack(">I", zlib.crc32(message))
            + message
        )
        frame = MAGIC + bytes([VERSION]) + body
        pad = (-len(frame)) % DATA_CHUNK
        frame += b"\x00" * pad
        stream = bytes(self.rs.encode(frame))
        assert len(stream) % NSIZE == 0
        return stream

    def stream_bit_length(self, message: bytes) -> int:
        """Number of payload bits (i.e. RS stream bits) for a message."""
        return len(self.encode(message)) * 8

    def max_message_bytes(self, stream_bits: int) -> int:
        """Largest message fitting in ``stream_bits`` of RS-protected space."""
        nbytes = stream_bits // 8
        ncodewords = nbytes // NSIZE
        data = ncodewords * DATA_CHUNK
        return max(0, data - OVERHEAD)

    # -- deframing -----------------------------------------------------------
    def decode(self, raw: bytes) -> tuple[bytes, int]:
        """RS stream -> (message, corrected_bytes).

        Raises PayloadCorrupt if the stream is uncorrectable or fails the
        magic/CRC integrity checks. ``corrected_bytes`` is the number of
        bytes RS had to repair (0 == clean decode) — used by the harness to
        estimate bit-error rates.
        """
        raw = bytes(raw)
        corrected = 0
        out = bytearray()
        for i in range(0, len(raw) - (len(raw) % NSIZE), NSIZE):
            chunk = raw[i : i + NSIZE]
            try:
                decoded, corrected_codeword, _ = self.rs.decode(chunk)
            except ReedSolomonError as exc:
                raise PayloadCorrupt(
                    f"RS uncorrectable at codeword {i // NSIZE} "
                    f"({exc}). Payload did not survive this round trip."
                ) from exc
            corrected += sum(
                1 for a, b in zip(chunk, corrected_codeword[: len(chunk)]) if a != b
            )
            out += decoded
        if len(out) < len(MAGIC) + 9 or out[: len(MAGIC)] != MAGIC:
            raise PayloadCorrupt("magic not found — not a Stegstr image or payload destroyed")
        if out[len(MAGIC)] != VERSION:
            raise PayloadCorrupt(f"unsupported framing version {out[len(MAGIC)]}")
        mlen = struct.unpack(">I", bytes(out[9:13]))[0]
        if 17 + mlen + 4 > len(out):
            raise PayloadCorrupt("length field inconsistent with stream")
        msg = bytes(out[17 : 17 + mlen])
        crc = struct.unpack(">I", bytes(out[13:17]))[0]
        if zlib.crc32(msg) != crc:
            raise PayloadCorrupt("CRC mismatch — payload corrupted after RS correction")
        return msg, corrected
