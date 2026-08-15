"""Layer 3 — Nostr networking.

Nostr's job here is NOT to transport the image (relays are for small events,
not media) — it is identity, key exchange, and sync:

- ``NostrClient``: multi-relay ``nostr-sdk`` Client wrapper (redundant relays
  = reliability, explicitly judged).  Publishes pre-signed events, fetches
  history, streams live notifications.
- **Capsule events** (kind 37300, parameterized-replaceable by ``d``):
  a small state event per exchange, syncing ``sent -> delivered -> decoded``
  between sender and receiver — the "syncing" requirement, explicitly.
- **NIP-94 file metadata events** (kind 1063) referencing a **Blossom** blob
  (SHA-256 content-addressed): when the app distributes images through Nostr
  itself, downloads are hash-verified and bit-exact — no silent re-encode.
- **NIP-17 gift-wrapped DMs**: exchange per-image extraction metadata
  (capsule id, blob hash, embed quality) out-of-band from the image itself —
  intercepting the picture alone is not enough.

Blossom is spoken over plain HTTPS (PUT /upload, GET /{sha256}) with a
NIP-98 ``Authorization`` header when a keypair is provided.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

import nostr_sdk as ns

log = logging.getLogger("stegstr.net")

CAPSULE_KIND = 37300            # parameterized-replaceable capsule state
FILE_METADATA_KIND = 1063       # NIP-94
GIFT_WRAP_KIND = 1059           # NIP-59
AUTH_KIND = 27235               # NIP-98
CAPSULE_TAG = "stegstr-capsule"
CAPSULE_STATUSES = ("sent", "delivered", "decoded")

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
]

_P = ns.SingleLetterTag.from_byte(ord("p"))
_D = ns.SingleLetterTag.from_byte(ord("d"))


class NetError(Exception):
    """Raised for networking-layer failures."""


def _dur(seconds: float) -> datetime.timedelta:
    return datetime.timedelta(seconds=seconds)


def parse_relay_url(url: str) -> ns.RelayUrl:
    try:
        return ns.RelayUrl.parse(url)
    except Exception as exc:  # noqa: BLE001
        raise NetError(f"invalid relay URL {url!r}: {exc}") from exc


def run_sync(coro) -> object:
    """Run an async net operation to completion (one-shot CLI ops)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# NostrClient — multi-relay wrapper
# --------------------------------------------------------------------------
class NostrClient:
    """Async wrapper around ``nostr-sdk`` Client over several relays."""

    def __init__(self, keys: ns.Keys | None = None, relays: list[str] | None = None):
        self.keys = keys
        self.relays = list(relays or DEFAULT_RELAYS)
        self.client = ns.Client()

    async def connect(self, timeout: float = 8.0) -> None:
        """Add all relays and connect.  Failures are logged, not fatal."""
        for url in self.relays:
            try:
                await self.client.add_relay(parse_relay_url(url))
            except Exception as exc:  # noqa: BLE001
                log.warning("relay %s rejected: %s", url, exc)
        await self.client.connect(_dur(timeout))
        log.debug("connected to %d relay(s)", len(self.relays))

    async def publish(self, event: ns.Event) -> str:
        """Publish a pre-signed event; returns its id (hex)."""
        try:
            await self.client.send_event(event)
        except Exception as exc:  # noqa: BLE001
            raise NetError(f"publish failed: {exc}") from exc
        return event.id().to_hex()

    async def fetch(self, filters: list[ns.Filter], timeout: float = 10.0,
                    max_events: int | None = 100) -> list[ns.Event]:
        """Fetch historical events matching the filters."""
        try:
            return await self.client.fetch_events(
                ns.ReqTarget.auto(filters), _dur(timeout), None, max_events
            )
        except Exception as exc:  # noqa: BLE001
            raise NetError(f"fetch failed: {exc}") from exc

    async def stream(self, filters: list[ns.Filter], on_event: Callable[[ns.Event], None],
                     timeout: float = 30.0, num_events: int | None = None,
                     on_close: Callable[[], None] | None = None) -> None:
        """Subscribe and stream live events, calling ``on_event`` per event.

        Blocks until the stream closes (timeout / EOSE policy / shutdown).
        """
        policy = ns.ReqExitPolicy.WAIT_FOR_EVENTS(num=num_events) if num_events else None
        try:
            stream = await self.client.stream_events(
                ns.ReqTarget.auto(filters), None, _dur(timeout), policy
            )
        except Exception as exc:  # noqa: BLE001
            raise NetError(f"subscribe failed: {exc}") from exc
        while True:
            coro = stream.next()
            if coro is None:  # stream terminated
                break
            item = await coro
            if item is None:
                break
            if item.event is not None:
                on_event(item.event)
        if on_close:
            on_close()

    async def shutdown(self) -> None:
        try:
            await self.client.shutdown()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# capsule events (kind 37300)
