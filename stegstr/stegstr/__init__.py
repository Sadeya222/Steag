"""Stegstr — contest-grade image steganography.

Layer 1 (steganographic engine, Tier A) + Layer 6 (validation harness) live here.
Layers 2-5 (crypto, Nostr networking, storage, interfaces) build on top.

The engine embeds an encrypted-ready payload into the mid-frequency DCT
coefficients of the luminance plane, with per-bit redundant voting across the
whole image and Reed-Solomon FEC (RS(255, 191, 64)) on the payload stream.
"""

__version__ = "0.1.0"
