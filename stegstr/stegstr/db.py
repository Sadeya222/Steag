"""Layer 4 — SQLite storage: capsules, decode outcomes, contacts.

Nothing heavier than SQLite: one file, zero setup.  This is the local
mirror of the "capsule" state machine (sent -> delivered -> decoded) that
Layer 3 will sync over Nostr events; the schema is already shaped for that.

DB location: $STEGSTR_DB or ~/.stegstr/stegstr.db
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

_LOCK = threading.Lock()

_CAPSULES_KEY = "stegstr:capsules"
_CONTACTS_KEY = "stegstr:contacts"

# --------------------------------------------------------------------------
# Upstash Redis REST backend (serverless deployments — Vercel)
# The capsule log lives in one JSON document per key; SQLite is the default.
# --------------------------------------------------------------------------
def _kv_config() -> tuple[str, str] | None:
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return url.rstrip("/"), token
    return None


def _kv_get(key: str) -> list | None:
    cfg = _kv_config()
    if not cfg:
        return None
    import urllib.error
    import urllib.request
    url, token = cfg
    req = urllib.request.Request(
        f"{url}/get/{key}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"kv get failed HTTP {exc.code}") from exc
    result = body.get("result")
    if result is None or result == "nil":
        return None
    try:
        return json.loads(result)
    except (ValueError, TypeError):
        return None


def _kv_set(key: str, value: list) -> None:
    cfg = _kv_config()
    if not cfg:
        return
    import urllib.error
    import urllib.parse
    import urllib.request
    url, token = cfg
    encoded = urllib.parse.quote(json.dumps(value), safe="")
    req = urllib.request.Request(
        f"{url}/set/{key}/{encoded}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"kv set failed HTTP {exc.code}") from exc


def _kv_append(key: str, row: dict) -> None:
    rows = _kv_get(key) or []
    rows.append(row)
    _kv_set(key, rows)


def _kv_update(key: str, predicate, update) -> None:
    rows = _kv_get(key) or []
    for r in rows:
        if predicate(r):
            update(r)
    _kv_set(key, rows)


def _kv_backend_active() -> bool:
    return _kv_config() is not None


def default_db_path() -> Path:
    env = os.environ.get("STEGSTR_DB")
    if env:
        return Path(env)
    return Path.home() / ".stegstr" / "stegstr.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS capsules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    direction      TEXT NOT NULL CHECK (direction IN ('sent','received')),
    image_sha256   TEXT,                  -- content hash of the carrier
    sender_npub    TEXT,
    receiver_npub  TEXT,
    payload_type   TEXT NOT NULL DEFAULT 'plain',   -- plain | nip44
    payload_bytes  INTEGER,
    status         TEXT NOT NULL DEFAULT 'encoded', -- encoded -> sent -> delivered -> decoded
    meta_json      TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_capsules_status ON capsules(status);
CREATE INDEX IF NOT EXISTS idx_capsules_direction ON capsules(direction);

CREATE TABLE IF NOT EXISTS contacts (
    npub        TEXT PRIMARY KEY,
    label       TEXT,
    first_seen  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

_MIGRATIONS = [
    "ALTER TABLE capsules ADD COLUMN capsule_uuid TEXT",
    "ALTER TABLE capsules ADD COLUMN event_id TEXT",
]


def _migrate(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _kv_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_capsule(
    direction: str,
    image_sha256: str | None = None,
    sender_npub: str | None = None,
    receiver_npub: str | None = None,
    payload_type: str = "plain",
    payload_bytes: int | None = None,
    status: str = "encoded",
    meta: dict | None = None,
    capsule_uuid: str | None = None,
    event_id: str | None = None,
    path: Path | None = None,
) -> int:
    if _kv_backend_active():
        rows = _kv_get(_CAPSULES_KEY) or []
        cid = (max((r["id"] for r in rows), default=0) or 0) + 1
        _kv_append(_CAPSULES_KEY, {
            "id": cid,
            "direction": direction,
            "image_sha256": image_sha256,
            "sender_npub": sender_npub,
            "receiver_npub": receiver_npub,
            "payload_type": payload_type,
            "payload_bytes": payload_bytes,
            "status": status,
            "meta_json": json.dumps(meta) if meta else None,
            "capsule_uuid": capsule_uuid,
            "event_id": event_id,
            "created_at": _kv_now(),
            "updated_at": _kv_now(),
        })
        return cid
    with _LOCK:
        conn = connect(path)
        try:
            cur = conn.execute(
                """INSERT INTO capsules
                   (direction, image_sha256, sender_npub, receiver_npub,
                    payload_type, payload_bytes, status, meta_json,
                    capsule_uuid, event_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (direction, image_sha256, sender_npub, receiver_npub,
                 payload_type, payload_bytes, status,
                 json.dumps(meta) if meta else None,
                 capsule_uuid, event_id),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def update_capsule_status(capsule_id: int, status: str, meta: dict | None = None,
                          path: Path | None = None) -> None:
    if _kv_backend_active():
        def pred(r):
            return r.get("id") == capsule_id

        def upd(r):
            merged = dict(json.loads(r.get("meta_json") or "{}"))
            if meta:
                merged.update(meta)
            r["status"] = status
            r["meta_json"] = json.dumps(merged) if merged else None
            r["updated_at"] = _kv_now()

        _kv_update(_CAPSULES_KEY, pred, upd)
        return
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute("SELECT meta_json FROM capsules WHERE id = ?",
                               (capsule_id,)).fetchone()
            merged = dict(json.loads(row["meta_json"])) if row and row["meta_json"] else {}
            if meta:
                merged.update(meta)
            conn.execute(
                """UPDATE capsules SET status = ?, meta_json = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                   WHERE id = ?""",
                (status, json.dumps(merged) if merged else None, capsule_id),
            )
            conn.commit()
        finally:
            conn.close()


def list_capsules(status: str | None = None, limit: int = 50,
                  path: Path | None = None) -> list[dict]:
    if _kv_backend_active():
        rows = _kv_get(_CAPSULES_KEY) or []
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda r: r.get("id", 0), reverse=True)
        return rows[:limit]
    conn = connect(path)
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM capsules WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM capsules ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_capsule(capsule_uuid: str, path: Path | None = None) -> dict | None:
    """Latest local row for a capsule uuid (either direction)."""
    if _kv_backend_active():
        rows = [r for r in (_kv_get(_CAPSULES_KEY) or [])
                if r.get("capsule_uuid") == capsule_uuid]
        if not rows:
            return None
        return max(rows, key=lambda r: r.get("id", 0))
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM capsules WHERE capsule_uuid = ? ORDER BY id DESC LIMIT 1",
            (capsule_uuid,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_capsule_status(capsule_uuid: str, status: str, sender_npub: str | None = None,
                          receiver_npub: str | None = None, event_id: str | None = None,
                          meta: dict | None = None, path: Path | None = None) -> int:
    """Create or update the local row tracking a remote capsule's status."""
    if _kv_backend_active():
        rows = _kv_get(_CAPSULES_KEY) or []
        existing = find_capsule(capsule_uuid, path)
        if existing is None:
            cid = (max((r["id"] for r in rows), default=0) or 0) + 1
            rows.append({
                "id": cid, "direction": "received",
                "sender_npub": sender_npub, "receiver_npub": receiver_npub,
                "status": status, "meta_json": json.dumps(meta) if meta else None,
                "capsule_uuid": capsule_uuid, "event_id": event_id,
                "created_at": _kv_now(), "updated_at": _kv_now(),
            })
        else:
            cid = existing["id"]
            for r in rows:
                if r.get("id") == cid:
                    merged = dict(json.loads(r.get("meta_json") or "{}"))
                    if meta:
                        merged.update(meta)
                    r.update({"status": status,
                              "sender_npub": sender_npub or r.get("sender_npub"),
                              "receiver_npub": receiver_npub or r.get("receiver_npub"),
                              "event_id": event_id or r.get("event_id"),
                              "meta_json": json.dumps(merged) if merged else None,
                              "updated_at": _kv_now()})
        _kv_set(_CAPSULES_KEY, rows)
        return cid
    existing = find_capsule(capsule_uuid, path)
    with _LOCK:
        conn = connect(path)
        try:
            if existing is None:
                cur = conn.execute(
                    """INSERT INTO capsules
                       (direction, sender_npub, receiver_npub, status, meta_json,
                        capsule_uuid, event_id)
                       VALUES ('received', ?, ?, ?, ?, ?, ?)""",
                    (sender_npub, receiver_npub, status,
                     json.dumps(meta) if meta else None, capsule_uuid, event_id),
                )
                conn.commit()
                return int(cur.lastrowid)
            merged = dict(json.loads(existing["meta_json"])) if existing.get("meta_json") else {}
            if meta:
                merged.update(meta)
            conn.execute(
                """UPDATE capsules SET status = ?, sender_npub = COALESCE(?, sender_npub),
                       receiver_npub = COALESCE(?, receiver_npub),
                       event_id = COALESCE(?, event_id),
                       meta_json = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                   WHERE id = ?""",
                (status, sender_npub, receiver_npub, event_id,
                 json.dumps(merged) if merged else None, existing["id"]),
            )
            conn.commit()
            return int(existing["id"])
        finally:
            conn.close()


def add_contact(npub: str, label: str | None = None, path: Path | None = None) -> None:
    if _kv_backend_active():
        rows = _kv_get(_CONTACTS_KEY) or []
        for r in rows:
            if r.get("npub") == npub:
                if label:
                    r["label"] = label
                break
        else:
            rows.append({"npub": npub, "label": label,
                         "first_seen": _kv_now()})
        _kv_set(_CONTACTS_KEY, rows)
        return
    with _LOCK:
        conn = connect(path)
        try:
            conn.execute(
                """INSERT INTO contacts (npub, label) VALUES (?, ?)
                   ON CONFLICT(npub) DO UPDATE SET label = COALESCE(?, label)""",
                (npub, label, label),
            )
            conn.commit()
        finally:
            conn.close()


def list_contacts(path: Path | None = None) -> list[dict]:
    if _kv_backend_active():
        return (_kv_get(_CONTACTS_KEY) or [])[::-1]
    conn = connect(path)
    try:
        rows = conn.execute("SELECT * FROM contacts ORDER BY first_seen DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