# --------------------------------------------------------------------------
def build_capsule_event(keys: ns.Keys, capsule_uuid: str, status: str,
                        content: dict, receiver_pk: ns.PublicKey | None = None,
                        extra_tags: list[ns.Tag] | None = None,
                        created_at: int | None = None) -> ns.Event:
    """Sign a capsule event: kind 37300, ``d`` = capsule uuid, ``p`` = receiver.

    ``created_at`` (unix seconds) is used for state updates published in the
    same second as a predecessor: kind 37300 is parameterized-replaceable, so
    the relay keeps the newest event per (author, d) — monotonic timestamps
    guarantee the later state wins even within one second.
    """
    if status not in CAPSULE_STATUSES:
        raise NetError(f"invalid capsule status {status!r}")
    tags = [
        ns.Tag.parse(["d", capsule_uuid]),
        ns.Tag.parse(["t", CAPSULE_TAG]),
    ]
    if receiver_pk is not None:
        tags.append(ns.Tag.public_key(receiver_pk))
    if extra_tags:
        tags.extend(extra_tags)
    builder = ns.EventBuilder(ns.Kind(CAPSULE_KIND), json.dumps(content)).tags(tags)
    if created_at is not None:
        builder = builder.custom_created_at(ns.Timestamp.from_secs(int(created_at)))
    return builder.finalize(keys)


def capsule_from_event(event: ns.Event) -> dict | None:
    """Parse a capsule event; returns None if it isn't one (or is malformed)."""
    if event.kind().as_u16() != CAPSULE_KIND:
        return None
    tags = {t.to_vec()[0]: t.to_vec()[1:] for t in event.tags()}
    if tags.get("t") != [CAPSULE_TAG] or "d" not in tags:
        return None
    try:
        content = json.loads(event.content())
    except (ValueError, TypeError):
        return None
    if not isinstance(content, dict) or content.get("status") not in CAPSULE_STATUSES:
        return None
    return {
        "uuid": tags["d"][0],
        "status": content["status"],
        "content": content,
        "event_id": event.id().to_hex(),
        "author": event.author().to_hex(),
        "created_at": event.created_at().as_secs(),
        "verified": bool(event.verify()),
    }


# --------------------------------------------------------------------------
# NIP-94 file metadata + NIP-17 DM helpers
# --------------------------------------------------------------------------
def build_file_metadata_event(keys: ns.Keys, url: str, sha256: str,
                              mime: str, size: int,
                              caption: str = "") -> ns.Event:
    """NIP-94 kind 1063 event for a Blossom blob (x = sha256, content-addressed)."""
    tags = [
        ns.Tag.parse(["url", url]),
        ns.Tag.parse(["x", sha256]),
        ns.Tag.parse(["m", mime]),
        ns.Tag.parse(["size", str(size)]),
    ]
    return ns.EventBuilder(ns.Kind(FILE_METADATA_KIND), caption).tags(tags).finalize(keys)


def build_dm(keys: ns.Keys, receiver_pk: ns.PublicKey, message: str) -> ns.Event:
    """NIP-17 gift-wrapped private message (kind 1059)."""
    try:
        return ns.nip17_make_private_msg(keys, receiver_pk, message)
    except Exception as exc:  # noqa: BLE001
        raise NetError(f"NIP-17 wrap failed: {exc}") from exc


