"""Layer 5 — agent-operable API (FastAPI + pydantic) and the local web UI.

The API exposes the full Stegstr flow as documented JSON endpoints so that
AI agents (and the web UI, and the MCP wrapper) can drive it without a shell:

    GET  /api/health                     service info
    GET  /api/capacity?width=&height=    payload capacity table
    POST /api/encode                     multipart: file + message (+to/key) -> carrier_id
    GET  /api/carrier/{carrier_id}       download a carrier image (bytes)
    POST /api/decode                     multipart: file | carrier_id (+key) -> message
    POST /api/send                       carrier_id/file + to + key -> capsule published
    GET  /api/status/{capsule_uuid}      capsule state machine from relays
    GET  /api/capsules                   local capsule log
    GET  /                               single-page web UI (drag-drop)
    /docs, /openapi.json                 auto-generated API docs (FastAPI)

Design notes:
- CPU-heavy work (encode/decode) lives in sync endpoints -> FastAPI runs them
  in a threadpool; async endpoints await the nostr coroutines directly on the
  server loop (no nested event loops).
- Carriers are stored server-side by id (SHA-256 verified), so agents can
  encode once and pass the id around (decode / send / download) without
  shuttling image bytes back and forth.
- Secret keys are only ever accepted in request bodies (never URLs/logs).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .codec import (
    capacity_bytes,
    decode_image,
    encode_image,
    load_image,
    max_capacity_bytes,
    psnr,
)
from .crypto import (
    CryptoError,
    decrypt_payload,
    encrypt_message,
    is_envelope,
    keys_from_secret,
    npub_of,
    parse_public_key,
)
from .db import (
    add_contact,
    find_capsule,
    list_capsules,
    record_capsule,
    upsert_capsule_status,
)
from .engine import TierAConfig, StegError
from .net import (
    DEFAULT_RELAYS,
    NetError,
    NostrClient,
    blossom_upload,
    capsule_filters,
    capsule_from_event,
    send_capsule,
)
from .storage import (
    CarrierStoreError,
    carrier_url,
    load_carrier,
    store_carrier,
)

# --------------------------------------------------------------------------
# carrier store (disk or Vercel Blob; sha256-verified)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# core service functions (shared by API + MCP + CLI paths)
# --------------------------------------------------------------------------
def service_encode(file_bytes: bytes, message: str, to: str | None = None,
                   key: str | None = None, quality: int = 70,
                   multiscale: bool = False) -> dict:
    """Encode -> stores the carrier; returns the result descriptor."""
    payload = message.encode("utf-8")
    payload_type = "plain"
    sender_npub = receiver_npub = None
    if to:
        if not key:
            raise ValueError("--to requires a sender key")
        receiver_pk = parse_public_key(to)
        payload = encrypt_message(payload, keys_from_secret(key), receiver_pk)
        payload_type = "nip44"
        sender_npub = npub_of(keys_from_secret(key).public_key())
        receiver_npub = receiver_pk.to_bech32()

    cfg = TierAConfig(embed_quality=quality)
    img = load_image(io.BytesIO(file_bytes))
    cap, cap_r = max_capacity_bytes(img.width, img.height, cfg)
    if len(payload) > cap:
        raise ValueError(
            f"payload {len(payload)}B exceeds capacity {cap}B "
            f"(at redundancy r={cap_r}) for {img.width}x{img.height}"
        )
    carrier = encode_image(img, payload, cfg, multiscale=multiscale)
    buf = io.BytesIO()
    carrier.convert("RGB").save(buf, "PNG")
    carrier_bytes = buf.getvalue()

    ref = store_carrier(carrier_bytes, {
        "quality": quality, "multiscale": multiscale,
        "payload_bytes": len(payload), "payload_type": payload_type,
        "sender_npub": sender_npub, "receiver_npub": receiver_npub,
        "size": img.width * img.height,
    })
    return {
        "ok": True,
        "carrier_id": ref.id,
        "carrier_url": ref.url,
        "carrier_sha256": hashlib.sha256(carrier_bytes).hexdigest(),
        "carrier_bytes": len(carrier_bytes),
        "carrier_size": f"{img.width}x{img.height}",
        "payload_bytes": len(payload),
        "payload_type": payload_type,
        "quality": quality,
        "multiscale": multiscale,
        "sender_npub": sender_npub,
        "receiver_npub": receiver_npub,
    }


def service_decode(file_bytes: bytes, key: str | None = None,
                   quality: int = 70) -> dict:
    """Decode (+ optional decrypt).  Raises ValueError on failure."""
    cfg = TierAConfig(embed_quality=quality)
    payload, meta = decode_image(load_image(io.BytesIO(file_bytes)), cfg)
    result = {
        "ok": True,
        "meta": {
            "r": meta.get("r"), "scale_key": meta.get("scale_key"),
            "corrected_bytes": meta.get("corrected_bytes", 0),
            "margin": round(meta.get("margin", 0.0), 4),
            "payload_bytes": len(payload),
        },
    }
    if is_envelope(payload):
        result["encrypted"] = True
        if key:
            try:
                dec = decrypt_payload(payload, keys_from_secret(key))
            except CryptoError as exc:
                raise ValueError(f"decrypt failed: {exc}") from exc
            result["sender_npub"] = dec.sender_npub
            result["sender_hex"] = dec.sender_pubkey_hex
            result["plaintext"] = _safe_text(dec.plaintext)
            try:
                dec.plaintext.decode("utf-8")
                result["binary"] = False
            except UnicodeDecodeError:
                result["binary"] = True
                result["payload_base64"] = base64.b64encode(dec.plaintext).decode()
        else:
            result["needs_key"] = True
    else:
        result["encrypted"] = False
        result["plaintext"] = _safe_text(payload)
        try:
            payload.decode("utf-8")
            result["binary"] = False
        except UnicodeDecodeError:
            result["binary"] = True
            result["payload_base64"] = base64.b64encode(payload).decode()
    return result


def _safe_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return "<binary payload — see _bytes (base64)>"


def service_capacity(width: int, height: int, quality: int = 70) -> dict:
    cfg = TierAConfig(embed_quality=quality)
    return {
        "ok": True,
        "width": width, "height": height, "quality": quality,
        "capacity_bytes": {
            f"r{r}": capacity_bytes(width, height, cfg, repetitions=r)
            for r in (1, 2, 3, 4, 6, 8, 12)
        },
    }


def service_status(capsule_uuid: str, relays: list[str] | None = None) -> dict:
    """Fetch the capsule state machine from relays (public, no key needed)."""
    import asyncio

    async def _fetch():
        client = NostrClient(None, relays or DEFAULT_RELAYS)
        await client.connect()
        try:
            events = await client.fetch(
                capsule_filters(capsule_uuid=capsule_uuid), timeout=10.0
            )
        finally:
            await client.shutdown()
        return events

    events = asyncio.run(_fetch())
    states = sorted(
        (capsule_from_event(e) for e in events if capsule_from_event(e)),
        key=lambda c: c["created_at"],
    )
    return {
        "ok": True,
        "capsule_uuid": capsule_uuid,
        "states": [
            {
                "status": c["status"],
                "author": c["author"],
                "created_at": c["created_at"],
                "verified": c["verified"],
                "event_id": c["event_id"],
                "content": c["content"],
            }
            for c in states
        ],
    }


def service_send(carrier_bytes: bytes, to: str, key: str,
                 relays: list[str] | None = None, blossom: str | None = None,
                 note: str | None = None, quality: int = 70) -> dict:
    """Publish the capsule (+ Blossom hosting + NIP-17 metadata DM)."""
    import asyncio

    sender = keys_from_secret(key)
    receiver_pk = parse_public_key(to)

    payload_type, payload_len = "unknown", None
    try:
        payload, meta = decode_image(load_image(io.BytesIO(carrier_bytes)),
                                     TierAConfig(embed_quality=quality))
        payload_type = "nip44" if is_envelope(payload) else "plain"
        payload_len = len(payload)
    except (StegError, Exception):  # noqa: BLE001 — warn-only for send
        pass

    sha = hashlib.sha256(carrier_bytes).hexdigest()
    capsule_uuid = uuid.uuid4().hex[:12]

    blob = None
    if blossom:
        mime = "image/png"
        try:
            blob = blossom_upload(blossom, carrier_bytes, mime, sender)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Blossom upload failed: {exc}") from exc

    content = {
        "v": 1, "status": "sent",
        "image_sha256": sha,
        "size": len(carrier_bytes),
        "payload_type": payload_type,
        "payload_len": payload_len,
        "embed_quality": quality,
        "blob": {"url": blob["url"], "sha256": blob["sha256"]} if blob else None,
        "note": note,
        "relays": relays or DEFAULT_RELAYS,
    }
    dm_message = json.dumps({
        "capsule": capsule_uuid,
        "image_sha256": sha,
        "blob": {"url": blob["url"], "sha256": blob["sha256"]} if blob else None,
        "embed_quality": quality,
        "payload_type": payload_type,
    })

    result = asyncio.run(send_capsule(
        sender, receiver_pk, capsule_uuid, content,
        relays or DEFAULT_RELAYS, blob=blob, dm_message=dm_message,
    ))

    record_capsule(
        direction="sent",
        image_sha256=sha,
        sender_npub=npub_of(sender.public_key()),
        receiver_npub=receiver_pk.to_bech32(),
        payload_type=payload_type if payload_type != "unknown" else "plain",
        payload_bytes=payload_len,
        status="sent",
        capsule_uuid=capsule_uuid,
        event_id=result.capsule_event_id,
        meta={"blob": blob, "note": note},
    )
    add_contact(receiver_pk.to_bech32(), label="")
    return {
        "ok": True,
        "capsule_uuid": capsule_uuid,
        "capsule_event_id": result.capsule_event_id,
        "file_event_id": result.file_event_id,
        "dm_event_id": result.dm_event_id,
        "blob": blob,
        "payload_type": payload_type,
        "payload_len": payload_len,
    }


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------
app = FastAPI(
    title="Stegstr API",
    version=__version__,
    description=(
        "Agent-operable interface to Stegstr: encode/decode hidden messages in "
        "images that survive WhatsApp/Instagram/Telegram re-compression, with "
        "NIP-44 encryption and Nostr capsule sync. See the web UI at /."
    ),
)

# CORS for split-hosting (e.g. static frontend on Vercel, API elsewhere).
# Configure with STEGSTR_CORS_ORIGINS (comma list) or STEGSTR_CORS_ALLOW_ALL=true.
_cors_origins = os.environ.get("STEGSTR_CORS_ORIGINS")
if os.environ.get("STEGSTR_CORS_ALLOW_ALL", "").lower() in ("1", "true", "yes"):
    _cors_origins = "*"
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors_origins.split(",")],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "stegstr", "version": __version__}


@app.get("/api/capacity")
def capacity_endpoint(width: int = Query(1024, ge=64),
                      height: int = Query(1024, ge=64),
                      quality: int = Query(70, ge=1, le=100)) -> dict:
    return service_capacity(width, height, quality)


@app.post("/api/encode")
def encode_endpoint(
    file: UploadFile = File(..., description="Carrier image (PNG/JPEG)"),
    message: str = Form(..., description="Message text"),
    to: str | None = Form(None, description="Receiver npub/hex — enables NIP-44 encryption"),
    key: str | None = Form(None, description="Sender nsec/hex (required with `to`)"),
    quality: int = Form(70, description="Embed quality (1-100; default 70)"),
    multiscale: bool = Form(False, description="Embed an extra half-scale copy"),
) -> dict:
    """Embed a message (optionally NIP-44 encrypted) into an uploaded image.

    Returns a carrier_id — the carrier is stored server-side; fetch it with
    GET /api/carrier/{id} or pass the id straight to /api/decode or /api/send.
    """
    data = file.file.read()
    try:
        return service_encode(data, message, to, key, quality, multiscale)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except CarrierStoreError as exc:
        raise HTTPException(503, f"carrier storage unavailable: {exc}") from exc


@app.get("/api/carrier/{carrier_id}")
def carrier_endpoint(carrier_id: str) -> Response:
    """Download a stored carrier image (PNG bytes, or 302 to the blob URL)."""
    url = carrier_url(carrier_id)
    if url:
        return RedirectResponse(url)
    try:
        data, meta = load_carrier(carrier_id)
    except CarrierStoreError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Content-Disposition": f'attachment; filename="carrier-{carrier_id}.png"',
            "X-Carrier-Id": carrier_id,
            "X-Carrier-Sha256": meta.get("sha256", ""),
            "X-Payload-Bytes": str(meta.get("payload_bytes", "")),
            "X-Payload-Type": meta.get("payload_type", ""),
        },
    )


class DecodeIn(BaseModel):
    carrier_id: str | None = None
    key: str | None = None
    quality: int = 70


@app.post("/api/decode")
def decode_endpoint(
    file: UploadFile | None = File(None, description="Received image"),
    carrier_id: str | None = Form(None, description="Stored carrier id (alternative to file)"),
    key: str | None = Form(None, description="Your nsec/hex — decrypts NIP-44 payloads"),
    quality: int = Form(70, description="Embed quality used at encode time"),
) -> dict:
    """Recover a message from a carrier image (upload or stored carrier_id)."""
    if file is not None:
        data = file.file.read()
    elif carrier_id:
        try:
            data, _ = load_carrier(carrier_id)
        except CarrierStoreError as exc:
            raise HTTPException(404, str(exc)) from exc
    else:
        raise HTTPException(400, "provide either `file` or `carrier_id`")
    try:
        result = service_decode(data, key, quality)
    except (ValueError, StegError, Exception) as exc:  # noqa: BLE001
        raise HTTPException(422, f"decode failed: {exc}") from exc
    # log to the capsule DB
    try:
        record_capsule(
            direction="received",
            image_sha256=hashlib.sha256(data).hexdigest(),
            payload_type="nip44" if result.get("encrypted") else "plain",
            payload_bytes=result.get("meta", {}).get("payload_bytes"),
            status="decoded" if (not result.get("encrypted") or result.get("plaintext")) else "delivered",
            meta={"source": "api", "margin": result.get("meta", {}).get("margin")},
        )
    except Exception:  # noqa: BLE001 — logging must never break the response
        pass
    return result


@app.post("/api/send")
def send_endpoint(
    file: UploadFile | None = File(None, description="Carrier image (or use carrier_id)"),
    carrier_id: str | None = Form(None, description="Stored carrier id"),
    to: str = Form(..., description="Receiver npub/hex"),
    key: str = Form(..., description="Your nsec/hex"),
    relays: str | None = Form(None, description="Comma-separated relay URLs"),
    blossom: str | None = Form(None, description="Blossom server URL"),
    note: str | None = Form(None, description="Human note in the capsule"),
    quality: int = Form(70),
) -> dict:
    """Publish a capsule for a carrier: sent -> receiver syncs to delivered/decoded."""
    if file is not None:
        data = file.file.read()
    elif carrier_id:
        try:
            data, _ = load_carrier(carrier_id)
        except CarrierStoreError as exc:
            raise HTTPException(404, str(exc)) from exc
    else:
        raise HTTPException(400, "provide either `file` or `carrier_id`")
    relay_list = [r.strip() for r in relays.split(",") if r.strip()] if relays else None
    try:
        return service_send(data, to, key, relay_list, blossom, note, quality)
    except (ValueError, NetError, Exception) as exc:  # noqa: BLE001
        raise HTTPException(400, f"send failed: {exc}") from exc


@app.get("/api/status/{capsule_uuid}")
def status_endpoint(capsule_uuid: str,
                    relays: str | None = Query(None)) -> dict:
    """Capsule state machine (sent/delivered/decoded) from the relays."""
    relay_list = [r.strip() for r in relays.split(",") if r.strip()] if relays else None
    try:
        return service_status(capsule_uuid, relay_list)
    except (NetError, Exception) as exc:  # noqa: BLE001
        raise HTTPException(502, f"status fetch failed: {exc}") from exc


@app.get("/api/capsules")
def capsules_endpoint(limit: int = Query(50, le=200)) -> dict:
    """Local capsule log (Layer 4 SQLite)."""
    return {"ok": True, "capsules": list_capsules(limit=limit)}


@app.get("/", response_class=HTMLResponse)
def web_ui() -> str:
    from .webui import PAGE
    return PAGE
