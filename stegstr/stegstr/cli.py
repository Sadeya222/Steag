"""Stegstr CLI — encode / decode / genkeys / log / validate / capacity.

Layer 1 (engine) + Layer 2 (NIP-44 crypto) + Layer 4 (SQLite capsule log)
are wired up here.  Layer 3 (Nostr networking) and the web/agent interfaces
are the next milestones.

Usage highlights:
  stegstr genkeys                                    # make a Nostr keypair
  stegstr encode img.png -m "secret" --to <npub> --key <nsec> -o carrier.png
  stegstr decode received.jpg --key <nsec>          # decrypt + print
  stegstr log                                        # capsule history
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import typer

from . import __version__
from .codec import capacity_bytes, decode_image, encode_image, load_image, max_capacity_bytes
from .crypto import (
    CryptoError,
    decrypt_payload,
    encrypt_message,
    generate_keys,
    is_envelope,
    keys_from_secret,
    npub_of,
    nsec_of,
    parse_public_key,
)
from .db import (
    add_contact,
    find_capsule,
    list_capsules,
    record_capsule,
    update_capsule_status,
    upsert_capsule_status,
)
from .engine import TierAConfig
from .net import (
    CAPSULE_TAG,
    DEFAULT_RELAYS,
    NetError,
    NostrClient,
    ack_capsule,
    blossom_get_url,
    blossom_upload,
    capsule_filters,
    capsule_from_event,
    parse_relay_url,
    run_sync,
    send_capsule,
    unwrap_dm,
)
from .validate import run_validation, write_report

app = typer.Typer(help=f"Stegstr v{__version__} — image steganography that survives social platforms.")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_key_opt(key: str | None) -> "object | None":
    if not key:
        return None
    return keys_from_secret(key)


@app.command()
def genkeys(
    out: Path = typer.Option(None, "--out", "-o", help="Write the nsec to a file (mode 0600)"),
):
    """Generate a Nostr keypair for use as Stegstr encryption identity."""
    keys = generate_keys()
    npub = npub_of(keys.public_key())
    nsec = nsec_of(keys.secret_key())
    typer.echo(f"npub: {npub}")
    typer.echo(f"nsec: {nsec}")
    typer.echo("(the nsec is your secret — anyone holding it can decrypt messages addressed to you)",
               err=True)
    if out:
        out.write_text(nsec + "\n")
        out.chmod(0o600)
        typer.echo(f"nsec saved to {out} (0600)", err=True)


@app.command()
def encode(
    image: Path = typer.Argument(..., help="Carrier image (PNG/JPEG)"),
    message: str = typer.Option(None, "--message", "-m", help="Message text"),
    text: Path = typer.Option(None, "--text", "-t", help="Read message from file"),
    to: str = typer.Option(None, "--to", help="Receiver npub/hex — encrypt with NIP-44 ECDH"),
    key: str = typer.Option(None, "--key", "-k", help="Sender nsec/hex (required with --to)"),
    out: Path = typer.Option(None, "--out", "-o", help="Output image path (default: <image>.steg.png)"),
    quality: int = typer.Option(70, "--quality", help="Embed quality (decode must use the same)"),
    multiscale: bool = typer.Option(False, "--multiscale", help="Also embed a half-scale copy (downscale robustness)"),
    jpeg: bool = typer.Option(False, "--jpeg", help="Write carrier as JPEG (q95) instead of PNG"),
    no_log: bool = typer.Option(False, "--no-log", help="Skip the capsule DB record"),
):
    """Embed a message into an image (optionally NIP-44 encrypted)."""
    if message is None and text is None:
        message = typer.prompt("Message")
    payload = message.encode("utf-8") if message is not None else text.read_bytes()

    payload_type = "plain"
    sender_npub = receiver_npub = None
    if to:
        if not key:
            typer.echo("error: --to requires --key (your sender key)", err=True)
            raise typer.Exit(2)
        receiver_pk = parse_public_key(to)
        sender = _parse_key_opt(key)
        payload = encrypt_message(payload, sender, receiver_pk)
        payload_type = "nip44"
        sender_npub = npub_of(sender.public_key())
        receiver_npub = receiver_pk.to_bech32()
        typer.echo(f"encrypted for {receiver_npub}", err=True)

    if len(payload) > 16 * 1024:
        typer.echo("warning: payload > 16 KiB; check the capacity table first", err=True)

    cfg = TierAConfig(embed_quality=quality)
    img = load_image(str(image))
    cap, cap_r = max_capacity_bytes(img.width, img.height, cfg)
    if len(payload) > cap:
        typer.echo(f"error: payload {len(payload)}B exceeds capacity {cap}B "
                   f"(at redundancy r={cap_r}) for {img.width}x{img.height}", err=True)
        raise typer.Exit(1)
    carrier = encode_image(img, payload, cfg, multiscale=multiscale)
    out = out or image.with_name(image.stem + ".steg.png")
    if jpeg:
        out = out.with_suffix(".jpg")
        carrier.convert("RGB").save(out, "JPEG", quality=95, subsampling=0)
    else:
        carrier.convert("RGB").save(out, "PNG")

    capsule_id = None
    if not no_log:
        capsule_id = record_capsule(
            direction="sent",
            image_sha256=_sha256_file(out),
            sender_npub=sender_npub,
            receiver_npub=receiver_npub,
            payload_type=payload_type,
            payload_bytes=len(payload),
            status="encoded",
            meta={"embed_quality": quality, "multiscale": multiscale,
                  "carrier": str(out)},
        )
        if receiver_npub:
            add_contact(receiver_npub, label="")
    typer.echo(f"encoded {len(payload)}B ({payload_type}) -> {out} "
               f"({carrier.width}x{carrier.height})")
    if capsule_id:
        typer.echo(f"capsule #{capsule_id} logged (status: encoded)", err=True)


@app.command()
def decode(
    image: Path = typer.Argument(..., help="Received image (export from WhatsApp/Telegram/Instagram)"),
    out: Path = typer.Option(None, "--out", "-o", help="Write recovered message bytes to file"),
    key: str = typer.Option(None, "--key", "-k", help="Your nsec/hex — decrypt NIP-44 payloads"),
    quality: int = typer.Option(70, "--quality", help="Embed quality used at encode time"),
    no_log: bool = typer.Option(False, "--no-log", help="Skip the capsule DB record"),
):
    """Recover a message from a carrier image (decrypts NIP-44 payloads)."""
    try:
        payload, meta = decode_image(load_image(str(image)), TierAConfig(embed_quality=quality))
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"decode failed: {exc}", err=True)
        raise typer.Exit(1)

    my_npub = None
    if is_envelope(payload):
        if not key:
            typer.echo("<encrypted payload — pass --key <your nsec> to decrypt>", err=True)
            typer.echo("[meta] r=%s scale_key=%s corrected=%sB margin=%.3f" % (
                meta.get("r"), meta.get("scale_key"), meta.get("corrected_bytes", 0),
                meta.get("margin", 0.0)), err=True)
            if not no_log:
                record_capsule(direction="received", image_sha256=_sha256_file(image),
                               payload_type="nip44", payload_bytes=len(payload),
                               status="delivered", meta={"carrier": str(image)})
            raise typer.Exit(3)
        try:
            dec = decrypt_payload(payload, keys_from_secret(key))
        except CryptoError as exc:
            typer.echo(f"decrypt failed: {exc}", err=True)
            if not no_log:
                record_capsule(direction="received", image_sha256=_sha256_file(image),
                               payload_type="nip44", payload_bytes=len(payload),
                               status="delivered", meta={"carrier": str(image), "error": str(exc)})
            raise typer.Exit(1)
        my_npub = npub_of(keys_from_secret(key).public_key())
        msg = dec.plaintext
        try:
            typer.echo(msg.decode("utf-8"))
        except UnicodeDecodeError:
            typer.echo(f"<binary payload, {len(msg)} bytes; use --out to save>")
        typer.echo(f"[from {dec.sender_npub}]", err=True)
        if out:
            out.write_bytes(msg)
    else:
        try:
            typer.echo(payload.decode("utf-8"))
        except UnicodeDecodeError:
            typer.echo(f"<binary payload, {len(payload)} bytes; use --out to save>")
        if out:
            out.write_bytes(payload)
        if not no_log:
            my_npub = npub_of(keys_from_secret(key).public_key()) if key else None

    typer.echo("[meta] r=%s scale_key=%s corrected=%sB margin=%.3f" % (
        meta.get("r"), meta.get("scale_key"), meta.get("corrected_bytes", 0),
        meta.get("margin", 0.0)), err=True)
    if not no_log:
        capsule_id = record_capsule(
            direction="received",
            image_sha256=_sha256_file(image),
            sender_npub=None,
            receiver_npub=my_npub,
            payload_type="nip44" if is_envelope(payload) else "plain",
            payload_bytes=len(payload),
            status="decoded",
            meta={"carrier": str(image), "corrected_bytes": meta.get("corrected_bytes", 0),
                  "margin": round(meta.get("margin", 0.0), 3)},
        )
        typer.echo(f"capsule #{capsule_id} logged (status: decoded)", err=True)


@app.command()
def log(
    status: str = typer.Option(None, "--status", "-s", help="Filter: encoded|sent|delivered|decoded"),
    limit: int = typer.Option(50, "--limit", "-n"),
):
    """Show the local capsule log (Layer 4 SQLite)."""
    rows = list_capsules(status=status, limit=limit)
    if not rows:
        typer.echo("no capsules logged yet")
        return
    typer.echo(f"{'id':>3}  {'dir':9s} {'status':9s} {'type':6s} {'bytes':>6s}  from/to")
    for r in rows:
        fr = r["sender_npub"] or "-"
        to_ = r["receiver_npub"] or "-"
        fr = (fr[:16] + "…") if fr and len(fr) > 17 else fr
        to_ = (to_[:16] + "…") if to_ and len(to_) > 17 else to_
        typer.echo(f"{r['id']:>3}  {r['direction']:9s} {r['status']:9s} {r['payload_type']:6s} "
                   f"{r['payload_bytes'] or 0:>6d}  {fr} -> {to_}")


@app.command()
def send(
    carrier: Path = typer.Argument(..., help="Stegstr carrier image (from `encode`)"),
    to: str = typer.Option(..., "--to", help="Receiver npub/hex"),
    key: str = typer.Option(..., "--key", "-k", help="Your nsec/hex"),
    relay: list[str] = typer.Option(None, "--relay", "-r", help="Relay URL (repeatable; default: public set)"),
    blossom: str = typer.Option(None, "--blossom", help="Blossom server URL to host the carrier on"),
    note: str = typer.Option(None, "--note", help="Human-readable note in the capsule"),
    no_dm: bool = typer.Option(False, "--no-dm", help="Skip the NIP-17 metadata DM"),
    quality: int = typer.Option(70, "--quality", help="Embed quality used when encoding"),
    multiscale: bool = typer.Option(False, "--multiscale", help="Mark capsule as multiscale-embedded"),
    no_log: bool = typer.Option(False, "--no-log", help="Skip the capsule DB record"),
):
    """Announce a carrier over Nostr: capsule event (+ Blossom hosting + metadata DM).

    The image itself travels through WhatsApp/Telegram/Instagram as usual; the
    capsule (kind 37300) lets the receiver's app know a capsule exists and sync
    its state: sent -> delivered -> decoded.
    """
    sender = keys_from_secret(key)
    receiver_pk = parse_public_key(to)
    relays = relay or DEFAULT_RELAYS
    my_npub = npub_of(sender.public_key())

    # inspect the carrier (proves it is a valid Stegstr artifact)
    payload_type, payload_len = "unknown", 0
    try:
        payload, meta = decode_image(load_image(str(carrier)), TierAConfig(embed_quality=quality))
        payload_type = "nip44" if is_envelope(payload) else "plain"
        payload_len = len(payload)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"warning: carrier does not decode as Stegstr ({exc}); sending anyway", err=True)

    capsule_uuid = uuid.uuid4().hex[:12]
    image_sha256 = _sha256_file(carrier)
    size = carrier.stat().st_size

    # optionally host the carrier on Blossom (hash-addressed, bit-exact)
    blob = None
    if blossom:
        data = carrier.read_bytes()
        mime = "image/jpeg" if carrier.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        try:
            blob = blossom_upload(blossom, data, mime, sender)
            typer.echo(f"uploaded to Blossom: {blob.get('url')}", err=True)
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"warning: Blossom upload failed ({exc}); continuing without blob", err=True)

    content = {
        "v": 1,
        "status": "sent",
        "image_sha256": image_sha256,
        "size": size,
        "payload_type": payload_type,
        "payload_len": payload_len,
        "embed_quality": quality,
        "multiscale": multiscale,
        "blob": {"url": blob["url"], "sha256": blob["sha256"]} if blob else None,
        "note": note,
        "relays": relays,
    }
    dm_message = None
    if not no_dm:
        dm_message = json.dumps({
            "capsule": capsule_uuid,
            "image_sha256": image_sha256,
            "blob": {"url": blob["url"], "sha256": blob["sha256"]} if blob else None,
            "embed_quality": quality,
            "payload_type": payload_type,
        })

    try:
        result = run_sync(send_capsule(sender, receiver_pk, capsule_uuid, content,
                                       relays, blob=blob, dm_message=dm_message))
    except NetError as exc:
        typer.echo(f"send failed: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"capsule {result.capsule_uuid} published (status: sent)")
    typer.echo(f"  capsule event: {result.capsule_event_id}")
    if result.file_event_id:
        typer.echo(f"  nip94 file event: {result.file_event_id}")
    if result.dm_event_id:
        typer.echo(f"  nip17 dm event: {result.dm_event_id}")
    if not no_log:
        cid = record_capsule(
            direction="sent",
            image_sha256=image_sha256,
            sender_npub=my_npub,
            receiver_npub=receiver_pk.to_bech32(),
            payload_type=payload_type,
            payload_bytes=payload_len or None,
            status="sent",
            capsule_uuid=capsule_uuid,
            event_id=result.capsule_event_id,
            meta={"blob": blob, "carrier": str(carrier), "note": note},
        )
        add_contact(receiver_pk.to_bech32(), label="")
        typer.echo(f"capsule #{cid} logged (status: sent)", err=True)


@app.command()
def listen(
    key: str = typer.Option(..., "--key", "-k", help="Your nsec/hex"),
    relay: list[str] = typer.Option(None, "--relay", "-r", help="Relay URL (repeatable; default: public set)"),
    once: bool = typer.Option(False, "--once", help="Fetch existing capsules once, then exit"),
    auto_save: Path = typer.Option(None, "--auto-save", help="Dir: download Blossom carriers, decode+decrypt, save messages"),
    timeout: float = typer.Option(600.0, "--timeout", help="Live listen duration (seconds)"),
    quality: int = typer.Option(70, "--quality", help="Embed quality used at encode time"),
):
    """Listen for capsules addressed to you; sync state and (optionally) auto-decode."""
    me = keys_from_secret(key)
    relays = relay or DEFAULT_RELAYS
    my_npub = npub_of(me.public_key())
    typer.echo(f"listening as {my_npub}", err=True)
    if auto_save:
        auto_save.mkdir(parents=True, exist_ok=True)

    seen: set[tuple[str, str]] = set()
    processed: set[str] = set()

    async def handle(ev) -> None:
        # gift-wrapped DMs (metadata out-of-band)
        if ev.kind().as_u16() == 1059:
            dec = unwrap_dm(me, ev)
            if dec:
                sender_hex, text = dec
                typer.echo(f"[dm] from {sender_hex[:16]}…: {text[:120]}")
                try:
                    info = json.loads(text)
                    if info.get("capsule"):
                        upsert_capsule_status(
                            info["capsule"], "sent",
                            sender_npub=parse_public_key(sender_hex).to_bech32(),
                            meta={"dm_info": info},
                        )
                except (ValueError, TypeError):
                    pass
            return
        parsed = capsule_from_event(ev)
        if parsed is None:
            return
        key_ = (parsed["uuid"], parsed["status"], parsed["event_id"])
        if key_ in seen:
            return
        seen.add(key_)
        ok = parsed["verified"]
        typer.echo(f"[capsule {parsed['uuid']}] status={parsed['status']} "
                   f"from {parsed['author'][:16]}… verified={ok}")
        upsert_capsule_status(
            parsed["uuid"], parsed["status"],
            sender_npub=parsed["author"],
            receiver_npub=my_npub,
            event_id=parsed["event_id"],
            meta={"content": parsed["content"]},
        )
        if parsed["status"] == "sent" and parsed["uuid"] not in processed:
            processed.add(parsed["uuid"])
            await _respond_to_capsule(me, parsed, auto_save, quality, relays)

    async def _respond_to_capsule(keys, parsed, auto_save, quality, relays) -> None:
        """Ack delivery, then (if the carrier is fetchable) decode+decrypt and ack decoded."""
        content = parsed["content"]
        sender_pk = parse_public_key(parsed["author"])
        blob = content.get("blob") or {}
        image_path = None
        if auto_save and blob.get("url"):
            try:
                data = blossom_get_url(blob["url"], blob.get("sha256", ""))
            except Exception as exc:  # noqa: BLE001
                data = None
                typer.echo(f"  blob download failed: {exc}", err=True)
            if data:
                suffix = ".jpg" if blob.get("url", "").split("?")[0].endswith((".jpg", ".jpeg")) else ".png"
                image_path = auto_save / f"{blob['sha256'][:16]}{suffix}"
                image_path.write_bytes(data)
                typer.echo(f"  downloaded carrier -> {image_path}", err=True)
        # ack delivery (monotonic timestamps: the capsule kind is replaceable)
        now = int(time.time())
        ack_content = {"v": 1, "status": "delivered", "by": npub_of(keys.public_key())}
        try:
            eid = await ack_capsule(keys, sender_pk, parsed["uuid"], "delivered",
                                    ack_content, relays, created_at=now)
            typer.echo(f"  ack delivered -> {eid}", err=True)
            upsert_capsule_status(parsed["uuid"], "delivered",
                                  receiver_npub=npub_of(keys.public_key()),
                                  event_id=eid, meta={"content": ack_content})
        except NetError as exc:
            typer.echo(f"  ack failed: {exc}", err=True)
        # attempt decode+decrypt
        if image_path:
            try:
                payload, meta = decode_image(load_image(str(image_path)),
                                             TierAConfig(embed_quality=quality))
                ok = True
                corrected = meta.get("corrected_bytes", 0)
                out_bytes = payload
                if is_envelope(payload):
                    try:
                        dec = decrypt_payload(payload, keys)
                        out_bytes = dec.plaintext
                        typer.echo(f"  decrypted from {dec.sender_npub}", err=True)
                    except CryptoError as exc:
                        typer.echo(f"  decrypt failed: {exc}", err=True)
                        ok = False
                if ok:
                    msg_path = auto_save / f"{parsed['uuid']}.msg"
                    msg_path.write_bytes(out_bytes)
                    typer.echo(f"  message saved -> {msg_path}", err=True)
                dec_ack = {"v": 1, "status": "decoded", "by": npub_of(keys.public_key()),
                           "decoded": {"ok": ok, "corrected": corrected}}
                try:
                    eid = await ack_capsule(keys, sender_pk, parsed["uuid"], "decoded",
                                            dec_ack, relays, created_at=now + 1)
                    typer.echo(f"  ack decoded -> {eid}", err=True)
                    upsert_capsule_status(parsed["uuid"], "decoded",
                                          receiver_npub=npub_of(keys.public_key()),
                                          event_id=eid, meta={"content": dec_ack})
                except NetError as exc:
                    typer.echo(f"  decoded ack failed: {exc}", err=True)
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"  decode failed: {exc}", err=True)

    def spawn(ev) -> None:
        """Sync callback for the live stream: run the async handler as a task."""
        try:
            asyncio_loop = asyncio.get_event_loop()
        except RuntimeError:
            asyncio_loop = None
        if asyncio_loop and asyncio_loop.is_running():
            asyncio_loop.create_task(handle(ev))
        else:
            run_sync(handle(ev))

    async def main():
        client = NostrClient(me, relays)
        await client.connect()
        try:
            events = await client.fetch(capsule_filters(me.public_key()), timeout=10.0)
            for ev in events:
                await handle(ev)
            if once:
                return
            typer.echo(f"watching for new capsules (timeout {timeout:.0f}s; Ctrl-C to stop)", err=True)
            try:
                await client.stream(capsule_filters(me.public_key()), spawn, timeout=timeout)
            except KeyboardInterrupt:
                pass
        finally:
            await client.shutdown()

    try:
        run_sync(main())
    except NetError as exc:
        typer.echo(f"listen failed: {exc}", err=True)
        raise typer.Exit(1)


@app.command()
def sync(
    capsule_uuid: str = typer.Argument(..., help="Capsule uuid (from `send`)"),
    key: str = typer.Option(None, "--key", "-k", help="Your nsec/hex (required to see capsule events)"),
    relay: list[str] = typer.Option(None, "--relay", "-r", help="Relay URL (repeatable; default: public set)"),
):
    """Fetch the latest state of a capsule from the relays."""
    me = keys_from_secret(key)
    relays = relay or DEFAULT_RELAYS

    async def main():
        client = NostrClient(me, relays)
        await client.connect()
        try:
            events = await client.fetch(capsule_filters(capsule_uuid=capsule_uuid), timeout=10.0)
        finally:
            await client.shutdown()
        return events

    try:
        events = run_sync(main())
    except NetError as exc:
        typer.echo(f"sync failed: {exc}", err=True)
        raise typer.Exit(1)
    states = sorted(
        (capsule_from_event(e) for e in events if capsule_from_event(e)),
        key=lambda c: c["created_at"],
    )
    if not states:
        typer.echo(f"no events found for capsule {capsule_uuid}")
        return
    typer.echo(f"capsule {capsule_uuid}:")
    for c in states:
        typer.echo(f"  {c['status']:9s} by {c['author'][:16]}… @ {c['created_at']} "
                   f"verified={c['verified']} ({c['event_id'][:16]}…)")
        if c["status"] == "decoded" and c["content"].get("decoded"):
            d = c["content"]["decoded"]
            typer.echo(f"    decoded ok={d.get('ok')} corrected={d.get('corrected')}")
        upsert_capsule_status(c["uuid"], c["status"],
                              sender_npub=c["author"],
                              event_id=c["event_id"],
                              meta={"content": c["content"]})


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address"),
    port: int = typer.Option(8000, "--port", help="Port"),
    open_browser: bool = typer.Option(False, "--open", help="Open the web UI in a browser"),
):
    """Run the local web UI + agent API (FastAPI) — http://<host>:<port>/."""
    import uvicorn

    from .api import app

    if open_browser:
        import threading
        import time
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    typer.echo(f"stegstr web UI + API on http://{host}:{port}/  (docs at /docs)")
    uvicorn.run(app, host=host, port=port, log_level="info")


