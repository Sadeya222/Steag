"""Carrier store abstraction — local disk (default) or Vercel Blob.

Serverless deployments (Vercel) have an ephemeral filesystem: carriers must
live in object storage instead.  The store is chosen at call time:

    disk  -> $STEGSTR_DATA/carriers (default; local dev, Docker, VPS)
    blob  -> Vercel Blob          (when BLOB_READ_WRITE_TOKEN is set)

Both backends return the same CarrierRef(id, sha256, url, meta) so the rest
of the app is agnostic.  Blob URLs are immutable and content-addressed
(SHA-256 prefix as the key), so downloads are hash-verifiable — matching the
Blossom philosophy for the carrier itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class CarrierStoreError(Exception):
    pass


@dataclass
class CarrierRef:
    id: str
    sha256: str
    url: str | None        # None for disk backend
    meta: dict


def data_dir() -> Path:
    env = os.environ.get("STEGSTR_DATA")
    if env:
        d = Path(env)
    else:
        d = Path.home() / ".stegstr" / "data"
    (d / "carriers").mkdir(parents=True, exist_ok=True)
    return d


def _blob_token() -> str | None:
    return os.environ.get("BLOB_READ_WRITE_TOKEN")


def _backend() -> str:
    forced = os.environ.get("STEGSTR_STORAGE")
    if forced in ("disk", "blob"):
        return forced
    return "blob" if _blob_token() else "disk"


# --------------------------------------------------------------------------
# Vercel Blob client (thin; vercel_blob would add a dep — same REST API)
# --------------------------------------------------------------------------
_BLOB_API = "https://api.vercel.com/v1/blob"


def _blob_put(pathname: str, data: bytes, token: str) -> dict:
    """PUT /v1/blob?pathname=...  (official Vercel Blob REST API)."""
    import urllib.parse
    url = f"{_BLOB_API}?pathname={urllib.parse.quote(pathname)}"
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CarrierStoreError(
            f"blob upload failed HTTP {exc.code}: {exc.read()[:200]!r}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CarrierStoreError(f"blob upload failed: {exc}") from exc


def _blob_head(pathname: str, token: str) -> dict | None:
    import urllib.parse
    url = f"{_BLOB_API}?pathname={urllib.parse.quote(pathname)}"
    req = urllib.request.Request(
        url, method="HEAD",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ct = resp.headers.get("Content-Type", "")
            return {"size": int(resp.headers.get("Content-Length", 0)),
                    "content_type": ct}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CarrierStoreError(f"blob head failed HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CarrierStoreError(f"blob head failed: {exc}") from exc


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def store_carrier(carrier_bytes: bytes, meta: dict) -> CarrierRef:
    """Save a carrier; returns its reference.  The id IS the sha256 prefix."""
    sha = hashlib.sha256(carrier_bytes).hexdigest()
    carrier_id = sha[:20]
    meta = dict(meta, id=carrier_id, sha256=sha, created_at=int(time.time()))
    if _backend() == "blob":
        token = _blob_token()
        key = f"stegstr/{carrier_id}.png"
        blob = _blob_put(key, carrier_bytes, token)
        _blob_put(f"stegstr/{carrier_id}.json",
                  json.dumps(meta).encode("utf-8"), token)
        return CarrierRef(id=carrier_id, sha256=sha, url=blob["url"], meta=meta)
    d = data_dir() / "carriers"
    (d / f"{carrier_id}.png").write_bytes(carrier_bytes)
    (d / f"{carrier_id}.json").write_text(json.dumps(meta))
    return CarrierRef(id=carrier_id, sha256=sha, url=None, meta=meta)


def _blob_get_url(url: str, timeout: int = 60) -> bytes:
    import urllib.request as _ur
    try:
        with _ur.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise CarrierStoreError(f"blob fetch failed HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CarrierStoreError(f"blob fetch failed: {exc}") from exc


def load_carrier(carrier_id: str) -> tuple[bytes, dict]:
    """Fetch a carrier's bytes + metadata (disk or blob backend)."""
    if _backend() == "blob":
        base = f"https://{carrier_id}.public.blob.vercel-storage.com"
        data = _blob_get_url(f"{base}/stegstr/{carrier_id}.png")
        meta = json.loads(_blob_get_url(f"{base}/stegstr/{carrier_id}.json"))
        if hashlib.sha256(data).hexdigest() != meta.get("sha256", ""):
            raise CarrierStoreError("carrier hash mismatch — blob altered")
        return data, meta
    d = data_dir() / "carriers"
    img = d / f"{carrier_id}.png"
    meta = d / f"{carrier_id}.json"
    if not img.exists():
        raise CarrierStoreError(f"carrier {carrier_id} not found")
    data = img.read_bytes()
    meta_obj = json.loads(meta.read_text()) if meta.exists() else {}
    return data, meta_obj


def carrier_url(carrier_id: str) -> str | None:
    """Public URL for a stored carrier (blob backend only)."""
    if _backend() == "blob":
        return f"https://{carrier_id}.public.blob.vercel-storage.com/stegstr/{carrier_id}.png"
    return None
