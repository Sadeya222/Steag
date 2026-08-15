"""Upstash Redis REST backend tests (stub server speaking the Upstash REST shape).

Upstash's REST API is GET-based:  GET /get/<key>  and  GET /set/<key>/<value>
(value URL-encoded).  The stub below mimics exactly that.
"""

import json
import threading
import urllib.parse

import pytest

import stegstr.db as db

STORE: dict[str, str] = {}
_lock = threading.Lock()


@pytest.fixture(scope="module")
def kv_server():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path
            if path.startswith("/get/"):
                key = path[len("/get/"):]
                with _lock:
                    val = STORE.get(key)
                body = json.dumps(
                    {"result": val if val is not None else "nil"}
                ).encode()
            elif path.startswith("/set/"):
                rest = path[len("/set/"):]
                key, _, encoded = rest.partition("/")
                value = urllib.parse.unquote(encoded)
                with _lock:
                    STORE[key] = value
                body = json.dumps({"result": "OK"}).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture()
def kv_env(kv_server, monkeypatch):
    STORE.clear()
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", kv_server)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "test-token")
    assert db._kv_backend_active()
    return kv_server


def test_record_and_list(kv_env):
    cid = db.record_capsule("sent", image_sha256="aa", sender_npub="npub1a",
                            receiver_npub="npub1b", payload_type="nip44",
                            payload_bytes=42, status="sent",
                            capsule_uuid="caps-1", event_id="ev-1")
    assert cid >= 1
    rows = db.list_capsules()
    assert len(rows) == 1
    assert rows[0]["capsule_uuid"] == "caps-1"
    assert rows[0]["status"] == "sent"
    assert db.list_capsules(status="decoded") == []
    assert len(db.list_capsules(status="sent")) == 1


def test_update_status_and_find(kv_env):
    cid = db.record_capsule("sent", capsule_uuid="caps-2", status="sent")
    db.update_capsule_status(cid, "decoded", meta={"margin": 0.9})
    row = db.find_capsule("caps-2")
    assert row is not None and row["status"] == "decoded"
    assert json.loads(row["meta_json"])["margin"] == 0.9


def test_upsert_creates_and_updates(kv_env):
    cid = db.upsert_capsule_status("caps-3", "sent", sender_npub="npub1s")
    row = db.find_capsule("caps-3")
    assert row["direction"] == "received" and row["status"] == "sent"
    cid2 = db.upsert_capsule_status("caps-3", "delivered", event_id="ev-9")
    assert cid2 == cid
    row = db.find_capsule("caps-3")
    assert row["status"] == "delivered" and row["event_id"] == "ev-9"
    assert row["sender_npub"] == "npub1s"


def test_contacts(kv_env):
    db.add_contact("npub1x", label="bob")
    db.add_contact("npub1y", label="alice")
    assert len(db.list_contacts()) == 2
    db.add_contact("npub1x", label="bob2")
    contacts = db.list_contacts()
    assert len(contacts) == 2
    assert next(c for c in contacts if c["npub"] == "npub1x")["label"] == "bob2"


def test_sqlite_still_used_without_kv(monkeypatch, tmp_path):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    assert not db._kv_backend_active()
    db_path = tmp_path / "t.db"
    cid = db.record_capsule("sent", capsule_uuid="caps-local", status="sent",
                            path=db_path)
    assert db.find_capsule("caps-local", path=db_path)["status"] == "sent"
    assert cid >= 1