@app.command()
def mcp():
    """Run the MCP (Model Context Protocol) stdio server for AI agents."""
    from .mcp_server import main as mcp_main

    import asyncio
    asyncio.run(mcp_main())


@app.command()
def capacity(
    image: Path = typer.Argument(..., help="Image to measure"),
    quality: int = typer.Option(70, "--quality"),
):
    """Show how much payload fits in an image at each redundancy level."""
    img = load_image(str(image))
    cfg = TierAConfig(embed_quality=quality)
    typer.echo(f"{img.width}x{img.height}:")
    for r in (2, 3, 4, 6, 8, 12):
        typer.echo(f"  r={r:<2d} {capacity_bytes(img.width, img.height, cfg, repetitions=r):>6d} B")
    typer.echo(f"  r=1  {capacity_bytes(img.width, img.height, cfg, repetitions=1):>6d} B (no FEC headroom)")


@app.command()
def validate(
    quiet: bool = typer.Option(False, "--quiet", "-q"),
):
    """Run the full validation harness and write reports/validation_report.*."""
    report = run_validation(quiet=quiet)
    jp, mp = write_report(report)
    typer.echo(f"\nreport written: {mp}")
    typer.echo(f"json: {jp}")


def main() -> None:
    app()


if __name__ == "__main__":
    app()