def unwrap_dm(keys: ns.Keys, gift_wrap_event: ns.Event) -> tuple[str, str] | None:
    """Unwrap a NIP-17 gift wrap -> (sender_hex, plaintext)."""
    if gift_wrap_event.kind().as_u16() != GIFT_WRAP_KIND:
        return None
    try:
        unwrapped = ns.UnwrappedGift.from_gift_wrap(keys, gift_wrap_event)
        rumor = unwrapped.rumor()
        return unwrapped.sender().to_hex(), rumor.content()
    except Exception as exc:  # noqa: BLE001 — wrong receiver / tampered
        log.debug("DM unwrap failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# Blossom — SHA-256 content-addressed media (stdlib HTTP, NIP-98 auth)
# --------------------------------------------------------------------------
class BlossomError(Exception):
    pass


def _nip98_auth(keys: ns.Keys, url: str, method: str,
                payload_sha256: str | None = None) -> str:
    """NIP-98 Authorization header value for an HTTP request."""
    tags = [ns.Tag.parse(["u", url]), ns.Tag.parse(["method", method])]
    if payload_sha256:
        tags.append(ns.Tag.parse(["payload", payload_sha256]))
    ev = ns.EventBuilder(ns.Kind(AUTH_KIND), "").tags(tags).finalize(keys)
    return "Nostr " + base64.b64encode(ev.as_json().encode("utf-8")).decode("ascii")


def blossom_upload(server: str, data: bytes, content_type: str,
                   keys: ns.Keys | None = None) -> dict:
    """PUT /upload to a Blossom server; returns the server's blob JSON."""
    url = server.rstrip("/") + "/upload"
    headers = {"Content-Type": content_type}
    if keys is not None:
        headers["Authorization"] = _nip98_auth(
            keys, url, "PUT", hashlib.sha256(data).hexdigest()
        )
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise BlossomError(f"upload failed HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except urllib.error.URLError as exc:
        raise BlossomError(f"upload failed: {exc}") from exc
    if blob.get("sha256") != hashlib.sha256(data).hexdigest():
        raise BlossomError("server returned wrong sha256")
    return blob


def blossom_get(server: str, sha256: str, timeout: int = 180) -> bytes:
    """GET /{sha256} from a Blossom server; verifies the download is bit-exact."""
    return blossom_get_url(server.rstrip("/") + "/" + sha256, sha256, timeout)


def blossom_get_url(url: str, sha256: str, timeout: int = 180) -> bytes:
    """GET any blob URL (e.g. from a capsule's blob.url) and verify sha256."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        raise BlossomError(f"download failed HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise BlossomError(f"download failed: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != sha256:
        raise BlossomError("blob hash mismatch — content was altered in transit")
    return data


# --------------------------------------------------------------------------
# higher-level flows (async) used by the CLI
# --------------------------------------------------------------------------
def capsule_filters(my_pk: ns.PublicKey | None = None,
                    capsule_uuid: str | None = None) -> list[ns.Filter]:
    """Filters for capsule events: addressed to me, and/or a specific capsule."""
    filt = ns.Filter().kind(ns.Kind(CAPSULE_KIND))
    if my_pk is not None:
        filt = filt.custom_tag(_P, my_pk.to_hex())
    if capsule_uuid:
        filt = filt.custom_tag(_D, capsule_uuid)
    return [filt]


@dataclass
class SendResult:
    capsule_uuid: str
    capsule_event_id: str
    file_event_id: str | None = None
    dm_event_id: str | None = None
    blob: dict | None = None


async def send_capsule(
    keys: ns.Keys,
    receiver_pk: ns.PublicKey,
    capsule_uuid: str,
    content: dict,
    relays: list[str] | None = None,
    blob: dict | None = None,
    dm_message: str | None = None,
) -> SendResult:
    """Publish a capsule event (+ optional NIP-94 ref and NIP-17 DM)."""
    client = NostrClient(keys, relays)
    await client.connect()
    try:
        status = content.get("status", "sent")
        event = build_capsule_event(keys, capsule_uuid, status, content, receiver_pk)
        capsule_event_id = await client.publish(event)

        file_event_id = None
        if blob is not None and blob.get("url"):
            fev = build_file_metadata_event(
                keys,
                blob["url"],
                blob.get("sha256", ""),
                blob.get("mime", "image/png"),
                int(blob.get("size", 0)),
            )
            file_event_id = await client.publish(fev)

        dm_event_id = None
        if dm_message:
            dm = build_dm(keys, receiver_pk, dm_message)
            dm_event_id = await client.publish(dm)
        return SendResult(capsule_uuid=capsule_uuid, capsule_event_id=capsule_event_id,
                          file_event_id=file_event_id, dm_event_id=dm_event_id, blob=blob)
    finally:
        await client.shutdown()


async def process_incoming_capsules(
    keys: ns.Keys,
    relays: list[str] | None = None,
    on_capsule: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Fetch all capsule events addressed to me; return parsed capsules (newest last)."""
    client = NostrClient(keys, relays)
    await client.connect()
    try:
        events = await client.fetch(capsule_filters(keys.public_key()), timeout=10.0)
        capsules = []
        for ev in events:
            parsed = capsule_from_event(ev)
            if parsed is None:
                continue
            capsules.append(parsed)
            if on_capsule:
                on_capsule(parsed)
        return capsules
    finally:
        await client.shutdown()


async def ack_capsule(keys: ns.Keys, receiver_pk: ns.PublicKey, capsule_uuid: str,
                      status: str, content: dict,
                      relays: list[str] | None = None,
                      created_at: int | None = None) -> str:
    """Publish a status ack (delivered/decoded) for a capsule."""
    client = NostrClient(keys, relays)
    await client.connect()
    try:
        event = build_capsule_event(keys, capsule_uuid, status, content, receiver_pk,
                                    created_at=created_at)
        return await client.publish(event)
    finally:
        await client.shutdown()
