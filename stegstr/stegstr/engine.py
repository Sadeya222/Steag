"""Tier A steganographic engine: DCT mid-frequency QIM + redundant voting.

Pipeline (encode):
    message --RS(255,191,64)--> bit stream --header(R, nbits)--> bit copies
    --seeded permutation--> slot positions --QIM into Y-plane DCT coeffs-->

Pipeline (decode): inverse, with weighted majority voting per bit.

Design notes
------------
* Embedding domain is the luminance plane of the JPEG color space, split into
  8x8 blocks, transformed with the JPEG-convention DCT (see ``dct``).  Only
  the Y plane is used: platforms subsample chroma 4:2:0, which destroys any
  chroma embedding, while Y survives untouched.
* Each block contributes 6 mid-frequency coefficients (zigzag indices
  6, 8, 10, 12, 14, 16 — all within the first 3x3-ish ring of the block, where
  the JPEG quantization steps are small and stable).  QIM: the coefficient is
  snapped to the nearest multiple of its per-coefficient embedding step Q_i,
  where Q_i is the Annex-K step at ``embed_quality`` (default 75).
  Rationale: if the platform recompresses at a quality >= embed_quality its
  quantization is *finer* than ours, so our snapped values sit on its grid and
  survive; if it recompresses below, parity is destroyed (~50% flips) and
  nothing helps except a coarser embed step.  So embed at the worst-case
  quality of the target platforms (WhatsApp ~85, Telegram ~90, Instagram ~80
  are all >= 75).
* Repetition R is chosen at encode time as the largest value in
  [min_repetitions, max_repetitions] that fits, so a 200-byte note gets ~12
  votes per bit while a maxed-out payload still gets at least one.
* Bit positions are decorrelated with a seeded Fisher-Yates permutation, so
  consecutive payload bits never share a block: a recompression burst that
  kills one block only takes down one vote per bit, and votes per bit are
  spread across the whole image.
* A 6-byte header (magic 0xA5, version, R, 24-bit stream length) is embedded
  in the first 48 blocks with 6 votes per bit, so the decoder learns R and
  the payload length without any out-of-band knowledge.

All transforms and votes are vectorized over the full block array.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .dct import fdct, idct, from_blocks, quant_steps_for_band, to_blocks, zigzag_indices

BAND_INDICES = (6, 8, 10, 12, 14, 16)
SLOTS_PER_BLOCK = len(BAND_INDICES)
_ZZ = zigzag_indices(8)  # index -> (row, col) in JPEG zigzag order


def _band_coords(band: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
    """(row, col) arrays for a band of zigzag indices."""
    rows = np.array([_ZZ[i][0] for i in band])
    cols = np.array([_ZZ[i][1] for i in band])
    return rows, cols

# Header layout: 6 raw bytes -> RS(22, 6, 16) -> 176 bits.
# Each header bit is voted across `votes` slots (6 per block), with the layout
# chosen by image size: 12 votes (2 blocks/bit, 352 blocks) on larger images,
# 6 votes (1 block/bit, 176 blocks) on small ones.  The RS layer fixes the
# remaining scattered byte errors — the header must be *more* robust than the
# payload, since everything downstream depends on r and stream_bits.
HEADER_RAW_BYTES = 6
HEADER_NSYM = 16
HEADER_BITS = (HEADER_RAW_BYTES + HEADER_NSYM) * 8  # 176
HEADER_MAGIC = 0xA5
_HEADER_LAYOUTS = ((352, 12), (176, 6))  # (blocks, votes per bit)

# Scale key for multiscale embedding (0 = native size, 1 = pre-scaled copy)
_KEY_STRIDE = 0x9E3779B1


class StegError(Exception):
    """Raised for engine-level failures (too small, too large, corrupt)."""


@dataclass
class TierAConfig:
    """Engine parameters.  Both sides must use the same config."""

    band_indices: tuple[int, ...] = BAND_INDICES
    # Embedding grid: Annex-K steps at this quality.  Parity survives a
    # recompress only when the platform's grid is an exact integer multiple
    # of ours (round(q * r) preserves parity for integer r): q70 is exactly
    # 2.0x the q85 grid (WhatsApp), 3.0x q90 (Telegram), 6.0x q95, and 1.0x
    # q70 itself.  Fractional ratios (e.g. q70 vs q80) still work but with
    # ~10-25% vote flips, which the redundancy + RS absorb for small payloads.
    embed_quality: int = 70  # worst-case platform quality to survive
    min_repetitions: int = 1  # 1 = max capacity with zero headroom; 2+ = protected
    max_repetitions: int = 12
    seed: int = 0x5EED
    max_stream_bytes: int = 1 << 21  # 2 MiB cap, well under the 24-bit header

    def quant_steps(self) -> np.ndarray:
        return quant_steps_for_band(self.embed_quality, self.band_indices)

    @staticmethod
    def header_layout(nblocks: int) -> tuple[int, int]:
        """(header_blocks, votes_per_header_bit) for an image of nblocks."""
        for blocks, votes in _HEADER_LAYOUTS:
            if nblocks >= blocks:
                return blocks, votes
        raise StegError(
            f"image too small for steganography ({nblocks} 8x8 blocks < 176)"
        )

    def nslots(self, h: int, w: int) -> int:
        """Payload slot count for an image of this size."""
        bh, bw = h // 8, w // 8
        blocks, _ = self.header_layout(bh * bw)
        return max(0, bh * bw - blocks) * SLOTS_PER_BLOCK

    def choose_repetitions(self, stream_bits: int, nslots: int) -> int:
        if stream_bits > nslots * self.max_repetitions:
            raise StegError(
                f"payload needs {stream_bits} bits x{self.max_repetitions} votes "
                f"but image has only {nslots} slots"
            )
        for r in range(self.max_repetitions, self.min_repetitions - 1, -1):
            if stream_bits * r <= nslots:
                return r
        raise StegError(f"payload too large: {stream_bits} bits don't fit")

    # -- deterministic per-scale permutation over payload slots ---------------
    def permutation(self, nslots: int, scale_key: int) -> tuple[np.ndarray, np.ndarray]:
        rng = np.random.RandomState(self.seed + scale_key * _KEY_STRIDE)
        perm = rng.permutation(nslots)
        inv = np.empty(nslots, dtype=np.int64)
        inv[perm] = np.arange(nslots, dtype=np.int64)
        return perm, inv


# --------------------------------------------------------------------------
# header encode/decode (RS-protected)
# --------------------------------------------------------------------------
_HEADER_RS = None  # lazy singleton


def _header_rs():
    global _HEADER_RS
    if _HEADER_RS is None:
        from reedsolo import RSCodec
        _HEADER_RS = RSCodec(HEADER_NSYM)
    return _HEADER_RS


def _header_stream(r: int, stream_bits: int) -> bytes:
    """22-byte RS codeword carrying (magic, version, r, stream_bits)."""
    raw = bytes([
        HEADER_MAGIC, 1, r & 0xFF,
        (stream_bits >> 16) & 0xFF, (stream_bits >> 8) & 0xFF, stream_bits & 0xFF,
    ])
    return bytes(_header_rs().encode(raw))


def _header_bits(r: int, stream_bits: int) -> np.ndarray:
    return np.unpackbits(np.frombuffer(_header_stream(r, stream_bits), dtype=np.uint8))


def _parse_header(bits: np.ndarray) -> tuple[bool, int, int]:
    from reedsolo import ReedSolomonError
    if bits.shape[0] != HEADER_BITS:
        return False, 0, 0
    raw = np.packbits(bits.astype(np.uint8)).tobytes()
    try:
        decoded, _, _ = _header_rs().decode(raw)
    except ReedSolomonError:
        return False, 0, 0
    if decoded[0] != HEADER_MAGIC or decoded[1] != 1:
        return False, 0, 0
    r = decoded[2]
    stream_bits = (decoded[3] << 16) | (decoded[4] << 8) | decoded[5]
    if not (1 <= r <= 32) or stream_bits <= 0 or stream_bits % 8:
        return False, 0, 0
    return True, int(r), int(stream_bits)


# --------------------------------------------------------------------------
# coefficient gather/scatter helpers
# --------------------------------------------------------------------------
def _slot_matrix(F: np.ndarray, band: tuple[int, ...]) -> np.ndarray:
    """(nblocks, 6) coefficient matrix in slot-major order (block, slot)."""
    rows, cols = _band_coords(band)
    return F[:, rows, cols]


def _embed_slotmat(slotmat: np.ndarray, bits: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """QIM-embed ``bits`` (per slot) into a (n, 6) coefficient matrix."""
    c = slotmat.ravel()
    Qv = np.tile(Q, len(bits) // len(Q))
    q = np.round(c / Qv).astype(np.int64)
    flip = (q & 1) != bits
    q2 = q.copy()
    # move to the nearer neighboring grid point whose parity matches the bit
    q2[flip] += np.where((q[flip] * Qv[flip] - c[flip]) <= 0, 1, -1)
    return (q2 * Qv).reshape(slotmat.shape)


def _extract_slotmat(slotmat: np.ndarray, Q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-slot parity votes + confidences from a (n, 6) coefficient matrix.

    Confidence w in [0, 1]: 1 when the coefficient sits exactly on its grid
    point, falling to 0 halfway to the neighboring grid point.  Coefficients
    sitting inside the dead zone (|c| < 0.4 Q) abstain — they would otherwise
    cast a systematic ``bit=0`` vote.
    """
    c = slotmat.ravel()
    Qv = np.tile(Q, c.shape[0] // len(Q))
    ratio = c / Qv
    q = np.round(ratio).astype(np.int64)
    frac = np.abs(ratio - q)
    w = np.clip(1.0 - 2.0 * frac, 0.0, 1.0)
    w[np.abs(c) < 0.4 * Qv] = 0.0  # abstain in the dead zone
    votes = (q & 1).astype(np.int8)
    return votes, w


# --------------------------------------------------------------------------
# header helpers (vectorized over all header bits at once)
# --------------------------------------------------------------------------
def _header_positions(header_blocks: int, header_votes: int):
    """(blocks_idx, slot_idx) for every header vote slot, in bit-major order."""
    blocks_per_bit = header_votes // SLOTS_PER_BLOCK
    bits = HEADER_BITS // 8 * 8  # 176
    blk = np.repeat(np.arange(bits) * blocks_per_bit, header_votes)
    slt = np.tile(np.arange(header_votes) % SLOTS_PER_BLOCK, bits)
    return blk, slt


def _embed_header(Fflat, hdr: np.ndarray, header_blocks: int, header_votes: int,
                  rows, cols, Q: np.ndarray) -> None:
    blk, slt = _header_positions(header_blocks, header_votes)
    coeff = Fflat[:header_blocks][blk, rows[slt], cols[slt]]
    parity = np.repeat(hdr, header_votes)
    embedded = _embed_slotmat(coeff.reshape(-1, 1), parity, Q).ravel()
    Fflat[:header_blocks][blk, rows[slt], cols[slt]] = embedded


def _extract_header(Fflat, header_blocks: int, header_votes: int,
                    rows, cols, Q: np.ndarray) -> tuple[bool, int, int]:
    blk, slt = _header_positions(header_blocks, header_votes)
    coeff = Fflat[:header_blocks][blk, rows[slt], cols[slt]]
    votes, weights = _extract_slotmat(coeff.reshape(-1, 1), Q)
    bit_idx = np.repeat(np.arange(HEADER_BITS), header_votes)
    accum = np.bincount(bit_idx, weights=np.where(votes == 1, weights, -weights),
                        minlength=HEADER_BITS)
    bits = (accum > 0).astype(np.uint8)
    return _parse_header(bits)


# --------------------------------------------------------------------------
# plane-level embed / extract (Y plane, uint8 -> uint8)
# --------------------------------------------------------------------------
def embed_y(y: np.ndarray, stream: bytes, cfg: TierAConfig, scale_key: int = 0) -> np.ndarray:
    """Embed an RS stream into a uint8 luminance plane, returning the plane."""
    h, w = y.shape
    bh, bw = h // 8, w // 8
    nblocks = bh * bw
    header_blocks, header_votes = cfg.header_layout(nblocks)

    stream_bits = len(stream) * 8
    if stream_bits > cfg.max_stream_bytes * 8:
        raise StegError("stream exceeds configured max")
    nslots = cfg.nslots(h, w)
    r = cfg.choose_repetitions(stream_bits, nslots)
    perm, _ = cfg.permutation(nslots, scale_key)
    Q = cfg.quant_steps()

    blocks = to_blocks(y.astype(np.float64))  # (bh, bw, 8, 8)
    F = np.ascontiguousarray(fdct(blocks))  # einsum output may be non-contiguous!
    Fflat = F.reshape(nblocks, 8, 8)

    # -- header: RS(22,6,16) codeword, each bit voted across blocks ------------
    hdr = _header_bits(r, stream_bits)  # 176 bits
    rows, cols = _band_coords(cfg.band_indices)
    _embed_header(Fflat, hdr, header_blocks, header_votes, rows, cols, Q)

    # -- payload: copies of the stream, permuted across the payload region ----
    region = Fflat[header_blocks:]  # view
    slotmat = _slot_matrix(region, cfg.band_indices)  # (nblocks-hb, 6)
    slot_flat = slotmat.ravel()  # block-major, slot-minor
    assert slot_flat.shape[0] == nslots

    bits = np.unpackbits(np.frombuffer(stream, dtype=np.uint8)).astype(np.int8)
    bitmap = np.zeros(nslots, dtype=np.int8)
    for copy in range(r):
        lo = copy * stream_bits
        bitmap[perm[lo : lo + stream_bits]] = bits
    # NOTE: `region[:, rows, cols]` yields an F-contiguous array, so ravel()
    # would COPY — embed straight into the slot matrix and write it back.
    region[:, rows, cols] = _embed_slotmat(
        slotmat, bitmap, np.tile(Q, nslots // SLOTS_PER_BLOCK)
    )

    y_emb = np.clip(np.round(from_blocks(idct(F), bh * 8, bw * 8)), 0, 255).astype(np.uint8)
    out = y.copy()
    out[: bh * 8, : bw * 8] = y_emb
    return out


def extract_y(y: np.ndarray, cfg: TierAConfig, scale_key: int = 0) -> tuple[bytes | None, dict]:
    """Try to extract an RS stream from a uint8 luminance plane.

    Returns (stream or None, meta) where meta includes R, stream_bits and the
    mean vote margin (a decode-quality signal in [0, 1]).
    """
    h, w = y.shape
    bh, bw = h // 8, w // 8
    nblocks = bh * bw
    meta: dict = {"scale_key": scale_key, "r": 0, "stream_bits": 0, "margin": 0.0}
    try:
        header_blocks, header_votes = cfg.header_layout(nblocks)
    except StegError:
        return None, meta

    Q = cfg.quant_steps()
    rows, cols = _band_coords(cfg.band_indices)
    blocks = to_blocks(y.astype(np.float64))
    F = np.ascontiguousarray(fdct(blocks))
    Fflat = F.reshape(nblocks, 8, 8)

    # -- header ----------------------------------------------------------------
    ok, r, stream_bits = _extract_header(Fflat, header_blocks, header_votes,
                                         rows, cols, Q)
    if not ok:
        return None, meta
    meta["r"] = r
    meta["stream_bits"] = stream_bits

    # -- payload ---------------------------------------------------------------
    nslots = cfg.nslots(h, w)
    if stream_bits * r > nslots:
        return None, meta
    perm, inv = cfg.permutation(nslots, scale_key)
    region = Fflat[header_blocks:]
    slotmat = _slot_matrix(region, cfg.band_indices)
    votes, weights = _extract_slotmat(slotmat, Q)
    # Only the first `stream_bits * r` permutation entries carry payload; the
    # remaining slots were left untouched by the encoder and must not vote
    # (their coefficients are unrelated to the payload bits).
    used = stream_bits * r
    active = inv < used
    bit_idx = inv[active] % stream_bits  # which stream bit each slot votes for
    w = weights[active]
    v = votes[active]
    weight = np.where(v == 1, w, -w)
    accum = np.bincount(bit_idx, weights=weight, minlength=stream_bits)
    n_votes = np.bincount(bit_idx, weights=(w > 0), minlength=stream_bits)
    stream_bits_arr = accum > 0
    margin = float(np.mean(np.abs(accum) / np.maximum(n_votes, 1))) if stream_bits else 0.0
    meta["margin"] = margin
    if stream_bits % 8:
        return None, meta
    stream = np.packbits(stream_bits_arr.astype(np.uint8)).tobytes()
    return stream, meta
