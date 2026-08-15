"""Image-level codec: ties the payload framing and the DCT engine together.

- RGB <-> YCbCr via full-range BT.601 (JPEG convention, same as libjpeg).
  Only the Y plane carries data; Cb/Cr pass through untouched.
- ``encode_image`` / ``decode_image`` operate on PIL Images, so the same
  code path handles PNG/JPEG carriers, EXIF-rotated photos, and whatever a
  messaging platform spits back out.
- ``multiscale=True`` additionally embeds a second copy at half scale (the
  platform's typical downscale factor), which is the Tier-A best-effort at
  surviving platforms that downscale large uploads (see validation report).
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import numpy as np
from PIL import Image, ImageOps

from .colors import rgb_to_ycbcr, ycbcr_to_rgb
from .engine import TierAConfig, extract_y, embed_y, StegError
from .payload import PayloadCodec, PayloadCorrupt, PayloadTooLarge

DEFAULT_MULTISCALE_FACTOR = 0.5


def load_image(path: str | bytes) -> Image.Image:
    """Load an image, apply EXIF orientation, normalize to RGB uint8."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _rgb_to_ycbcr(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """uint8 RGB (h, w, 3) -> full-range BT.601 Y, Cb, Cr planes (uint8)."""
    return rgb_to_ycbcr(rgb)


def _ycbcr_to_rgb(y: np.ndarray, cb: np.ndarray, cr: np.ndarray) -> np.ndarray:
    """Rebuild uint8 RGB from (possibly modified) Y and original Cb/Cr."""
    return ycbcr_to_rgb(y, cb, cr)


def capacity_bytes(w: int, h: int, cfg: TierAConfig | None = None,
                   repetitions: int | None = None) -> int:
    """Max message bytes for an image of w x h at the given redundancy."""
    cfg = cfg or TierAConfig()
    nslots = cfg.nslots(h, w)
    if nslots <= 0:
        return 0
    r = repetitions or cfg.choose_repetitions(cfg.min_repetitions * 8, nslots)
    if r is None:
        return 0
    stream_bits = (nslots // r) // 8 * 8
    return PayloadCodec().max_message_bytes(stream_bits)


def max_capacity_bytes(w: int, h: int, cfg: TierAConfig | None = None) -> tuple[int, int]:
    """Best achievable payload (bytes) and the redundancy that achieves it.

    Images below ~400x400 can't hold even one 255-byte RS codeword at r=2 but
    may fit small messages at r=1; the max over all redundancy levels is the
    honest number to report in errors and capacity hints.
    """
    cfg = cfg or TierAConfig()
    best, best_r = 0, 0
    for r in range(1, cfg.max_repetitions + 1):
        try:
            c = capacity_bytes(w, h, cfg, repetitions=r)
        except StegError:
            c = 0
        if c > best:
            best, best_r = c, r
    return best, best_r


def encode_image(
    img: Image.Image,
    message: bytes,
    cfg: TierAConfig | None = None,
    multiscale: bool = False,
    scale_factor: float = DEFAULT_MULTISCALE_FACTOR,
) -> Image.Image:
    """Embed ``message`` into a copy of ``img``; returns the carrier image."""
    cfg = cfg or TierAConfig()
    codec = PayloadCodec()
    stream = codec.encode(message)

    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    y, cb, cr = _rgb_to_ycbcr(rgb)
    if not multiscale:
        y_emb = embed_y(y, stream, cfg, scale_key=0)
    else:
        # Inner copy at the destination scale, then the native-scale copy.
        dst_w = max(8, int(rgb.shape[1] * scale_factor) // 8 * 8)
        dst_h = max(8, int(rgb.shape[0] * scale_factor) // 8 * 8)
        small = img.resize((dst_w, dst_h), Image.Resampling.LANCZOS)
        y_s, cb_s, cr_s = _rgb_to_ycbcr(np.asarray(small, dtype=np.uint8))
        y_s_emb = embed_y(y_s, stream, cfg, scale_key=1)
        small_emb = Image.fromarray(_ycbcr_to_rgb(y_s_emb, cb_s, cr_s))
        up = small_emb.resize(rgb.shape[1::-1], Image.Resampling.LANCZOS)
        y2, cb2, cr2 = _rgb_to_ycbcr(np.asarray(up, dtype=np.uint8))
        y_emb = embed_y(y2, stream, cfg, scale_key=0)
        cb, cr = cb2, cr2
    out_rgb = _ycbcr_to_rgb(y_emb, cb, cr)
    return Image.fromarray(out_rgb)


def decode_image(
    img: Image.Image,
    cfg: TierAConfig | None = None,
    try_scale_keys: tuple[int, ...] = (0, 1),
) -> tuple[bytes, dict]:
    """Recover a message from a carrier image.

    Returns (message, meta).  Raises StegError if no scale key yields a
    payload that passes RS + magic + CRC (i.e. the image is not a Stegstr
    carrier, or the channel destroyed the payload).
    """
    cfg = cfg or TierAConfig()
    codec = PayloadCodec()
    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    y, _, _ = _rgb_to_ycbcr(rgb)

    last_meta: dict = {}
    for key in try_scale_keys:
        stream, meta = extract_y(y, cfg, scale_key=key)
        last_meta = meta
        if stream is None:
            continue
        try:
            message, corrected = codec.decode(stream)
        except PayloadCorrupt:
            continue  # right key but too damaged, or wrong key -> try next
        meta["corrected_bytes"] = corrected
        meta["stream_bytes"] = len(stream)
        meta["payload_bytes"] = len(message)
        return message, meta
    raise StegError(
        "no payload recovered (not a Stegstr carrier, or the channel "
        f"destroyed it; last attempt: {last_meta})"
    )


def decode_image_bytes(data: bytes, cfg: TierAConfig | None = None) -> tuple[bytes, dict]:
    """Decode from raw image bytes (e.g. a file downloaded from a chat app)."""
    return decode_image(load_image(BytesIO(data)), cfg)


def psnr(a: Image.Image, b: Image.Image) -> float:
    """PSNR between two same-size RGB images (dB)."""
    x = np.asarray(a.convert("RGB"), dtype=np.float64)
    y = np.asarray(b.convert("RGB"), dtype=np.float64)
    mse = np.mean((x - y) ** 2)
    if mse == 0:
        return float("inf")
    return float(10.0 * np.log10(255.0 ** 2 / mse))
