"""Layer 5 — MCP (Model Context Protocol) server for AI agents.

Exposes the same core operations as the JSON API as MCP tools over stdio:

    encode          embed a message into an image (optionally NIP-44 encrypted)
    decode          recover a message from a carrier image (optionally decrypt)
    send_capsule    publish a Nostr capsule for a carrier (sent state)
    capsule_status  read a capsule's state machine from relays
    capacity        payload capacity for a given image size

Run:  python -m stegstr.mcp_server
Agent config (e.g. Claude Desktop / Cursor):
    { "mcpServers": { "stegstr": { "command": "python", "args": ["-m", "stegstr.mcp_server"] } } }
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import stdio
from mcp.server.lowlevel import Server
from mcp.types import (
    CallToolRequestParams,
    PaginatedRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from . import __version__
from .api import service_capacity, service_decode, service_encode, service_send, service_status

server = Server("stegstr", version=__version__,
                instructions="Stegstr: hide encrypted messages in images that survive "
                             "WhatsApp/Instagram/Telegram re-compression, with Nostr "
                             "capsule sync. `image` args are local file paths.")


def _err(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"error: {message}")],
                          is_error=True)


def _ok(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _tools() -> list[Tool]:
    return [
        Tool(
            name="encode",
            description="Embed a message into an image. The output carrier survives "
                        "WhatsApp/Instagram/Telegram re-compression. If `to` and `key` "
                        "are given, the payload is NIP-44 encrypted for the receiver.",
            input_schema={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "path to the carrier image"},
                    "message": {"type": "string"},
                    "out": {"type": "string", "description": "output path (default <image>.steg.png)"},
                    "to": {"type": "string", "description": "receiver npub (optional; enables encryption)"},
                    "key": {"type": "string", "description": "sender nsec (required with `to`)"},
                    "quality": {"type": "integer", "default": 70, "description": "embed quality 1-100"},
                    "multiscale": {"type": "boolean", "default": False},
                },
                "required": ["image", "message"],
            },
        ),
        Tool(
            name="decode",
            description="Recover the message from a carrier image (the received file "
                        "from WhatsApp/Telegram/Instagram). Pass `key` to decrypt "
                        "NIP-44 payloads.",
            input_schema={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "path to the received image"},
                    "key": {"type": "string", "description": "your nsec (optional; decrypts)"},
                    "quality": {"type": "integer", "default": 70},
                },
                "required": ["image"],
            },
        ),
        Tool(
            name="send_capsule",
            description="Publish a Nostr capsule (kind 37300) announcing a carrier to a "
                        "receiver: state sent -> they sync delivered/decoded. Optionally "
                        "host the carrier on a Blossom server (content-addressed, "
                        "hash-verified) and send a NIP-17 metadata DM.",
            input_schema={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "path to the carrier image"},
                    "to": {"type": "string", "description": "receiver npub"},
                    "key": {"type": "string", "description": "your nsec"},
                    "relays": {"type": "array", "items": {"type": "string"},
                               "description": "relay URLs (default: public set)"},
                    "blossom": {"type": "string", "description": "blossom server URL (optional)"},
                    "note": {"type": "string"},
                    "quality": {"type": "integer", "default": 70},
                },
                "required": ["image", "to", "key"],
            },
        ),
        Tool(
            name="capsule_status",
            description="Read the full state machine of a capsule from the relays "
                        "(sent / delivered / decoded with authors and verify flags). "
                        "No key needed — the states are public events.",
            input_schema={
                "type": "object",
                "properties": {
                    "capsule_uuid": {"type": "string"},
                    "relays": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["capsule_uuid"],
            },
        ),
        Tool(
            name="capacity",
            description="How many payload bytes fit in an image of the given size at "
                        "each redundancy level.",
            input_schema={
                "type": "object",
                "properties": {
                    "width": {"type": "integer", "default": 1024},
                    "height": {"type": "integer", "default": 1024},
                    "quality": {"type": "integer", "default": 70},
                },
            },
        ),
    ]


async def handle_list_tools(ctx, params: PaginatedRequestParams) -> ListToolsResult:
    return ListToolsResult(tools=_tools())


async def handle_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    args = params.arguments or {}
    try:
        if name == "encode":
            image = Path(args["image"])
            out = Path(args.get("out") or image.with_name(image.stem + ".steg.png"))
            result = service_encode(image.read_bytes(), args["message"],
                                    args.get("to"), args.get("key"),
                                    int(args.get("quality", 70)),
                                    bool(args.get("multiscale", False)))
            # save the carrier next to the requested output path
            from .storage import load_carrier
            data, _ = load_carrier(result["carrier_id"])
            out.write_bytes(data)
            return _ok(json.dumps({
                "ok": True,
                "carrier_path": str(out),
                "carrier_id": result["carrier_id"],
                "payload_bytes": result["payload_bytes"],
                "payload_type": result["payload_type"],
                "sender_npub": result.get("sender_npub"),
                "receiver_npub": result.get("receiver_npub"),
            }, indent=2))
        if name == "decode":
            result = service_decode(Path(args["image"]).read_bytes(),
                                    args.get("key"), int(args.get("quality", 70)))
            return _ok(json.dumps(result, indent=2))
        if name == "send_capsule":
            result = service_send(
                Path(args["image"]).read_bytes(), args["to"], args["key"],
                relays=args.get("relays"), blossom=args.get("blossom"),
                note=args.get("note"), quality=int(args.get("quality", 70)),
            )
            return _ok(json.dumps(result, indent=2))
        if name == "capsule_status":
            result = service_status(args["capsule_uuid"], relays=args.get("relays"))
            return _ok(json.dumps(result, indent=2))
        if name == "capacity":
            result = service_capacity(int(args.get("width", 1024)),
                                      int(args.get("height", 1024)),
                                      int(args.get("quality", 70)))
            return _ok(json.dumps(result, indent=2))
        return _err(f"unknown tool {name!r}")
    except Exception as exc:  # noqa: BLE001 — surface any failure to the agent
        return _err(f"{type(exc).__name__}: {exc}")


server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)


async def main() -> None:
    # stdio transport: line-buffered output so responses flush immediately
    # even when spawned by an MCP client without -u
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:  # noqa: BLE001 — non-TTY streams may not support it
            pass
    async with stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
