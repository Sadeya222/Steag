"""Validation harness — built before any networking code, per the build order.

What it proves:
  1. Tier A survives local approximations of the real platforms' JPEG
     re-encodes (WhatsApp q85, Telegram q90, Instagram q80) bit-exactly.
  2. Where the survivability envelope ends (quality sweep) and what the
     bit-error rates look like (RS-corrected byte counts + vote margins).
  3. What happens when a platform also *downscales* the image (single-scale
     vs. multiscale embedding) — expected Tier-B territory, measured here.
  4. False-positive rejection: a plain image must never decode as a message.

Run:  stegstr validate  (or: python -m stegstr.validate)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .codec import encode_image, decode_image, psnr, capacity_bytes
from .engine import TierAConfig, StegError
from .presets import PRESETS, pipeline

ASSETS = Path(__file__).resolve().parent.parent / "assets"
REPORTS = Path(__file__).resolve().parent.parent / "reports"

PASS_CASES = {
    "whatsapp": {"size": (1280, 1280), "payload": 512},
    "telegram-photo": {"size": (1280, 1280), "payload": 512},
    "telegram-file": {"size": (1024, 1024), "payload": 512},
    "instagram": {"size": (1080, 1080), "payload": 512},
    "worst-case": {"size": (1024, 1024), "payload": 256},
}


# --------------------------------------------------------------------------
# synthetic carriers (photo-like statistics, deterministic)
# --------------------------------------------------------------------------
def synthetic_image(w: int, h: int, seed: int) -> Image.Image:
    """Deterministic photo-like image: multi-octave texture + soft shapes.

    Real photos have energy at every scale, so the synthetic carrier uses
    three noise octaves (structure, texture, fine detail) plus soft shapes —
    much closer to photographic statistics than pure low-pass content, which
    is the worst case for DCT embedding (all mid-band coefficients ~0).
    """
    import cv2  # dev-only dependency: not needed on serverless deployments

    rng = np.random.RandomState(seed)
    h_, w_ = h, w
    octaves = []
    amp = 60.0
    for sigma in (40, 12, 3, 0.8):
        noise = cv2.GaussianBlur(rng.uniform(-amp, amp, (h_, w_, 3)).astype(np.float32),
                                 (0, 0), sigmaX=sigma)
        octaves.append(noise)
        amp *= 0.35
    img = np.clip(sum(octaves) + 128.0, 0, 255).astype(np.uint8)
    im = Image.fromarray(img)
    d = ImageDraw.Draw(im)
    for _ in range(5):
        x0, y0 = rng.randint(0, w), rng.randint(0, h)
        r = int(rng.uniform(min(w, h) * 0.05, min(w, h) * 0.3))
        color = tuple(int(c) for c in rng.uniform(0, 255, 3))
        if rng.rand() < 0.5:
            d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=color)
        else:
            d.rectangle([x0, y0, x0 + 2 * r, y0 + int(r * 0.6)], fill=color)
    return im.filter(ImageFilter.GaussianBlur(1.5))


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
@dataclass
class Trial:
    label: str
    ok: bool
    payload_bytes: int = 0
    psnr_db: float = 0.0
    corrected_bytes: int = 0
    margin: float = 0.0
    r: int = 0
    scale_key: int = -1
    error: str = ""
    note: str = ""


def _run_trial(img: Image.Image, message: bytes, preset_name: str,
               recompress_only: bool, multiscale: bool, label: str,
               cfg: TierAConfig) -> Trial:
    preset = next(p for p in PRESETS if p.name == preset_name)
    psnr_db = 0.0
    try:
        carrier = encode_image(img, message, cfg, multiscale=multiscale)
        psnr_db = psnr(img, carrier)
        received = pipeline(carrier, preset, recompress_only=recompress_only)
        decoded, meta = decode_image(received, cfg)
        t = Trial(label=label, ok=decoded == message, payload_bytes=len(message),
                  psnr_db=round(psnr_db, 2), corrected_bytes=meta.get("corrected_bytes", 0),
                  margin=round(meta.get("margin", 0.0), 3), r=meta.get("r", 0),
                  scale_key=meta.get("scale_key", -1))
        if t.ok and meta.get("corrected_bytes"):
            t.note = f"{meta['corrected_bytes']} bytes repaired by RS"
        return t
    except Exception as exc:  # noqa: BLE001 — the harness must not die on one case
        return Trial(label=label, ok=False, psnr_db=round(psnr_db, 2),
                     error=f"{type(exc).__name__}: {exc}"[:120])


def run_validation(cfg: TierAConfig | None = None, quiet: bool = False) -> dict:
    cfg = cfg or TierAConfig()
    started = time.time()
    report: dict = {
        "tool": "stegstr",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "Tier A DCT-QIM + RS(255,191,64), per-bit redundant voting",
        "config": asdict(cfg),
        "capacity": {},
        "sweep": [],
        "preset_matrix": [],
        "randomized_trial": {},
        "multiscale": [],
        "false_positive": {},
    }

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    # -- capacity table -----------------------------------------------------
    log("== capacity ==")
    for w, h in [(512, 512), (1024, 1024), (1080, 1080), (1280, 1280), (2160, 2160)]:
        row = {
            "max_at_r2": capacity_bytes(w, h, cfg, repetitions=2),
            "max_at_r6": capacity_bytes(w, h, cfg, repetitions=6),
            "max_at_r12": capacity_bytes(w, h, cfg, repetitions=12),
        }
        report["capacity"][f"{w}x{h}"] = row
        log(f"  {w}x{h}: r2={row['max_at_r2']}B r6={row['max_at_r6']}B r12={row['max_at_r12']}B")

    # -- 1. preset matrix: recompress-only (sender pre-sizes to platform cap) --
    log("== preset matrix (recompress-only; image pre-sized to platform cap) ==")
    photos = sorted((ASSETS / "photos").glob("*.jpg"))
    for preset in PRESETS:
        if preset.name == "telegram-file":
            continue  # bit-exact, trivially passes — shown once below
        spec = PASS_CASES[preset.name]
        if photos:
            img = Image.open(photos[0]).convert("RGB").resize(spec["size"], Image.Resampling.LANCZOS)
        else:
            img = synthetic_image(*spec["size"], seed=11)
        msg = (b"stegstr " + str(preset.name).encode() + b" " + b"x" * (spec["payload"] - 20))
        t = _run_trial(img, msg, preset.name, recompress_only=True, multiscale=False,
                       label=preset.name, cfg=cfg)
        report["preset_matrix"].append(asdict(t))
        log(f"  {t.label:16s} {'PASS' if t.ok else 'FAIL'}  payload={t.payload_bytes}B "
            f"psnr={t.psnr_db:.1f}dB margin={t.margin} r={t.r}{'  ' + t.note if t.note else ''}")

    # telegram-file: bit-exact channel
    img = synthetic_image(1024, 1024, seed=12)
    t = _run_trial(img, b"file-channel " + b"y" * 240, "telegram-file",
                   recompress_only=True, multiscale=False, label="telegram-file", cfg=cfg)
    report["preset_matrix"].append(asdict(t))
    log(f"  {t.label:16s} {'PASS' if t.ok else 'FAIL'}  payload={t.payload_bytes}B psnr={t.psnr_db:.1f}dB")

    # -- 2. quality sweep -----------------------------------------------------
    log("== quality sweep (1024x1024, 512B payload, no resize) ==")
    if photos:
        img = Image.open(photos[0]).convert("RGB").resize((1024, 1024), Image.Resampling.LANCZOS)
    else:
        img = synthetic_image(1024, 1024, seed=13)
    msg = b"quality-sweep " + b"q" * 496
    carrier = encode_image(img, msg, cfg)
    for q in range(60, 96, 5):
        from .presets import PlatformPreset
        p = PlatformPreset(f"q{q}", None, q)
        received = pipeline(carrier, p, recompress_only=True)
        try:
            decoded, meta = decode_image(received, cfg)
            ok = decoded == msg
        except StegError as exc:
            ok = False
            meta = {"margin": 0.0, "r": 0}
        report["sweep"].append({"q": q, "ok": ok, "margin": round(meta.get("margin", 0.0), 3),
                                "r": meta.get("r", 0)})
        log(f"  q={q:2d}  {'PASS' if ok else 'FAIL'}  margin={meta.get('margin', 0.0):.3f} r={meta.get('r', 0)}")

    # -- 3. randomized trial ---------------------------------------------------
    log("== randomized trial (20 synthetic images, 300B payload) ==")
    rng = np.random.RandomState(0)
    results = {p.name: {"ok": 0, "n": 20} for p in PRESETS if p.name != "telegram-file"}
    for i in range(20):
        size = (1080, 1080) if i % 2 == 0 else (1280, 1280)
        img = synthetic_image(*size, seed=100 + i)
        msg = bytes(rng.randint(0, 256, 300))
        for preset in results:
            t = _run_trial(img, msg, preset, recompress_only=True, multiscale=False,
                           label=f"{preset}#{i}", cfg=cfg)
            if t.ok:
                results[preset]["ok"] += 1
    report["randomized_trial"] = results
    for name, r in results.items():
        log(f"  {name:16s} {r['ok']}/{r['n']} passed")

    # -- 4. downscale robustness (single-scale vs multiscale) -------------------
    # Exact-2x downscale + recompress at light weight (platforms downscale to
    # their cap, typically ~2x from a 2x-sized upload; 1280->640 exercises the
    # same mechanism without the memory cost of 2560x2560 carriers).
    log("== downscale robustness (2x LANCZOS downscale + JPEG recompress) ==")
    from .presets import PlatformPreset as _PP
    for q in (85, 80):
        img = synthetic_image(1280, 1280, seed=21)
        msg = b"resize-test " + b"z" * 300
        for ms in (False, True):
            try:
                carrier = encode_image(img, msg, cfg, multiscale=ms)
                # exact 2x downscale (what platforms do to 2x-sized uploads),
                # then the platform-style recompress
                resized = carrier.resize((640, 640), Image.Resampling.LANCZOS)
                received = pipeline(resized, _PP(f"downscale2x-q{q}", None, q),
                                    recompress_only=True)
                try:
                    out, meta = decode_image(received, cfg)
                    t = Trial(label=f"2x-downscale q{q} multiscale={ms}", ok=out == msg,
                              payload_bytes=len(msg), r=meta.get("r", 0),
                              margin=round(meta.get("margin", 0.0), 3))
                except StegError as exc:
                    t = Trial(label=f"2x-downscale q{q} multiscale={ms}", ok=False,
                              error=str(exc)[:60])
            except Exception as exc:  # noqa: BLE001
                t = Trial(label=f"2x-downscale q{q} multiscale={ms}", ok=False,
                          error=f"{type(exc).__name__}: {exc}"[:60])
            report["multiscale"].append(asdict(t))
            log(f"  {t.label:38s} {'PASS' if t.ok else 'FAIL'}  {t.error[:60] if t.error else ''}")

    # -- 5. false positives ----------------------------------------------------
    log("== false positives (plain images must not decode) ==")
    fp = {"plain_synthetic": True, "plain_jpeg": True}
    for seed in (31, 32):
        img = synthetic_image(1024, 1024, seed=seed)
        try:
            decode_image(img, cfg)
            fp["plain_synthetic"] = False
        except StegError:
            pass
    try:
        decode_image(Image.open(_first_asset_photo(seed=41)), cfg)
        fp["plain_jpeg"] = False
    except (StegError, FileNotFoundError):
        pass
    report["false_positive"] = fp
    log(f"  plain synthetic rejected: {fp['plain_synthetic']}; plain photo rejected: {fp['plain_jpeg']}")

    # -- 6. real photos through the real presets --------------------------------
    log("== real photos (downloaded assets) through presets ==")
    photos = sorted(Path(ASSETS / "photos").glob("*.jpg"))
    if photos:
        for photo in photos[:3]:
            img = Image.open(photo).convert("RGB")
            preset_name = "instagram" if max(img.size) <= 1080 else "whatsapp"
            t = _run_trial(img, b"real-photo " + b"r" * 220, preset_name,
                           recompress_only=True, multiscale=False,
                           label=f"{photo.name}->{preset_name}", cfg=cfg)
            report["real_photos"] = report.get("real_photos", []) + [asdict(t)]
            log(f"  {t.label:40s} {'PASS' if t.ok else 'FAIL'}  psnr={t.psnr_db:.1f}dB")
    else:
        log("  (no photos in assets/photos — run scripts/make_assets.py)")

    report["elapsed_seconds"] = round(time.time() - started, 1)
    return report


def _first_asset_photo(seed: int) -> Path:
    photos = sorted(Path(ASSETS / "photos").glob("*.jpg"))
    if not photos:
        # fall back to a synthetic image
        p = Path(ASSETS / "synthetic") / f"fp_{seed}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        synthetic_image(1024, 1024, seed).save(p, "JPEG", quality=80)
        return p
    return photos[0]


# --------------------------------------------------------------------------
# report rendering
# --------------------------------------------------------------------------
def write_report(report: dict, out_dir: Path = REPORTS) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / "validation_report.json"
    jpath.write_text(json.dumps(report, indent=2))
    md = render_markdown(report)
    mpath = out_dir / "validation_report.md"
    mpath.write_text(md)
    return jpath, mpath


def render_markdown(report: dict) -> str:
    L = []
    L.append("# Stegstr — Tier A validation report")
    L.append("")
    L.append(f"Generated {report['generated_at']} · engine: {report['engine']}")
    L.append("")
    L.append(f"## Config\n\n```json\n{json.dumps(report['config'], indent=2)}\n```")
    L.append("")
    L.append("## Capacity (max payload bytes)")
    L.append("")
    L.append("| size | r=2 | r=6 | r=12 |")
    L.append("|---|---|---|---|")
    for size, row in report["capacity"].items():
        L.append(f"| {size} | {row['max_at_r2']} | {row['max_at_r6']} | {row['max_at_r12']} |")
    L.append("")
    L.append("## Preset matrix (recompress-only)")
    L.append("")
    L.append("| case | result | payload | psnr dB | margin | r |")
    L.append("|---|---|---|---|---|---|")
    for t in report["preset_matrix"]:
        note = f" {t['note']}" if t.get("note") else f" {t['error'][:60]}" if t.get("error") else ""
        L.append(f"| {t['label']} | {'PASS' if t['ok'] else 'FAIL'} | {t['payload_bytes']} | {t['psnr_db']} | {t['margin']} | {t['r']}{note} |")
    L.append("")
    L.append("## Quality sweep (1024×1024, 512B)")
    L.append("")
    L.append("| q | result | margin | r |")
    L.append("|---|---|---|---|")
    for s in report["sweep"]:
        L.append(f"| {s['q']} | {'PASS' if s['ok'] else 'FAIL'} | {s['margin']} | {s['r']} |")
    L.append("")
    L.append("## Randomized trial (20 images × 300B)")
    L.append("")
    for name, r in report["randomized_trial"].items():
        L.append(f"- **{name}**: {r['ok']}/{r['n']} passed")
    L.append("")
    L.append("## Downscale robustness (2× carrier, resize+recompress)")
    L.append("")
    L.append("| case | result | psnr dB | |")
    L.append("|---|---|---|---|")
    for t in report["multiscale"]:
        detail = t.get("error", "") or f"margin={t['margin']} r={t['r']}"
        L.append(f"| {t['label']} | {'PASS' if t['ok'] else 'FAIL'} | {t['psnr_db']} | {detail} |")
    L.append("")
    L.append("## False positives")
    L.append("")
    fp = report["false_positive"]
    L.append(f"- plain synthetic rejected: **{fp['plain_synthetic']}**; plain photo rejected: **{fp['plain_jpeg']}**")
    L.append("")
    if report.get("real_photos"):
        L.append("## Real photos through presets")
        L.append("")
        for t in report["real_photos"]:
            L.append(f"- {t['label']}: {'PASS' if t['ok'] else 'FAIL'} (psnr {t['psnr_db']} dB)")
        L.append("")
    L.append(f"Total harness time: {report['elapsed_seconds']}s")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    report = run_validation()
    jp, mp = write_report(report)
    print(f"\nreport written: {mp} (+ {jp.name})")
