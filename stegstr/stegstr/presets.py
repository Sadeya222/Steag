"""Local approximations of real messaging-platform image pipelines.

These are *approximations* (documented as such): the real apps run libjpeg
with quality settings that vary by device/AB-test, so the harness treats
these presets as the nominal case and the quality sweep as the envelope.
WhatsApp and Telegram downscale the long edge to ~1280 for photos; Instagram
to 1080.  Telegram "send as file" is bit-exact (no recompression).

Pillow is used for both resize (LANCZOS) and JPEG re-encode (libjpeg), which
is close to what the apps do client-side; ffmpeg-based pipelines (e.g. video
thumbnail re-encodes) are out of scope for Tier A.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps


@dataclass(frozen=True)
class PlatformPreset:
    name: str
    max_dim: int | None       # long edge cap (None = no resize)
    jpeg_quality: int         # recompress quality (libjpeg scale)
    note: str = ""


PRESETS: tuple[PlatformPreset, ...] = (
    PlatformPreset("whatsapp", 1280, 85, "photo send; long edge -> 1280, q~85"),
    PlatformPreset("telegram-photo", 1280, 90, "photo send; long edge -> 1280, q~90"),
    PlatformPreset("telegram-file", None, 95, "send as file: bit-exact (no recompress)"),
    PlatformPreset("instagram", 1080, 80, "feed; long edge -> 1080, q~80"),
    PlatformPreset("worst-case", None, 70, "stress: heavy recompress, no resize"),
)


def get_preset(name: str) -> PlatformPreset:
    for p in PRESETS:
        if p.name == name:
            return p
    raise KeyError(f"unknown preset {name!r}; known: {[p.name for p in PRESETS]}")


def apply_preset(img: Image.Image, preset: PlatformPreset) -> Image.Image:
    """Run an image through a platform's approximated pipeline."""
    img = img.convert("RGB")
    if preset.max_dim and max(img.size) > preset.max_dim:
        img.thumbnail((preset.max_dim, preset.max_dim), Image.Resampling.LANCZOS)
    if preset.jpeg_quality >= 95:
        # telegram-file: bit-exact pass-through
        return img.copy()
    buf = BytesIO()
    img.save(buf, "JPEG", quality=preset.jpeg_quality, subsampling=2,
             optimize=False, progressive=False)
    buf.seek(0)
    out = Image.open(buf).convert("RGB")
    out = ImageOps.exif_transpose(out)
    return out


def pipeline(img: Image.Image, preset: PlatformPreset, recompress_only: bool = False) -> Image.Image:
    """Apply a preset; ``recompress_only`` skips the resize step.

    recompress_only models the case where the sender pre-sizes the image to
    the platform's cap, so the app only re-encodes (the common case for the
    tool's recommended workflow).
    """
    if recompress_only:
        target = PlatformPreset(preset.name, None, preset.jpeg_quality, preset.note)
    else:
        target = preset
    return apply_preset(img, target)
