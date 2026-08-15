"""Engine + codec tests: DCT correctness, round trips, JPEG survival."""

import numpy as np
import pytest
from PIL import Image

from stegstr.codec import capacity_bytes, decode_image, encode_image, psnr
from stegstr.dct import fdct, idct, jpeg_quant_table, zigzag_indices
from stegstr.engine import TierAConfig, StegError


def test_dct_roundtrip_exact():
    rng = np.random.RandomState(0)
    blocks = rng.uniform(0, 255, size=(16, 16, 8, 8))
    back = idct(fdct(blocks))
    assert np.allclose(back, blocks, atol=1e-9)


def test_dct_jpeg_convention():
    """DC term of a flat block = 8x the mean (JPEG Annex-A scaling)."""
    blocks = np.full((1, 1, 8, 8), 100.0)
    F = fdct(blocks)
    assert abs(F[0, 0, 0, 0] - 8.0 * (100.0 - 128.0)) < 1e-9
    assert np.abs(F[0, 0, 1:, :]).max() < 1e-9


def test_zigzag_and_quant_table_sane():
    zz = zigzag_indices()
    assert zz[0] == (0, 0)
    assert len(set(zz)) == 64
    t75 = jpeg_quant_table(75)
    t90 = jpeg_quant_table(90)
    assert t75[0, 0] == 8  # 16 * 0.5
    assert (t90 <= t75).all()
    assert (jpeg_quant_table(100) >= 1).all()


def make_img(w=512, h=512, seed=1):
    rng = np.random.RandomState(seed)
    base = np.clip(rng.normal(120, 45, (h, w, 3)), 0, 255).astype(np.uint8)
    # add smooth structure
    y, x = np.mgrid[0:h, 0:w]
    base = np.clip(base + (30 * np.sin(x / 50) + 20 * np.cos(y / 40))[..., None], 0, 255)
    return Image.fromarray(base.astype(np.uint8))


@pytest.mark.parametrize("size", [(256, 256), (512, 384), (1024, 1024)])
def test_lossless_roundtrip_sizes(size):
    """Payloads scale with image size: 5%, 40% and 90% of capacity at r=2."""
    from stegstr.codec import capacity_bytes
    cfg = TierAConfig()
    for frac in (0.05, 0.4, 0.9):
        n = int(capacity_bytes(size[0], size[1], cfg, repetitions=2) * frac)
        msg = bytes((i * 7) % 251 for i in range(n))
        img = make_img(*size, seed=n)
        carrier = encode_image(img, msg, cfg)
        decoded, meta = decode_image(carrier, cfg)
        assert decoded == msg
        assert meta["corrected_bytes"] == 0


@pytest.mark.parametrize("quality", [85, 90])
def test_jpeg_recompress_survives(quality):
    msg = b"jpeg survival " + b"j" * 240
    img = make_img(1024, 1024, seed=7)
    carrier = encode_image(img, msg)
    buf = __import__("io").BytesIO()
    carrier.save(buf, "JPEG", quality=quality, subsampling=2)
    buf.seek(0)
    received = Image.open(buf).convert("RGB")
    decoded, meta = decode_image(received)
    assert decoded == msg
    assert meta["corrected_bytes"] >= 0


def test_payload_too_large():
    img = make_img(64, 64)
    with pytest.raises(StegError):
        encode_image(img, b"x" * 5000)


def test_capacity_monotonic():
    cfg = TierAConfig()
    assert capacity_bytes(512, 512, cfg, repetitions=2) < capacity_bytes(1024, 1024, cfg, repetitions=2)
    assert capacity_bytes(1024, 1024, cfg, repetitions=12) < capacity_bytes(1024, 1024, cfg, repetitions=2)


def test_false_positive_rejected():
    img = make_img(512, 512, seed=99)
    with pytest.raises(StegError):
        decode_image(img)


def test_psnr_high_for_embedding():
    msg = b"psnr check " + b"p" * 100
    img = make_img(1024, 1024, seed=3)
    carrier = encode_image(img, msg)
    assert psnr(img, carrier) > 30.0
