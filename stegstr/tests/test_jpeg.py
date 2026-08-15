"""JPEG codec tests (baseline reader/writer used by the JPEG-native engine path).

Status: the codec is verified interoperable with Pillow/libjpeg; the
JPEG-native embedding engine (embedding into quantized levels directly) is
the next milestone.  These tests lock in the codec so it doesn't rot.
"""

import io

import numpy as np
import pytest
from PIL import Image

from stegstr.dct import fdct, jpeg_quant_table, to_blocks, zigzag_indices
from stegstr.jpeg import JpegError, parse_jpeg, write_jpeg


def make_blocks(w=64, h=64, seed=0, density=0.2):
    rng = np.random.RandomState(seed)
    nblk = (w // 8) * (h // 8)
    blocks = {1: np.zeros((nblk, 64), dtype=np.int64),
              2: np.zeros((nblk, 64), dtype=np.int64),
              3: np.zeros((nblk, 64), dtype=np.int64)}
    for b in range(nblk):
        blocks[1][b, 0] = int(rng.randint(-60, 60))
        for k in range(1, 64):
            if rng.rand() < density:
                blocks[1][b, k] = int(rng.randint(-4, 5))
    return blocks


def test_write_read_roundtrip():
    blocks = make_blocks()
    Q = jpeg_quant_table(70)
    data = write_jpeg(blocks, {0: Q, 1: Q}, 64, 64)
    j = parse_jpeg(data)
    assert j.width == 64 and j.height == 64
    assert (j.blocks[1] == blocks[1]).all()
    assert (j.blocks[2] == blocks[2]).all()


def test_pil_opens_our_jpeg():
    blocks = make_blocks()
    Q = jpeg_quant_table(70)
    data = write_jpeg(blocks, {0: Q, 1: Q}, 64, 64)
    im = Image.open(io.BytesIO(data)).convert("RGB")
    assert im.size == (64, 64)


def test_parse_pil_jpeg_matches_theory():
    """Our reader must agree with our own FDCT on a trivial image."""
    grad = np.tile(np.arange(64, dtype=np.uint8), (64, 1))
    buf = io.BytesIO()
    Image.fromarray(grad).save(buf, "JPEG", quality=80, subsampling=0)
    buf.seek(0)
    j = parse_jpeg(buf.getvalue())
    F = fdct(to_blocks(grad.astype(np.float64)))
    Q = j.quant_tables[0]
    zz = zigzag_indices()
    levels = np.round(F[0, 0] / Q).astype(np.int64)
    parsed = j.blocks[1][0]
    for k in range(64):
        r, c = zz[k]
        if abs(levels[r, c] - parsed[k]) > 1:
            pytest.fail(f"zz{k}: theory {levels[r, c]} vs parsed {parsed[k]}")


def test_reject_non_jpeg():
    with pytest.raises((JpegError, AssertionError, Exception)):
        parse_jpeg(b"not a jpeg at all......")
