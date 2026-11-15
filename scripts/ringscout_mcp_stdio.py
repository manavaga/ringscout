#!/usr/bin/env python3
"""Stdio Model Context Protocol (MCP) Server for RingScout.

Allows local MCP hosts (Claude Desktop, Gemini CLI, Cursor, Jetski Stdio) to invoke
RingScout's `scout_places_on_google_maps` and `get_ringscout_pricing_and_status` tools
over `stdin` / `stdout` JSON-RPC 2.0, forwarding either to a running RingScout HTTP server
(`RINGSCOUT_MCP_URL`, default `http://127.0.0.1:8088/mcp`) or executing in-process.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx


async def _dispatch_line(line: str, mcp_url: str, passcode: str) -> dict[str, Any] | None:
    rpc = json.loads(line)
    headers = {"Content-Type": "application/json"}
    if passcode:
        headers["X-RingScout-Passcode"] = passcode

    try:
        async with httpx.AsyncClient(timeout=95.0) as http:
            resp = await http.post(mcp_url, headers=headers, json=rpc)
            if resp.status_code == 202 or not resp.text.strip():
                return None
            return resp.json()
    except Exception:
        # Fallback to direct in-process execution if daemon is not running
        from ringscout.a2a_gateway import handle_mcp_jsonrpc
        from ringscout.server import QuestManager

        mgr = QuestManager(synthesize_audio=True, require_provenance=True)
        return await handle_mcp_jsonrpc(
            rpc,
            manager=mgr,
            base_url="https://ringscout-hom7ntreuq-uc.a.run.app",
            public_wss_ready=True,
            payment_sig_header=None,
            auth_header=None,
            sandbox_header=None,
            ap2_header_raw=None,
            owner_passcode_valid=bool(passcode),
        )


async def main_async() -> None:
    mcp_url = os.environ.get("RINGSCOUT_MCP_URL", "http://127.0.0.1:8088/mcp").strip()
    passcode = os.environ.get("RINGSCOUT_PASSCODE", "").strip()
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            out = await _dispatch_line(stripped, mcp_url, passcode)
            if out is not None:
                sys.stdout.write(json.dumps(out, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main_async())
