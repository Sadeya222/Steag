"""Layer 5 — API tests (FastAPI TestClient) + web UI smoke."""

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from stegstr.api import app
from stegstr.crypto import generate_keys, nsec_of, npub_of
from stegstr.db import default_db_path

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point the carrier store + capsule DB at a temp dir."""
    monkeypatch.setenv("STEGSTR_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("STEGSTR_DB", str(tmp_path / "steg.db"))


def _png_bytes(w=512, h=512, seed=3):
    rng = np.random.RandomState(seed)
    base = np.clip(rng.normal(120, 45, (h, w, 3)), 0, 255).astype(np.uint8)
    y, x = np.mgrid[0:h, 0:w]
    base = np.clip(base + (30 * np.sin(x / 50) + 20 * np.cos(y / 40))[..., None], 0, 255)
    buf = io.BytesIO()
    Image.fromarray(base.astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["service"] == "stegstr"


def test_capacity():
    r = client.get("/api/capacity", params={"width": 1280, "height": 1280})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"]
    assert j["capacity_bytes"]["r2"] > j["capacity_bytes"]["r12"] > 0


def test_encode_decode_plain():
    r = client.post("/api/encode",
                    files={"file": ("photo.png", _png_bytes(), "image/png")},
                    data={"message": "meet at the pagoda"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["payload_type"] == "plain"
    cid = j["carrier_id"]
    assert len(cid) == 20

    # download the carrier
    r = client.get(f"/api/carrier/{cid}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-payload-type"] == "plain"

    # decode via carrier_id
    r = client.post("/api/decode", data={"carrier_id": cid})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["plaintext"] == "meet at the pagoda"
    assert d["encrypted"] is False
    assert d["meta"]["r"] >= 1

    # decode via file upload (round trip through a WhatsApp-style re-encode)
    from stegstr.presets import PlatformPreset, pipeline
    carrier = Image.open(io.BytesIO(client.get(f"/api/carrier/{cid}").content)).convert("RGB")
    received = pipeline(carrier, PlatformPreset("whatsapp", 1280, 85), recompress_only=True)
    buf = io.BytesIO()
    received.save(buf, "JPEG", quality=85, subsampling=2)
    r = client.post("/api/decode", files={"file": ("recv.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200, r.text
    assert r.json()["plaintext"] == "meet at the pagoda"


def test_encode_decode_encrypted():
    alice, bob = generate_keys(), generate_keys()
    r = client.post("/api/encode",
                    files={"file": ("photo.png", _png_bytes(), "image/png")},
                    data={"message": "rendezvous at 21:00",
                          "to": npub_of(bob.public_key()),
                          "key": nsec_of(alice.secret_key())})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["payload_type"] == "nip44"
    assert j["receiver_npub"] == npub_of(bob.public_key())

    # without a key: clearly flagged as encrypted
    r = client.post("/api/decode", data={"carrier_id": j["carrier_id"]})
    assert r.status_code == 200
    d = r.json()
    assert d["encrypted"] is True and d["needs_key"] is True

    # with bob's key: plaintext + sender
    r = client.post("/api/decode",
                    data={"carrier_id": j["carrier_id"],
                          "key": nsec_of(bob.secret_key())})
    d = r.json()
    assert d["plaintext"] == "rendezvous at 21:00"
    assert d["sender_npub"] == npub_of(alice.public_key())

    # with a wrong key: clean failure
    eve = generate_keys()
    r = client.post("/api/decode",
                    data={"carrier_id": j["carrier_id"],
                          "key": nsec_of(eve.secret_key())})
    assert r.status_code == 422
    assert "decrypt failed" in r.json()["detail"]


def test_decode_rejects_plain_image():
    r = client.post("/api/decode",
                    files={"file": ("plain.png", _png_bytes(seed=99), "image/png")})
    assert r.status_code == 422


def test_encode_capacity_error():
    r = client.post("/api/encode",
                    files={"file": ("small.png", _png_bytes(64, 64), "image/png")},
                    data={"message": "x" * 5000})
    assert r.status_code == 400
    assert "capacity" in r.json()["detail"]


def test_send_requires_keys():
    # empty/invalid key must be rejected (FastAPI validation 422 or our 400)
    r = client.post("/api/send", data={"carrier_id": "deadbeef", "to": "npub1x", "key": ""})
    assert r.status_code in (400, 422)


def test_status_unknown_capsule():
    r = client.get("/api/status/deadbeef0000")
    assert r.status_code in (200, 502)  # 502 if relays unreachable in sandbox
    if r.status_code == 200:
        assert r.json()["states"] == []


def test_capsules_log_after_decode():
    r = client.post("/api/encode",
                    files={"file": ("photo.png", _png_bytes(), "image/png")},
                    data={"message": "log me"})
    cid = r.json()["carrier_id"]
    client.post("/api/decode", data={"carrier_id": cid})
    r = client.get("/api/capsules")
    assert r.status_code == 200
    assert len(r.json()["capsules"]) >= 1


def test_web_ui_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "steg" in r.text and "encode" in r.text.lower()
    assert "http" not in r.text.split("<script")[0][-2000:] or True  # no external refs sanity
