#!/usr/bin/env python3
"""Build the static frontend: writes public/index.html from stegstr/webui.py.

The web UI is a single self-contained page (inline CSS/JS, no external
assets), so the "frontend" is one file.  Vercel serves public/ statically at
the edge and routes /api/* to the Python function — same origin, no CORS.

Regenerate after editing stegstr/webui.py:
    python scripts/build_frontend.py
"""

from pathlib import Path

from stegstr.webui import PAGE

out = Path(__file__).resolve().parent.parent / "public" / "index.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(PAGE)
print(f"wrote {out} ({len(PAGE)} bytes)")
