"""Storage backend tests: disk (default) + Vercel Blob (stubbed)."""

import hashlib

import pytest

import stegstr.storage as storage
from stegstr.storage import CarrierStoreError, load_carrier, store_carrier


@pytest.fixture()
def disk_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEGSTR_STORAGE", "disk")
    monkeypatch.setenv("STEGSTR_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    return tmp_path


def test_disk_roundtrip(disk_env):
    data = b"\x89PNG fake carrier bytes"
    ref = store_carrier(data, {"payload_type": "plain", "payload_bytes": 5})
    assert ref.id == hashlib.sha256(data).hexdigest()[:20]
    assert ref.url is None
    got, meta = load_carrier(ref.id)
    assert got == data
    assert meta["payload_type"] == "plain"
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()


def test_disk_missing(disk_env):
    with pytest.raises(CarrierStoreError):
        load_carrier("deadbeefdeadbeef0000")


def test_blob_roundtrip(tmp_path, monkeypatch):
    """Blob backend with stubbed Vercel Blob REST + public URL fetch."""
    monkeypatch.setenv("STEGSTR_STORAGE", "blob")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "test-token")

    blobs: dict[str, bytes] = {}

    def fake_put(pathname, data, token):
        assert token == "test-token"
        blobs[pathname] = data
        return {"url": f"https://fake.public.blob.vercel-storage.com/{pathname}"}

    def fake_get(url, timeout=60):
        key = url.split(".com/", 1)[1]
        if key not in blobs:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "no", None, None)
        return blobs[key]

    monkeypatch.setattr(storage, "_blob_put", fake_put)
    monkeypatch.setattr(storage, "_blob_get_url", fake_get)

    data = b"blob carrier bytes " * 10
    ref = store_carrier(data, {"payload_type": "nip44"})
    assert ref.url is not None
    assert ref.id == hashlib.sha256(data).hexdigest()[:20]
    assert f"stegstr/{ref.id}.png" in blobs and f"stegstr/{ref.id}.json" in blobs

    got, meta = load_carrier(ref.id)
    assert got == data
    assert meta["payload_type"] == "nip44"
    assert storage.carrier_url(ref.id).endswith(f"/stegstr/{ref.id}.png")


def test_blob_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("STEGSTR_STORAGE", "blob")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "t")

    def fake_get(url, timeout=60):
        raise CarrierStoreError("blob fetch failed HTTP 404")

    monkeypatch.setattr(storage, "_blob_get_url", fake_get)
    with pytest.raises(CarrierStoreError):
        load_carrier("00" * 20)
