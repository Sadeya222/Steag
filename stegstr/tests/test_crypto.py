"""Layer 2 crypto + Layer 4 storage tests."""

import io
import os

import pytest
from PIL import Image

from stegstr.codec import decode_image, encode_image
from stegstr.crypto import (
    CryptoError,
    decrypt_payload,
    encrypt_message,
    generate_keys,
    is_envelope,
    keys_from_secret,
    npub_of,
    nsec_of,
    parse_public_key,
    parse_secret_key,
)
from stegstr.db import (
    add_contact,
    connect,
    list_capsules,
    list_contacts,
    record_capsule,
    update_capsule_status,
)
from stegstr.engine import StegError


@pytest.fixture(scope="module")
def keypair():
    return generate_keys()


def test_keys_roundtrip(keypair):
    npub = npub_of(keypair.public_key())
    nsec = nsec_of(keypair.secret_key())
    assert npub.startswith("npub1")
    assert nsec.startswith("nsec1")
    assert npub_of(keys_from_secret(nsec).public_key()) == npub
    assert npub_of(keys_from_secret(keypair.secret_key().to_hex()).public_key()) == npub
    assert parse_public_key(npub).to_hex() == keypair.public_key().to_hex()


def test_encrypt_decrypt_roundtrip():
    alice = generate_keys()
    bob = generate_keys()
    for msg in (b"hello bob", os.urandom(200), b"\x00\xff" * 50):
        env = encrypt_message(msg, alice, bob.public_key())
        assert is_envelope(env)
        dec = decrypt_payload(env, bob)
        assert dec.plaintext == msg
        assert dec.sender_npub == npub_of(alice.public_key())


def test_wrong_receiver_fails():
    alice = generate_keys()
    bob = generate_keys()
    eve = generate_keys()
    env = encrypt_message(b"secret", alice, bob.public_key())
    with pytest.raises(CryptoError):
        decrypt_payload(env, eve)
    # also: alice (sender) cannot decrypt with her own key
    with pytest.raises(CryptoError):
        decrypt_payload(env, alice)


def test_tampered_envelope_fails():
    alice = generate_keys()
    bob = generate_keys()
    env = bytearray(encrypt_message(b"secret", alice, bob.public_key()))
    env[-5] ^= 0x01  # corrupt ciphertext body
    with pytest.raises(CryptoError):
        decrypt_payload(bytes(env), bob)
    env2 = bytearray(encrypt_message(b"secret", alice, bob.public_key()))
    env2[4] = 99  # bad version
    with pytest.raises(CryptoError):
        decrypt_payload(bytes(env2), bob)
    with pytest.raises(CryptoError):
        decrypt_payload(b"not an envelope at all", bob)


def test_bad_keys():
    with pytest.raises(CryptoError):
        parse_secret_key("nsec1definitelynotvalid!!!")
    with pytest.raises(CryptoError):
        parse_public_key("npub1broken")
    with pytest.raises(CryptoError):
        parse_secret_key("zz")

def _make_img(w=512, h=512, seed=1):
    import numpy as np
    rng = np.random.RandomState(seed)
    base = np.clip(rng.normal(120, 45, (h, w, 3)), 0, 255).astype(np.uint8)
    y, x = np.mgrid[0:h, 0:w]
    base = np.clip(base + (30 * np.sin(x / 50) + 20 * np.cos(y / 40))[..., None], 0, 255)
    return Image.fromarray(base.astype(np.uint8))


def test_full_pipeline_encrypted():
    """encrypt -> embed -> extract -> decrypt, losslessly and via JPEG."""
    alice = generate_keys()
    bob = generate_keys()
    msg = "meet at 7 \u2014 the bridge, bring the blue umbrella \U0001f382".encode("utf-8")
    img = _make_img()
    env = encrypt_message(msg, alice, bob.public_key())
    carrier = encode_image(img, env)
    payload, meta = decode_image(carrier)
    assert payload == env
    dec = decrypt_payload(payload, bob)
    assert dec.plaintext == msg

    # via a JPEG re-encode (WhatsApp-like)
    buf = io.BytesIO()
    carrier.save(buf, "JPEG", quality=85, subsampling=2)
    buf.seek(0)
    received = Image.open(buf).convert("RGB")
    payload2, _ = decode_image(received)
    assert decrypt_payload(payload2, bob).plaintext == msg


def test_plain_image_has_no_envelope():
    img = _make_img()
    with pytest.raises(StegError):
        decode_image(img)


def test_db_capsules(tmp_path):
    db = tmp_path / "test.db"
    cid = record_capsule("sent", image_sha256="abc", sender_npub="npub1a",
                         receiver_npub="npub1b", payload_type="nip44",
                         payload_bytes=123, status="encoded", path=db)
    assert cid >= 1
    update_capsule_status(cid, "decoded", meta={"margin": 0.9}, path=db)
    rows = list_capsules(path=db)
    assert len(rows) == 1
    assert rows[0]["status"] == "decoded"
    assert rows[0]["meta_json"] is not None
    assert "margin" in rows[0]["meta_json"]
    add_contact("npub1c", label="test", path=db)
    assert len(list_contacts(path=db)) == 1


def test_db_schema_idempotent(tmp_path):
    db = tmp_path / "test2.db"
    connect(db)
    connect(db)  # second call must not raise
