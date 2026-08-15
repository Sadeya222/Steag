# Stegstr — Tier A validation report

Generated 2026-08-11 13:16:38 · engine: Tier A DCT-QIM + RS(255,191,64), per-bit redundant voting

## Config

```json
{
  "band_indices": [
    6,
    8,
    10,
    12,
    14,
    16
  ],
  "embed_quality": 70,
  "min_repetitions": 1,
  "max_repetitions": 12,
  "seed": 24301,
  "max_stream_bytes": 2097152
}
```

## Capacity (max payload bytes)

| size | r=2 | r=6 | r=12 |
|---|---|---|---|
| 512x512 | 938 | 174 | 0 |
| 1024x1024 | 4376 | 1320 | 556 |
| 1080x1080 | 4949 | 1511 | 747 |
| 1280x1280 | 7050 | 2275 | 1129 |
| 2160x2160 | 20229 | 6668 | 3230 |

## Preset matrix (recompress-only)

| case | result | payload | psnr dB | margin | r |
|---|---|---|---|---|---|
| whatsapp | PASS | 509 | 43.19 | 0.457 | 12 2 bytes repaired by RS |
| telegram-photo | PASS | 515 | 43.17 | 0.392 | 12 |
| instagram | PASS | 510 | 42.26 | 0.277 | 12 |
| worst-case | PASS | 255 | 43.93 | 0.439 | 12 |
| telegram-file | PASS | 253 | 43.01 | 0.427 | 12 |

## Quality sweep (1024×1024, 512B)

| q | result | margin | r |
|---|---|---|---|
| 60 | FAIL | 0.0 | 0 |
| 65 | PASS | 0.371 | 12 |
| 70 | PASS | 0.504 | 12 |
| 75 | PASS | 0.323 | 12 |
| 80 | PASS | 0.279 | 12 |
| 85 | PASS | 0.49 | 12 |
| 90 | PASS | 0.423 | 12 |
| 95 | PASS | 0.5 | 12 |

## Randomized trial (20 images × 300B)

- **whatsapp**: 20/20 passed
- **telegram-photo**: 20/20 passed
- **instagram**: 20/20 passed
- **worst-case**: 20/20 passed

## Downscale robustness (2× carrier, resize+recompress)

| case | result | psnr dB | |
|---|---|---|---|
| 2x-downscale q85 multiscale=False | FAIL | 0.0 | no payload recovered (not a Stegstr carrier, or the channel  |
| 2x-downscale q85 multiscale=True | PASS | 0.0 | margin=0.49 r=8 |
| 2x-downscale q80 multiscale=False | FAIL | 0.0 | no payload recovered (not a Stegstr carrier, or the channel  |
| 2x-downscale q80 multiscale=True | PASS | 0.0 | margin=0.248 r=8 |

## False positives

- plain synthetic rejected: **True**; plain photo rejected: **True**

## Real photos through presets

- photo1.jpg->whatsapp: PASS (psnr 45.5 dB)
- photo_1080x1080.jpg->instagram: PASS (psnr 43.37 dB)
- photo_2160x2160.jpg->whatsapp: PASS (psnr 42.55 dB)

Total harness time: 91.1s
