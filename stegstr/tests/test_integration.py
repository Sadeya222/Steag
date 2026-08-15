"""Full-stack integration: encode -> send (capsule+Blossom+DM) -> listen
(fetch, ack delivered, auto-download, decode+decrypt, ack decoded) -> sync.

Everything runs against the hermetic local relay + stub Blossom server.
The CLI's run_sync is monkeypatched to the shared test loop so the whole
stack lives on one event loop (required by the nostr FFI runtime).
"""

import json

import numpy as np
import pytest
from typer.testing import CliRunner

import stegstr.cli as cli
from helpers import BLOBS, run
from stegstr.codec import encode_image
from stegstr.crypto import encrypt_message, generate_keys, nsec_of, npub_of
from stegstr.db import find_capsule, list_capsules

runner = CliRunner()


@pytest.fixture()
def fake_loop(monkeypatch):
    """Point the CLI's sync runner at the shared test loop."""
    monkeypatch.setattr(cli, "run_sync", run)


def _synthetic_img(w=512, h=512, seed=42):
    rng = np.random.RandomState(seed)
    base = np.clip(rng.normal(120, 45, (h, w, 3)), 0, 255).astype(np.uint8)
    y, x = np.mgrid[0:h, 0:w]
    base = np.clip(base + (30 * np.sin(x / 50) + 20 * np.cos(y / 40))[..., None], 0, 255)
    from PIL import Image
    return Image.fromarray(base.astype(np.uint8))


def test_full_stack(tmp_path, relay_url, blossom_server, fake_loop, monkeypatch):
    # each identity has its own DB, like real deployments
    alice_db = tmp_path / "alice.db"
    bob_db = tmp_path / "bob.db"
    BLOBS.clear()

    alice = generate_keys()
    bob = generate_keys()
    secret = "the pagoda at 21:00 — bring the red lantern \U0001f382"

    # 1. encode an encrypted carrier
    carrier = tmp_path / "carrier.png"
    envelope = encrypt_message(secret.encode(), alice, bob.public_key())
    encode_image(_synthetic_img(), envelope).save(carrier, "PNG")

    # 2. send: capsule + Blossom + NIP-17 DM (alice's DB)
    monkeypatch.setenv("STEGSTR_DB", str(alice_db))
    r = runner.invoke(cli.app, [
        "send", str(carrier),
        "--to", npub_of(bob.public_key()),
        "--key", nsec_of(alice.secret_key()),
        "--relay", relay_url,
        "--blossom", blossom_server,
        "--note", "contest demo",
    ])
    assert r.exit_code == 0, r.output
    assert "capsule " in r.output and "published" in r.output
    assert len(BLOBS) == 1

    rows = list_capsules(path=alice_db)
    sent = [x for x in rows if x["direction"] == "sent"]
    assert len(sent) == 1 and sent[0]["status"] == "sent"
    assert sent[0]["capsule_uuid"] and sent[0]["event_id"]
    capsule_uuid = sent[0]["capsule_uuid"]

    # 3. receiver listens once (bob's DB): fetch, ack delivered, auto-download,
    #    decode+decrypt, ack decoded
    monkeypatch.setenv("STEGSTR_DB", str(bob_db))
    auto = tmp_path / "inbox"
    r = runner.invoke(cli.app, [
        "listen",
        "--key", nsec_of(bob.secret_key()),
        "--relay", relay_url,
        "--once",
        "--auto-save", str(auto),
    ])
    assert r.exit_code == 0, r.output
    assert f"capsule {capsule_uuid}" in r.output
    assert "ack delivered" in r.output
    assert "ack decoded" in r.output

    msgs = sorted(auto.glob("*.msg"))
    assert len(msgs) == 1, [p.name for p in auto.iterdir()]
    assert msgs[0].read_bytes() == secret.encode()
    assert len(sorted(auto.glob("*.png"))) == 1

    rec = find_capsule(capsule_uuid, path=bob_db)
    assert rec is not None
    assert rec["direction"] == "received"
    assert rec["status"] == "decoded"

    # 4. sync from both sides sees the state machine.  Kind 37300 is
    #    parameterized-replaceable, so bob's "decoded" supersedes his
    #    "delivered" on the relay (monotonic timestamps) — the final state is
    #    sent -> decoded, with the decode outcome attached.
    monkeypatch.setenv("STEGSTR_DB", str(alice_db))
    r = runner.invoke(cli.app, ["sync", capsule_uuid, "--key", nsec_of(alice.secret_key()),
                                "--relay", relay_url])
    assert r.exit_code == 0, r.output
    assert "sent" in r.output and "decoded" in r.output
    assert "decoded ok=True" in r.output
    monkeypatch.setenv("STEGSTR_DB", str(bob_db))
    r = runner.invoke(cli.app, ["sync", capsule_uuid, "--key", nsec_of(bob.secret_key()),
                                "--relay", relay_url])
    assert r.exit_code == 0, r.output

    # 5. sender's row now reflects the acks; receiver's row carries decode outcome
    sent_row = find_capsule(capsule_uuid, path=alice_db)
    assert sent_row["status"] == "decoded"
    rec2 = find_capsule(capsule_uuid, path=bob_db)
    meta = json.loads(rec2["meta_json"] or "{}")
    assert meta.get("content", {}).get("decoded", {}).get("ok") is True
