"""colors.py must be bit-exact with OpenCV (that was its whole point)."""

import numpy as np
import pytest

from stegstr.colors import rgb_to_ycbcr, ycbcr_to_rgb

cv2 = pytest.importorskip("cv2")


@pytest.mark.parametrize("seed", [0, 1, 7, 99])
def test_forward_matches_cv2(seed):
    rng = np.random.RandomState(seed)
    rgb = rng.randint(0, 256, (333, 271, 3)).astype(np.uint8)
    y, cb, cr = rgb_to_ycbcr(rgb)
    ref = cv2.cvtColor(rgb[:, :, ::-1], cv2.COLOR_BGR2YCrCb)
    assert (y == ref[:, :, 0]).all()
    assert (cb == ref[:, :, 2]).all()
    assert (cr == ref[:, :, 1]).all()


@pytest.mark.parametrize("seed", [2, 5, 23, 77])
def test_inverse_matches_cv2(seed):
    rng = np.random.RandomState(seed)
    y = rng.randint(0, 256, (333, 271)).astype(np.uint8)
    cb = rng.randint(0, 256, (333, 271)).astype(np.uint8)
    cr = rng.randint(0, 256, (333, 271)).astype(np.uint8)
    rgb = ycbcr_to_rgb(y, cb, cr)
    ycrcb = np.stack([y, cr, cb], axis=-1)
    ref = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)[:, :, ::-1]
    assert (rgb == ref).all()


def test_roundtrip_stability():
    """Y drift through RGB->Y->RGB->Y stays within 1 LSB (same as OpenCV)."""
    rng = np.random.RandomState(11)
    base = np.clip(rng.normal(120, 45, (128, 128, 3)), 0, 255).astype(np.uint8)
    y, cb, cr = rgb_to_ycbcr(base)
    back = ycbcr_to_rgb(y, cb, cr)
    y2, _, _ = rgb_to_ycbcr(back)
    assert np.abs(y.astype(int) - y2.astype(int)).max() <= 1
