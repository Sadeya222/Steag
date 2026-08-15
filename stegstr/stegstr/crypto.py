"""Layer 2 — payload crypto: encrypt-before-embed via NIP-44 ECDH.

Key design (mirrors the architecture doc):
- The payload that goes into the stego engine is **already encrypted**:
  even if someone successfully extracts the raw bits, they only get
  NIP-44 ciphertext, unreadable without the receiver's secret key.
- Key derivation is exactly NIP-44: ECDH(secret_key, counterpart_public_key)
  -> shared x-coordinate -> HKDF -> AES-GCM.  ``nostr-sdk`` implements this
  natively (``nip44_encrypt`` / ``nip44_decrypt``), so no separate crypto
  library is needed — the sender/receiver *Nostr keypairs* are the keys.
- Envelope framing makes the image self-decryptable for the intended
  receiver: it carries the sender's public key in the clear (pubkeys are
  public), so the receiver derives the same shared secret from their own
  secret key + the sender's pubkey, with no out-of-band key exchange.
  Intercepting the picture alone is still useless without the receiver key.
- Integrity: NIP-44's AES-GCM tag authenticates the ciphertext, the engine
  adds RS + CRC32 at the byte level, and the layer-1 framing validates the
  full stream — a clean decode vs. corrupted is always confirmable.

Envelope (what gets embedded):
    b"STG1" | version(1) | sender_pubkey(32 raw bytes) | nip44_ciphertext(ascii)
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import nostr_sdk as ns

ENVELOPE_MAGIC = b"STG1"
ENVELOPE_VERSION = 1
_HEADER_LEN = len(ENVELOPE_MAGIC) + 1 + 32


class CryptoError(Exception):
    """Raised for crypto-level failures (framing, decryption, keys)."""


@dataclass
class DecryptedPayload:
    plaintext: bytes
    sender_pubkey_hex: str
    sender_npub: str


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------
def generate_keys() -> ns.Keys:
    return ns.Keys.generate()


def parse_secret_key(key: str) -> ns.SecretKey:
    """Parse a secret key from nsec bech32 or 64-hex, or an optional 0x prefix."""
    key = key.strip()
    try:
        if key.startswith("nsec1"):
            return ns.SecretKey.parse(key)
        return ns.SecretKey.parse(key.removeprefix("0x"))
    except Exception as exc:  # noqa: BLE001 — binding raises generic exceptions
        raise CryptoError(f"invalid secret key: {exc}") from exc


def parse_public_key(key: str) -> ns.PublicKey:
    """Parse a public key from npub bech32 or 64-hex."""
    key = key.strip()
    try:
        if key.startswith("npub1"):
            return ns.PublicKey.parse(key)
        return ns.PublicKey.parse(key.removeprefix("0x"))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"invalid public key: {exc}") from exc


def keys_from_secret(key: str) -> ns.Keys:
    return ns.Keys(parse_secret_key(key))


def npub_of(pk: ns.PublicKey) -> str:
    return pk.to_bech32()


def nsec_of(sk: ns.SecretKey) -> str:
    return sk.to_bech32()


# --------------------------------------------------------------------------
# encrypt / decrypt
# --------------------------------------------------------------------------
def encrypt_message(message: bytes, sender: ns.Keys, receiver_pk: ns.PublicKey) -> bytes:
    """Encrypt arbitrary bytes for ``receiver_pk``; returns the embeddable envelope."""
    if len(message) > 60 * 1024:
        raise CryptoError("message too large for NIP-44 (64 KiB plaintext cap)")
    # NIP-44 content is UTF-8 text; carry arbitrary bytes as base64 inside.
    b64 = base64.b64encode(message).decode("ascii")
    try:
        ciphertext = ns.nip44_encrypt(sender.secret_key(), receiver_pk, b64, ns.Nip44Version.V2)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"NIP-44 encryption failed: {exc}") from exc
    return (
        ENVELOPE_MAGIC
        + bytes([ENVELOPE_VERSION])
        + bytes.fromhex(sender.public_key().to_hex())
        + ciphertext.encode("ascii")
    )


def decrypt_payload(payload: bytes, receiver: ns.Keys) -> DecryptedPayload:
    """Decrypt an embedded envelope with the receiver's own keypair."""
    if not payload.startswith(ENVELOPE_MAGIC):
        raise CryptoError("payload is not a Stegstr-crypt envelope")
    if len(payload) < _HEADER_LEN + 10:
        raise CryptoError("envelope truncated")
    version = payload[len(ENVELOPE_MAGIC)]
    if version != ENVELOPE_VERSION:
        raise CryptoError(f"unsupported envelope version {version}")
    sender_raw = payload[len(ENVELOPE_MAGIC) + 1 : _HEADER_LEN]
    try:
        sender_pk = ns.PublicKey.from_bytes(bytes(sender_raw))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"bad sender pubkey in envelope: {exc}") from exc
    ciphertext = payload[_HEADER_LEN:].decode("ascii", errors="strict")
    try:
        plaintext_b64 = ns.nip44_decrypt(receiver.secret_key(), sender_pk, ciphertext)
    except Exception as exc:  # noqa: BLE001 — wrong key / tampered
        raise CryptoError(
            f"NIP-44 decryption failed (wrong key, or payload tampered): {exc}"
        ) from exc
    try:
        plaintext = base64.b64decode(plaintext_b64.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"ciphertext decoded but inner base64 invalid: {exc}") from exc
    return DecryptedPayload(
        plaintext=plaintext,
        sender_pubkey_hex=sender_pk.to_hex(),
        sender_npub=sender_pk.to_bech32(),
    )


def is_envelope(payload: bytes) -> bool:
    return payload.startswith(ENVELOPE_MAGIC)
