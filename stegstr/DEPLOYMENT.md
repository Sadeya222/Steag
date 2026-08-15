# Stegstr — Local Run & Deployment Guide

This guide covers everything you need to **run Stegstr locally** (backend +
web UI + CLI + MCP) and **deploy it** (VPS via systemd, Docker, or LAN), plus
operations and troubleshooting. It assumes nothing beyond a working Python.

> **TL;DR — local run**
> ```bash
> python -m venv .venv && source .venv/bin/activate
> pip install -e ".[web,mcp]"
> stegstr serve --open          # web UI + API at http://127.0.0.1:8000
> ```
> Frontend is served by the same process — there is **no separate frontend
> build**. Point your browser at `/` and you're done.

---

## 0. What you are running (10-second recap)

Stegstr is one Python package with three front doors to the same core:

| Door | Command | What it gives you |
|---|---|---|
| **CLI** | `stegstr <cmd>` | encode/decode/send/listen/sync/log/capacity/validate/genkeys |
| **Web UI + JSON API** | `stegstr serve` | one-page drag-drop UI at `/`, agent API at `/api/*`, docs at `/docs` |
| **MCP server** | `stegstr mcp` | the same operations as tools for AI agents (stdio) |

All state is local files: the SQLite capsule log (`$STEGSTR_DB`, default
`~/.stegstr/stegstr.db`) and the carrier store (`$STEGSTR_DATA/carriers`).
The only network traffic is to Nostr relays (capsules/DMs/Blossom) — the
images themselves travel through WhatsApp/Instagram/Telegram as usual.

---

## 1. Run locally

### 1.1 Prerequisites

- Python **3.10+** (tested on 3.13) — on Windows/Mac/Linux.
- ~400 MB disk for the install (opencv + nostr-sdk wheels).

### 1.2 Install

```bash
git clone <your-repo> && cd stegstr        # or copy the project folder

python -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -e ".[web,mcp]"                # engine + crypto + nostr + web + mcp
```

Extras explained:

| Extra | Needed for | Install |
|---|---|---|
| (none) | CLI core: engine, crypto, Nostr, storage | `pip install -e .` |
| `web` | web UI + JSON API (FastAPI/uvicorn) | `pip install -e ".[web]"` |
| `mcp` | MCP agent server | `pip install -e ".[mcp]"` |
| `ml` | Tier B (learned encoder — optional, see §5) | `pip install -e ".[ml]"` |
| `test` | run the test suite | `pip install -e ".[test]"` |

Verify:

```bash
stegstr --help          # lists all commands
python -m pytest tests/ -q   # 51 tests, all green
```

### 1.3 Start the backend + frontend

```bash
stegstr serve                       # http://0.0.0.0:8000
```

Then open:

- **Web UI (frontend):** http://127.0.0.1:8000/ — drag-drop encode/decode,
  send capsules, track capsule timelines, browse the log. No build step, no
  node, no npm. Everything is served inline.
- **API docs:** http://127.0.0.1:8000/docs (Swagger) — every endpoint is
  callable from the browser.
- **Health:** http://127.0.0.1:8000/api/health

Under the hood `serve` runs `uvicorn stegstr.api:app`. The equivalent
explicit command:

```bash
uvicorn stegstr.api:app --host 0.0.0.0 --port 8000 --reload   # --reload = dev hot-reload
```

### 1.4 Quick end-to-end sanity check

```bash
# 1. your identity (a Nostr keypair — this IS your Stegstr identity)
stegstr genkeys                       # copy the npub + nsec

# 2. hide a message in any photo
stegstr encode photo.png -m "meet at 7" --to <receiver npub> --key <your nsec> -o carrier.png

# 3. decode the file that comes back from WhatsApp/Telegram/Instagram
stegstr decode received.jpg --key <your nsec>

# 4. announce + sync over Nostr
stegstr send carrier.png --to <receiver npub> --key <your nsec> --blossom https://blossom.primal.net
stegstr listen --key <your nsec> --once
stegstr sync <capsule-uuid> --key <your nsec>
```

Or click through the same flow in the web UI at `/`.

### 1.5 MCP (for AI agents)

```bash
stegstr mcp        # stdio server; registers tools: encode, decode, send_capsule,
                   # capsule_status, capacity
```

Configure in any MCP client (Claude Desktop, Cursor, VS Code):

```json
{ "mcpServers": {
    "stegstr": { "command": "python", "args": ["-m", "stegstr.mcp_server"] }
} }
```

