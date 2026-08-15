"""Vercel deployment artifacts: entrypoint, config, static frontend, deps."""

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_vercel_entrypoint_exports_app():
    sys.path.insert(0, str(ROOT))
    mod = importlib.import_module("api.index")
    assert hasattr(mod, "app")
    assert mod.app.title == "Stegstr API"
    # routes that matter for the deployed app
    paths = {r.path for r in mod.app.routes}
    assert "/api/health" in paths
    assert "/api/encode" in paths
    assert "/api/decode" in paths
    assert "/api/send" in paths


def test_vercel_json_valid():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    assert "functions" in cfg and "api/index.py" in cfg["functions"]
    f = cfg["functions"]["api/index.py"]
    assert f["maxDuration"] <= 300 and f["memory"] <= 2048
    destinations = {r["destination"] for r in cfg.get("rewrites", [])}
    assert destinations == {"/api/index.py"}


def test_public_frontend_exists_and_complete():
    html = (ROOT / "public" / "index.html").read_text()
    assert "<title>stegstr" in html
    # same-origin API by default; configurable base supported
    assert "STEGSTR_API_BASE" in html
    for marker in ("api/encode", "api/decode", "api/send", "api/status"):
        assert marker in html
    # no external resources (self-contained)
    assert "https://" not in html.replace("https://blossom", "")


def test_requirements_cover_api_imports():
    req = (ROOT / "requirements.txt").read_text()
    for dep in ("fastapi", "pydantic", "numpy", "Pillow", "reedsolo", "nostr-sdk",
                "python-multipart"):
        assert dep.lower() in req.lower(), f"{dep} missing from requirements.txt"


def test_python_version_pinned():
    v = (ROOT / ".python-version").read_text().strip()
    assert v.startswith("3.1")  # 3.10-3.13 are all fine for Vercel
