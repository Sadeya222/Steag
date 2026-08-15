"""Shared hermetic infra for network tests: one background event loop, a
local nostr relay, and a stub Blossom server.  All nostr-sdk FFI async calls
must happen on the single shared loop (the relay only stores/serves events
when clients run on the same loop that hosts it)."""

import asyncio
import hashlib
import http.server
import json
import threading

import nostr_sdk as ns

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def run(coro, timeout: float = 120.0):
    """Run a coroutine on the shared loop and return its result."""
    fut = asyncio.run_coroutine_threadsafe(coro, LOOP)
    return fut.result(timeout=timeout)


_RELAY = {"url": None, "local": None, "lock": threading.Lock()}


def get_relay_url() -> str:
    """Start (once) a local relay on the shared loop; returns its ws:// URL."""
    with _RELAY["lock"]:
        if _RELAY["url"] is not None:
            return _RELAY["url"]

        async def _start():
            local = ns.LocalRelayBuilder().build()
            await local.run()
            return local, str(await local.url())

        local, url = run(_start(), timeout=15)
        _RELAY["local"] = local  # keep alive: its store lives on the instance
        _RELAY["url"] = url
        return url


# --------------------------------------------------------------------------
# stub Blossom server
# --------------------------------------------------------------------------
BLOBS: dict[str, bytes] = {}
AUTH_HEADERS: list[str] = []


class BlossomHandler(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        sha = hashlib.sha256(data).hexdigest()
        BLOBS[sha] = data
        AUTH_HEADERS.append(self.headers.get("Authorization", ""))
        host, port = self.server.server_address
        body = json.dumps({
            "hash": sha, "sha256": sha, "size": len(data),
            "url": f"http://{host}:{port}/{sha}",
            "mime": self.headers.get("Content-Type", "application/octet-stream"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        sha = self.path.lstrip("/")
        data = BLOBS.get(sha)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def start_blossom_server() -> str:
    BLOBS.clear()
    AUTH_HEADERS.clear()
    srv = http.server.HTTPServer(("127.0.0.1", 0), BlossomHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"
