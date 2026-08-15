"""Layer 3 — Nostr networking tests (hermetic: local relay + stub Blossom)."""

import asyncio
import datetime
import hashlib
import json

import numpy as np
import pytest

import nostr_sdk as ns

from helpers import AUTH_HEADERS, get_relay_url, run

from stegstr.net import (
    BlossomError,
    NostrClient,
    ack_capsule,
    blossom_get,
    blossom_upload,
    build_capsule_event,
    build_dm,
    build_file_metadata_event,
    capsule_filters,
    capsule_from_event,
    process_incoming_capsules,
    send_capsule,
    unwrap_dm,
)

DUR = lambda s: datetime.timedelta(seconds=s)  # noqa: E731
P = ns.SingleLetterTag.from_byte(ord("p"))


def test_capsule_publish_fetch(relay_url):
    alice, bob = ns.Keys.generate(), ns.Keys.generate()

    async def main():
        ca, cb = NostrClient(alice, [relay_url]), NostrClient(bob, [relay_url])
        await ca.connect(); await cb.connect()
        try:
            ev = build_capsule_event(alice, "caps-test-1", "sent",
                                     {"v": 1, "status": "sent", "note": "hi"},
                                     receiver_pk=bob.public_key())
            await ca.publish(ev)
            events = await cb.fetch(capsule_filters(bob.public_key()), timeout=8.0)
            assert len(events) >= 1
            parsed = capsule_from_event(events[0])
            assert parsed is not None
            assert parsed["uuid"] == "caps-test-1"
            assert parsed["status"] == "sent"
            assert parsed["author"] == alice.public_key().to_hex()
            assert parsed["verified"] is True
        finally:
            await ca.shutdown(); await cb.shutdown()

    run(main())


def test_capsule_live_ack(relay_url):
    alice, bob = ns.Keys.generate(), ns.Keys.generate()
    got = []
    done = asyncio.Event()

    async def main():
        ca, cb = NostrClient(alice, [relay_url]), NostrClient(bob, [relay_url])
        await ca.connect(); await cb.connect()
        try:
            filt = ns.Filter().kind(ns.Kind(37300)).custom_tag(P, alice.public_key().to_hex())
            policy = ns.ReqExitPolicy.WAIT_FOR_EVENTS(num=2)
            stream = await ca.client.stream_events(
                ns.ReqTarget.auto([filt]), None, DUR(10), policy
            )

            async def pump():
                while True:
                    coro = stream.next()
                    if coro is None:
                        break
                    item = await coro
                    if item is None:
                        break
                    if item.event is not None:
                        got.append(item.event.content())
                        done.set()

            task = asyncio.ensure_future(pump())
            await asyncio.sleep(0.3)

            ack = build_capsule_event(bob, "caps-test-1", "delivered",
                                      {"v": 1, "status": "delivered", "by": "bob"},
                                      receiver_pk=alice.public_key())
            await cb.publish(ack)
            await asyncio.wait_for(done.wait(), timeout=8)
            await task
        finally:
            await ca.shutdown(); await cb.shutdown()

    run(main())
    assert any(json.loads(x).get("status") == "delivered" for x in got), got


def test_nip17_dm_roundtrip(relay_url):
    alice, bob = ns.Keys.generate(), ns.Keys.generate()

    async def main():
        ca, cb = NostrClient(alice, [relay_url]), NostrClient(bob, [relay_url])
        await ca.connect(); await cb.connect()
        try:
            dm = build_dm(alice, bob.public_key(), "{\"capsule\": \"caps-x\"}")
            await ca.publish(dm)
            events = await cb.fetch([ns.Filter().kind(ns.Kind(1059))], timeout=8.0)
            # NIP-59 gift wraps use randomized outer keys: unwrap candidates
            # until one verifies (only DMs addressed to bob will unwrap)
            dec = None
            for e in events:
                dec = unwrap_dm(bob, e)
                if dec:
                    break
            assert dec is not None
            sender_hex, text = dec
            assert sender_hex == alice.public_key().to_hex()
            assert json.loads(text)["capsule"] == "caps-x"
        finally:
            await ca.shutdown(); await cb.shutdown()

    run(main())


def test_nip94_file_metadata():
    keys = ns.Keys.generate()
    ev = build_file_metadata_event(keys, "https://cdn.example/abc", "ab" * 32,
                                   "image/png", 1234, caption="carrier")
    assert ev.kind().as_u16() == 1063
    tags = {t.to_vec()[0]: t.to_vec()[1] for t in ev.tags()}
    assert tags["x"] == "ab" * 32
    assert tags["m"] == "image/png"
    assert tags["size"] == "1234"
    assert ev.verify()


def test_blossom_roundtrip(blossom_server):
    keys = ns.Keys.generate()
    data = bytes(np.random.RandomState(0).randint(0, 256, 5000))
    blob = blossom_upload(blossom_server, data, "image/png", keys)
    assert blob["sha256"] == hashlib.sha256(data).hexdigest()
    assert AUTH_HEADERS[0].startswith("Nostr ")
    got = blossom_get(blossom_server, blob["sha256"])
    assert got == data
    with pytest.raises(BlossomError):
        blossom_get(blossom_server, "00" * 32)


def test_send_fetch_ack_flow(relay_url):
    """Full cycle: alice sends capsule -> bob fetches -> bob acks -> alice sees ack."""
    alice, bob = ns.Keys.generate(), ns.Keys.generate()

    async def main():
        result = await send_capsule(
            alice, bob.public_key(), "caps-flow-1",
            {"v": 1, "status": "sent", "image_sha256": "aa" * 32},
            relays=[relay_url],
        )
        assert result.capsule_uuid == "caps-flow-1"
        assert result.capsule_event_id

        capsules = await process_incoming_capsules(bob, relays=[relay_url])
        assert any(c["uuid"] == "caps-flow-1" and c["status"] == "sent" for c in capsules)

        ack_id = await ack_capsule(bob, alice.public_key(), "caps-flow-1", "delivered",
                                   {"v": 1, "status": "delivered", "by": "bob"},
                                   relays=[relay_url])
        assert ack_id

        ca = NostrClient(alice, [relay_url])
        await ca.connect()
        try:
            events = await ca.fetch(capsule_filters(capsule_uuid="caps-flow-1"), timeout=8.0)
            statuses = sorted(c["status"] for c in (capsule_from_event(e) for e in events)
                              if c)
            assert "sent" in statuses and "delivered" in statuses
        finally:
            await ca.shutdown()

    run(main())


def test_capsule_rejects_foreign_kinds():
    keys = ns.Keys.generate()
    ev = ns.EventBuilder(ns.Kind(1), "hello").finalize(keys)
    assert capsule_from_event(ev) is None
    bad = ns.EventBuilder(ns.Kind(37300), "not json").tags(
        [ns.Tag.parse(["d", "x"]), ns.Tag.parse(["t", "stegstr-capsule"])]
    ).finalize(keys)
    assert capsule_from_event(bad) is None
