"""DCT primitives in the exact JPEG (Annex A / Annex K) convention.

Why not ``cv2.dct``?  The JPEG spec's forward DCT is

    F(u,v) = (1/4) C(u) C(v) sum_x sum_y f(x,y) cos((2x+1)u*pi/16) cos((2y+1)v*pi/16)

with C(0) = 1/sqrt(2), C(k>0) = 1.  libjpeg's encoder/decoder use exactly this
scaling, so the standard Annex-K quantization tables apply to coefficients
produced by it.  OpenCV's ``dct`` uses a different normalization, so a
per-coefficient Q read from the Annex-K table would silently mismatch
libjpeg's quantization grid.  Using the JPEG-convention matrix DCT keeps our
embedding grid aligned with what real JPEG re-encoders (WhatsApp / Instagram /
Telegram, all libjpeg-ish) will quantize against.

Vectorized with einsum over the full block array, so it stays fast
(~10^5 blocks/sec on a laptop).
"""

from __future__ import annotations

import numpy as np

N = 8

# --- 1D DCT-II kernel, scaled so K^T K = 4I (JPEG convention) --------------
_i = np.arange(N)[:, None]
_j = np.arange(N)[None, :]
_C = np.where(_i == 0, 1.0 / np.sqrt(2.0), 1.0)
_K = (_C * np.cos((2 * _j + 1) * _i * np.pi / (2 * N))).astype(np.float64)


def fdct(blocks: np.ndarray) -> np.ndarray:
    """Forward DCT of an (..., 8, 8) float array of pixel values (0..255).

    Level-shift by -128 is applied inside, exactly like the JPEG FDCT.
    """
    return 0.25 * np.einsum("ij,...jl,kl->...ik", _K, blocks - 128.0, _K)


def idct(coeffs: np.ndarray) -> np.ndarray:
    """Inverse DCT of an (..., 8, 8) float array of JPEG-domain coefficients.

    Returns pixel values in the 0..255 range (level shift re-applied).
    """
    return 128.0 + 0.25 * np.einsum("ij,...ik,kl->...jl", _K, coeffs, _K)


def to_blocks(y: np.ndarray) -> np.ndarray:
    """Split a (h, w) plane into (h//8, w//8, 8, 8) blocks (top-left crop).

    NOTE: a plain ``reshape(h//8, 8, w//8, 8)`` is WRONG for 2D input — in
    C-order it yields *image rows* as blocks, not spatial 8x8 blocks.  The
    transpose is required so block (i, j) = plane[8i:8i+8, 8j:8j+8].
    """
    h, w = y.shape
    bh, bw = h // 8, w // 8
    return y[: bh * 8, : bw * 8].reshape(bh, 8, bw, 8).transpose(0, 2, 1, 3)


def from_blocks(blocks: np.ndarray, h: int, w: int) -> np.ndarray:
    """Reassemble an (h//8, w//8, 8, 8) block array into a (h, w) plane."""
    bh, bw = blocks.shape[:2]
    plane = blocks.transpose(0, 2, 1, 3).reshape(bh * 8, bw * 8)
    out = np.zeros((h, w), dtype=blocks.dtype)
    out[: bh * 8, : bw * 8] = plane
    return out


def zigzag_indices(n: int = N) -> list[tuple[int, int]]:
    """(row, col) pairs in zigzag scan order, starting at the DC term."""
    idx = []
    r = c = 0
    down = True
    for _ in range(n * n):
        idx.append((r, c))
        if down:
            if c == n - 1:
                r += 1
                down = False
            elif r == 0:
                c += 1
                down = False
            else:
                r -= 1
                c += 1
        else:
            if r == n - 1:
                c += 1
                down = True
            elif c == 0:
                r += 1
                down = True
            else:
                r += 1
                c -= 1
    return idx


# --- JPEG Annex-K luminance quantization table (quality 50 base) ------------
_BASE_LUMA = np.array(
    [
        [16, 11, 10, 16, 24, 40, 51, 61],
        [12, 12, 14, 19, 26, 58, 60, 55],
        [14, 13, 16, 24, 40, 57, 69, 56],
        [14, 17, 22, 29, 51, 87, 80, 62],
        [18, 22, 37, 56, 68, 109, 103, 77],
        [24, 35, 55, 64, 81, 104, 113, 92],
        [49, 64, 78, 87, 103, 121, 120, 101],
        [72, 92, 95, 98, 112, 100, 103, 99],
    ],
    dtype=np.float64,
)


def jpeg_quant_table(quality: int) -> np.ndarray:
    """Annex-K table scaled to ``quality`` using libjpeg's formula.

    scale = 5000/q  (q < 50)  |  200 - 2q  (q >= 50)
    Q = max(1, round(base * scale / 100))
    """
    if quality < 1 or quality > 100:
        raise ValueError(f"quality must be in 1..100, got {quality}")
    scale = 5000.0 / quality if quality < 50 else 200.0 - 2.0 * quality
    return np.maximum(1, (np.round(_BASE_LUMA * scale / 100.0)).astype(np.int64))


def quant_steps_for_band(quality: int, band_indices: tuple[int, ...]) -> np.ndarray:
    """Per-coefficient embedding steps (zigzag order) for a band, as int64."""
    zz = zigzag_indices(N)
    table = jpeg_quant_table(quality)
    return np.array([table[zz[i]] for i in band_indices], dtype=np.int64)
