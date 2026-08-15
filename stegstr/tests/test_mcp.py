"""Layer 5 — MCP server tests: initialize, tools/list, tools/call over stdio."""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

# the stdio client spawns the server as a subprocess
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stegstr.crypto import generate_keys, nsec_of, npub_of


def _png_bytes(tmp_path, seed=7):
    rng = np.random.RandomState(seed)
    base = np.clip(rng.normal(120, 45, (512, 512, 3)), 0, 255).astype(np.uint8)
    img = Image.fromarray(base.astype(np.uint8))
    p = tmp_path / f"photo_{seed}.png"
    img.save(p, "PNG")
    return p


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEGSTR_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("STEGSTR_DB", str(tmp_path / "steg.db"))
    return tmp_path


async def _client():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "stegstr.mcp_server"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def test_mcp_tools_list(env):
    async def main():
        async for session in _client():
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            assert {"encode", "decode", "send_capsule", "capsule_status", "capacity"} <= set(names)
    import asyncio
    asyncio.run(main())


def test_mcp_encode_decode(env):
    async def main():
        async for session in _client():
            img = _png_bytes(env, seed=11)
            # capacity
            r = await session.call_tool("capacity", {"width": 1024, "height": 1024})
            txt = r.content[0].text
            assert "r2" in txt
            # encode
            out = env / "carrier.png"
            r = await session.call_tool("encode", {"image": str(img), "message": "hello mcp",
                                                   "out": str(out)})
            txt = r.content[0].text
            j = json.loads(txt)
            assert j["ok"] and out.exists()
            # decode
            r = await session.call_tool("decode", {"image": str(out)})
            j = json.loads(r.content[0].text)
            assert j["ok"] and j["plaintext"] == "hello mcp"
    import asyncio
    asyncio.run(main())


def test_mcp_encode_encrypted_roundtrip(env):
    async def main():
        async for session in _client():
            alice, bob = generate_keys(), generate_keys()
            img = _png_bytes(env, seed=23)
            out = env / "enc.png"
            r = await session.call_tool("encode", {
                "image": str(img), "message": "secret for bob",
                "out": str(out),
                "to": npub_of(bob.public_key()),
                "key": nsec_of(alice.secret_key()),
            })
            j = json.loads(r.content[0].text)
            assert j["ok"] and j["payload_type"] == "nip44"
            # bob decrypts
            r = await session.call_tool("decode", {"image": str(out), "key": nsec_of(bob.secret_key())})
            j = json.loads(r.content[0].text)
            assert j["plaintext"] == "secret for bob"
            assert j["sender_npub"] == npub_of(alice.public_key())
            # eve fails cleanly (tool returns is_error)
            eve = generate_keys()
            r = await session.call_tool("decode", {"image": str(out), "key": nsec_of(eve.secret_key())})
            assert r.is_error
    import asyncio
    asyncio.run(main())