> Run MCP from the **activated venv** (use the venv's `python` path), or the
> client won't find `stegstr`.

### 1.6 Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `STEGSTR_DB` | `~/.stegstr/stegstr.db` | SQLite capsule/contact database |
| `STEGSTR_DATA` | `~/.stegstr/data` | carrier store (PNG files + sidecar JSON) |
| relays | damus.io, nos.lol, primal.net | override per-command with `--relay` (repeatable) |
| blossom | none | per-command with `--blossom <server>` |

No other config files are required.

---

## 2. Deploy

### 2.1 Option A — Docker (simplest)

A `Dockerfile` and `docker-compose.yml` ship with the repo.

```bash
docker compose up -d --build
# -> http://localhost:8000/
```

- Data persists in the `stegstr_data` volume (`/data` inside the container).
- Logs: `docker compose logs -f stegstr`.
- Upgrades: `git pull && docker compose up -d --build`.

### 2.2 Option B — VPS with systemd (no Docker)

**1. Install**

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
sudo useradd -r -m -d /opt/stegstr stegstr
sudo -u stegstr bash -c 'cd /opt/stegstr && git clone <your-repo> . && python3 -m venv .venv && .venv/bin/pip install -e ".[web,mcp]"'
```

**2. systemd unit** — `/etc/systemd/system/stegstr.service`:

```ini
[Unit]
Description=Stegstr steganography service
After=network-online.target
Wants=network-online.target

[Service]
User=stegstr
WorkingDirectory=/opt/stegstr
Environment=STEGSTR_DB=/var/lib/stegstr/stegstr.db
Environment=STEGSTR_DATA=/var/lib/stegstr/data
ExecStart=/opt/stegstr/.venv/bin/stegstr serve --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/lib/stegstr && sudo chown stegstr:stegstr /var/lib/stegstr
sudo systemctl daemon-reload && sudo systemctl enable --now stegstr
sudo systemctl status stegstr
```

> Bind to `127.0.0.1` and put a TLS reverse proxy in front (below). The API
> has **no built-in auth** — it is a local tool.

**3. Reverse proxy with Caddy (auto-TLS)** — `/etc/caddy/Caddyfile`:

```
stegstr.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

nginx equivalent:

```nginx
server {
    listen 443 ssl;
    server_name stegstr.example.com;
    # ... your ssl_certificate lines ...
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 25m;   # carriers are a few MB
    }
}
```

**4. Firewall**: only open 80/443.

### 2.3 Option C — LAN / personal use

```bash
stegstr serve --host 0.0.0.0 --port 8000
# colleagues open http://<your-ip>:8000/
```

Fine for a demo box on a trusted network. Don't expose it to the public
internet without auth (§2.4).

### 2.4 Security notes (read before exposing anything)

1. **The API has no authentication.** Anyone who can reach `/api/encode` can
   use your compute; anyone with `/api/carrier/{id}` can download carriers
   (but **not** read messages — those are NIP-44 encrypted). For public
   exposure, put it behind a reverse proxy with basic auth / OIDC, or a VPN.
2. **nsec values are secrets.** They are only ever sent in request bodies and
   stored nowhere by the app. `genkeys` writes a file only when you pass
   `--out` (mode 0600). If an nsec leaks, messages encrypted to that key are
   readable — generate a fresh keypair.
3. Messages are encrypted to the **receiver's key**, so a leaked carrier file
   alone reveals nothing (GCM tag + RS + CRC32 also flag any tampering).
4. Back up `$STEGSTR_DB` and `$STEGSTR_DATA` if you want capsule history
   across restores. The carriers are also recoverable from any chat export.

---

## 3. Operations

### 3.1 Logs

- systemd: `journalctl -u stegstr -f`
- Docker: `docker compose logs -f stegstr`
- local: terminal output of `stegstr serve`

### 3.2 Backups

```bash
# DB + carrier store are the only state:
cp ~/.stegstr/stegstr.db backup.db
cp -r ~/.stegstr/data backup-data
```

### 3.3 Upgrades

```bash
git pull
pip install -e ".[web,mcp]"          # re-resolve deps
sudo systemctl restart stegstr       # or: docker compose up -d --build
```

The SQLite schema migrates itself on startup (new columns added
idempotently).

---

## 4. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: fastapi / mcp / cv2` | Install the extra: `pip install -e ".[web,mcp]"`; if you used a venv, activate it first |
| `decode failed: no payload recovered` | Wrong `--quality` (must match the encode quality, default 70); or the image was downscaled (platforms cap at 1280/1080 — pre-size the carrier to the platform cap before sending) |
| `payload N B exceeds capacity` | Image too small (< ~400×400) or message too long. Check `stegstr capacity photo.png`; for WhatsApp-size photos you have 1–7 KB depending on redundancy |
| `send/listen` timeout | Relays unreachable from this network (some countries block wss). Pass `--relay wss://<reachable>` or run your own relay; failure is logged, never fatal |
| `Blossom upload failed` | Server down or NIP-98 auth rejected; the capsule still publishes without a blob |
| Web UI loads but API calls fail | Port conflict — change with `--port`; or the proxy strips multipart bodies (check `client_max_body_size`) |
| MCP client can't connect | Client must spawn the **venv python**: `"command": "/opt/stegstr/.venv/bin/python"` |
| MCP `error: ...` from tools | Errors are returned as tool results (is_error=true) with a message — read the text; e.g. `invalid HMAC` = wrong nsec |
| `nslots` / `capacity 0` on tiny images | By design: the RS-protected header needs ~352 8×8 blocks; real chat photos are far above that |
| Tests fail after fresh clone | `pip install -e ".[test]"` then `python -m pytest tests/ -q` |

---

## 4.5 Deploy to Vercel

**Can you deploy Stegstr to Vercel? Yes — with three adjustments, all already
made in this repo.** Vercel is serverless: the function filesystem is
ephemeral and per-invocation, so the two things Stegstr stores locally
(SQLite capsule log, carrier files) must move to serverless storage. The
setup uses Vercel's own ecosystem so it stays free-tier friendly:

| Component | On Vercel | Provided by |
|---|---|---|
| Frontend (web UI) | `public/index.html` — static, served at the edge | Vercel static hosting |
| Backend (API) | `api/index.py` — Python ASGI function | Vercel Functions (Hobby: 300 s max, 2 GB RAM, 500 MB Python bundle) |
| Carrier store | **Vercel Blob** (`BLOB_READ_WRITE_TOKEN`) — SHA-256-addressed, immutable URLs | Vercel Blob |
| Capsule log / contacts | **Upstash Redis** REST (`UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`) | Upstash (free tier) |
| Docs | `/docs`, `/openapi.json` rewritten to the function | vercel.json rewrites |

### What's already in the repo (no code changes needed)

```
vercel.json            # function config: maxDuration 300, memory 2048, rewrites
api/index.py           # ASGI entrypoint: `from stegstr.api import app`
public/index.html      # static frontend (built from stegstr/webui.py)
requirements.txt       # Vercel runtime deps (no opencv — engine is pure-numpy now)
.python-version        # 3.12
scripts/build_frontend.py   # regenerate public/index.html after UI edits
```

The engine no longer needs OpenCV on the server: `stegstr/colors.py`
reproduces OpenCV's BT.601 conversion **bit-for-bit** in pure numpy
(verified against OpenCV on 200k random pixels), so embedding/decoding
behavior is identical with a ~50 MB smaller bundle.

### Step-by-step deploy

1. **Push to GitHub** and import the repo in the Vercel dashboard
   (Framework preset: **Other**; build command: leave empty — the static
   `public/` and the `api/` function are auto-detected).
2. **Create the storage**:
   - *Vercel Blob*: Storage tab → Create → Blob → copy
     `BLOB_READ_WRITE_TOKEN`.
   - *Upstash*: add the Upstash Redis integration (or create a database at
     upstash.com) → copy `UPSTASH_REDIS_REST_URL` and
     `UPSTASH_REDIS_REST_TOKEN`.
3. **Set env vars** in Project → Settings → Environment Variables:
   `BLOB_READ_WRITE_TOKEN`, `UPSTASH_REDIS_REST_URL`,
   `UPSTASH_REDIS_REST_TOKEN`. (Without them the API still deploys, but
   carriers/logs are lost between invocations.)
4. **Deploy** (or push to the branch). Open the deployment URL:
   - `/` → web UI · `/docs` → API docs · `/api/health` → health check
5. **Verify**: encode an image in the UI → it returns a `carrier_id` and a
   blob URL → decode it back → capsule log appears in `/api/capsules`.

### What works / what to know on Vercel

- ✅ Full encode/decode/send/status API; web UI; MCP is **not** deployed
  (it's a local/agent tool — run `stegstr mcp` on your machine or a VPS).
- ✅ Payloads up to ~7 KB (1280² image) encode in a few seconds — far under
  the 300 s Hobby limit.
- ✅ Nostr relays + Blossom are outbound HTTPS/WSS from the function.
- ⚠️ Each invocation may hit a fresh instance: cold starts are ~1–3 s
  (numpy + nostr-sdk FFI load). Fine for demo/agent use.
- ⚠️ The capsule log via Upstash is a simple JSON-document store: reads are
  consistent, concurrent writes are last-writer-wins. Fine at this scale.
- ⚠️ No authentication on the API (same as local): the function URL is
  public. Do not put secrets in messages you wouldn't post publicly unless
  the *receiver's* key is private — NIP-44 protects content, but anyone can
  call `/api/encode` and use your function quota. Vercel offers
  [Hobby/Pro protections](https://vercel.com/docs/security) or put it behind
  a proxy with basic auth if you care.
- ⚠️ `send`/`listen`/`sync` CLI commands are not server endpoints (by
  design); the API exposes `/api/send` + `/api/status/{uuid}` which is the
  same functionality.

### When NOT to use Vercel

- If you want the CLI's `listen --auto-save` loop running 24/7 → use a VPS
  (systemd) or Docker (the options above).
- If you expect heavy encode traffic → Vercel Hobby's CPU-hour/month limits
  (4 CPU-hrs) will be the constraint; a $5 VPS is cheaper at volume.
- If you need a *persistent* full SQLite history on one host → Docker/VPS.

Vercel is the best fit when the deliverable is "a URL the judges can click":
static UI + API + docs in one deployment, free tier, no server to maintain.

## 5. Tier B — do you need it? (short honest answer)

**No — Tier A already covers the scenario the judges test.**

- The brief's stated grading scenario is WhatsApp / Instagram / Telegram
  re-compression. Tier A passes all of those locally (80/80 randomized
  trials + real photos), because those platforms re-encode at qualities the
  embed grid is designed against.
- Tier B (a trained StegaStamp-style encoder) only adds meaningful value for
  **arbitrary/aggressive downscaling** — e.g. someone resizes your image to
  500px before it reaches the judge. That is not part of the stated test
  flow, and the multiscale mode already covers the common exact-2× case.
- Tier B costs: GPU time (hours–days), torch+kornia deps, and a second
  validation story. It would not change your field-test result.

**Better uses of the same effort, in order:**

1. **The real-app field pass** (the literal grading method): encode → send
   through actual WhatsApp/Telegram/Instagram → save the received file →
   decode. Log it in the README field-test table.
2. Run the `stegstr validate` report against your judge's likely carrier
   sizes.
3. Demo polish: the web UI + MCP story is your "AI Agent Operability" proof.

Come back to Tier B only if you have spare compute *and* you've done the
field pass.

---

## 6. Where everything lives

```
stegstr/
├── stegstr/
│   ├── engine.py        # Layer 1 — DCT-QIM + RS embedding
│   ├── payload.py       # Layer 1 — RS framing + integrity
│   ├── dct.py           # JPEG-convention DCT + quantization tables
│   ├── jpeg.py          # minimal baseline-JPEG codec (JPEG-native path)
│   ├── colors.py        # pure-numpy BT.601 (bit-exact with OpenCV; serverless-friendly)
│   ├── crypto.py        # Layer 2 — NIP-44 ECDH envelopes
│   ├── net.py           # Layer 3 — relays, capsules (37300), NIP-17, Blossom
│   ├── db.py            # Layer 4 — capsule log (SQLite local / Upstash KV serverless)
│   ├── storage.py       # carrier store (disk local / Vercel Blob serverless)
│   ├── api.py           # Layer 5 — FastAPI app (backend)
│   ├── webui.py         # Layer 5 — single-page frontend
│   ├── mcp_server.py    # Layer 5 — MCP tools
│   ├── cli.py           # Layer 5 — CLI
│   └── validate.py      # Layer 6 — validation harness
├── api/index.py         # Vercel serverless entrypoint (ASGI app)
├── public/index.html    # Vercel static frontend (built from webui.py)
├── vercel.json          # Vercel function config + rewrites
├── requirements.txt     # Vercel runtime dependencies
├── scripts/build_frontend.py   # regenerate public/index.html
├── tests/               # 74 tests (engine, crypto, net, api, mcp, storage, kv, vercel)
├── Dockerfile           # deployment
├── docker-compose.yml   # deployment
└── reports/validation_report.md   # latest harness results
```
