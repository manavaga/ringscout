#!/usr/bin/env python3
"""External AI Agent & MCP Directory Scanner Simulator for RingScout.

Verifies all 4 discovery layers against a live RingScout base URL:
  1. Central Registry & Uptime Probes (`/server.json`, `/ping`, `/mcp/health`)
  2. RFC 8615 / SEP-2127 Domain Auto-Discovery (`/.well-known/ai-catalog.json`,
     `/.well-known/mcp/server-card.json`, `/.well-known/mcp.json`, `/.well-known/agent-card.json`,
     `/openapi-actions.json`, `/.well-known/x402`)
  3. AI Search / GEO Grounding (`/llms.txt`, `/llms-full.txt`, `/robots.txt`, `/sitemap.xml`)
  4. Full MCP Protocol Handshake (`OPTIONS /mcp` CORS, `HEAD /mcp`, `POST /mcp` initialize ->
     notifications/initialized -> ping -> tools/list -> resources/list -> resources/read ->
     prompts/list -> prompts/get -> batch JSON-RPC -> tools/call -> `DELETE /mcp` + Legacy `/sse`)

Usage:
  python3 scripts/verify_mcp_discovery.py http://127.0.0.1:8088
  python3 scripts/verify_mcp_discovery.py https://ringscout-hom7ntreuq-uc.a.run.app --legacy-only-ok
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def _http_call(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 12.0,
) -> tuple[int, dict[str, str], bytes]:
    hdrs = {"User-Agent": "MCP-Directory-Scanner/2026.1"}
    if headers:
        hdrs.update(headers)
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp_headers, resp.read()
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, resp_headers, exc.read()


def _sse_first_event(url: str, timeout: float = 5.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "text/event-stream", "User-Agent": "MCP-SSE-Scanner/2026.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            lines: list[str] = []
            for _ in range(3):
                line = resp.readline().decode("utf-8", errors="replace")
                if not line:
                    break
                lines.append(line)
                if line.strip() == "" and lines:
                    break
            return resp.status, "".join(lines)
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def run_audit(base_url: str, *, strict_v31: bool = True) -> int:
    clean = base_url.rstrip("/")
    passed = 0
    failed = 0
    warned = 0

    def record(ok: bool, label: str, detail: str = "", *, optional: bool = False) -> None:
        nonlocal passed, failed, warned
        if ok:
            passed += 1
            print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
        elif optional and not strict_v31:
            warned += 1
            print(f"  [WARN] {label} (pending redeploy)" + (f" — {detail}" if detail else ""))
        else:
            failed += 1
            print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))

    print(f"\n=== RingScout MCP & Agent Discovery Audit: {clean} ===")

    # 1. Registry & Health
    st, _, raw = _http_call(f"{clean}/api/health")
    health_obj = json.loads(raw.decode("utf-8")) if st == 200 else {}
    record(st == 200 and health_obj.get("status") == "ok", "GET /api/health", f"HTTP {st}")

    st_ping, _, _ = _http_call(f"{clean}/ping")
    st_mh, _, _ = _http_call(f"{clean}/mcp/health")
    record(st_ping == 200 and st_mh == 200, "GET /ping & /mcp/health (Directory Uptime Probes)", f"HTTP {st_ping}/{st_mh}", optional=True)

    st_srv, _, raw_srv = _http_call(f"{clean}/server.json")
    srv_obj = json.loads(raw_srv.decode("utf-8")) if st_srv == 200 else {}
    desc_len = len(str(srv_obj.get("description") or ""))
    remotes = srv_obj.get("remotes") or []
    record(
        st_srv == 200 and bool(remotes) and remotes[0].get("type") == "streamable-http",
        "GET /server.json (Official MCP Registry)",
        f"HTTP {st_srv}, remotes={len(remotes)}, desc_len={desc_len}",
    )
    record(
        0 < desc_len <= 100 and "status" not in srv_obj and isinstance(srv_obj.get("repository"), dict),
        "server.json 2025-12-11 strict schema (desc <= 100, repository object, no status)",
        f"desc_len={desc_len}",
        optional=True,
    )

    # 2. Well-Known Auto-Discovery
    st_mcp_wk, _, raw_mcp_wk = _http_call(f"{clean}/.well-known/mcp.json")
    mcp_wk = json.loads(raw_mcp_wk.decode("utf-8")) if st_mcp_wk == 200 else {}
    record(
        st_mcp_wk == 200 and "scout_places_on_google_maps" in (mcp_wk.get("tools") or []),
        "GET /.well-known/mcp.json",
        f"tools={mcp_wk.get('tools')}",
    )

    st_sc, hdr_sc, raw_sc = _http_call(f"{clean}/.well-known/mcp/server-card.json")
    sc_obj = json.loads(raw_sc.decode("utf-8")) if st_sc == 200 else {}
    sc_desc_len = len(str(sc_obj.get("description") or ""))
    record(
        st_sc == 200
        and "application/mcp-server-card+json" in hdr_sc.get("content-type", "")
        and sc_obj.get("$schema", "").endswith("server-card.schema.json")
        and 0 < sc_desc_len <= 100
        and isinstance(sc_obj.get("repository"), dict),
        "GET /.well-known/mcp/server-card.json (SEP-2127 ServerCard schema + MIME)",
        f"HTTP {st_sc}, ct={hdr_sc.get('content-type')}, desc_len={sc_desc_len}",
        optional=True,
    )

    st_cat, hdr_cat, raw_cat = _http_call(f"{clean}/.well-known/ai-catalog.json")
    cat_obj = json.loads(raw_cat.decode("utf-8")) if st_cat == 200 else {}
    entries = cat_obj.get("entries") or []
    record(
        st_cat == 200
        and "application/ai-catalog+json" in hdr_cat.get("content-type", "")
        and cat_obj.get("specVersion") == "1.0"
        and len(entries) >= 3
        and all("identifier" in e and "type" in e and "url" in e for e in entries),
        "GET /.well-known/ai-catalog.json (SEP-2127 AI Catalog specVersion + entries)",
        f"HTTP {st_cat}, ct={hdr_cat.get('content-type')}, entries={len(entries)}",
        optional=True,
    )

    st_a2a, _, raw_a2a = _http_call(f"{clean}/.well-known/agent-card.json")
    a2a_obj = json.loads(raw_a2a.decode("utf-8")) if st_a2a == 200 else {}
    record(
        st_a2a == 200 and a2a_obj.get("protocolVersion") == "1.0",
        "GET /.well-known/agent-card.json (Google A2A v1.0)",
        f"HTTP {st_a2a}",
    )

    st_oa, _, raw_oa = _http_call(f"{clean}/openapi-actions.json")
    oa_obj = json.loads(raw_oa.decode("utf-8")) if st_oa == 200 else {}
    record(
        st_oa == 200 and oa_obj.get("openapi") == "3.1.0",
        "GET /openapi-actions.json (ChatGPT Actions OpenAPI 3.1)",
        f"HTTP {st_oa}",
    )

    st_x4, _, raw_x4 = _http_call(f"{clean}/.well-known/x402")
    record(st_x4 == 200 and b"x402Version" in raw_x4, "GET /.well-known/x402", f"HTTP {st_x4}")

    # 3. GEO / AI Search Grounding
    st_llms, _, raw_llms = _http_call(f"{clean}/llms.txt")
    st_llms_full, _, _ = _http_call(f"{clean}/llms-full.txt")
    record(
        st_llms == 200 and st_llms_full == 200 and b"scout_places_on_google_maps" in raw_llms,
        "GET /llms.txt & /llms-full.txt (LLM Grounding Index)",
        f"HTTP {st_llms}/{st_llms_full}, {len(raw_llms)} bytes",
    )

    st_rob, _, raw_rob = _http_call(f"{clean}/robots.txt")
    st_sm, _, raw_sm = _http_call(f"{clean}/sitemap.xml")
    record(
        st_rob == 200 and st_sm == 200 and b"OAI-SearchBot" in raw_rob and b"ClaudeBot" in raw_rob,
        "GET /robots.txt & /sitemap.xml (AI Search Crawler Allowlist)",
        f"HTTP {st_rob}/{st_sm}",
    )

    # 4. Streamable HTTP MCP Protocol Handshake, CORS, Batch, Resources, Prompts, & Legacy SSE
    st_opt, hdr_opt, _ = _http_call(
        f"{clean}/mcp",
        method="OPTIONS",
        headers={
            "Origin": "https://inspector.modelcontextprotocol.io",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,mcp-session-id",
        },
    )
    record(
        st_opt == 200 and hdr_opt.get("access-control-allow-origin") == "*",
        "OPTIONS /mcp (Browser MCP Inspector CORS Preflight)",
        f"HTTP {st_opt}, methods={hdr_opt.get('access-control-allow-methods')}",
    )

    st_head, hdr_head, _ = _http_call(f"{clean}/mcp", method="HEAD")
    record(
        st_head == 200 and "mcp-protocol-version" in hdr_head,
        "HEAD /mcp (Liveness + MCP-Protocol-Version header)",
        f"HTTP {st_head}",
        optional=True,
    )

    st_init, hdr_init, raw_init = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers={"Origin": "https://inspector.modelcontextprotocol.io"},
        json_body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ringscout-discovery-auditor", "version": "1.0.0"},
            },
        },
    )
    init_obj = json.loads(raw_init.decode("utf-8")) if st_init == 200 else {}
    init_res = init_obj.get("result") or {}
    sid = hdr_init.get("mcp-session-id", "")
    record(
        st_init == 200 and init_res.get("protocolVersion") == "2025-06-18",
        "POST /mcp initialize (2025-06-18)",
        f"server={init_res.get('serverInfo', {}).get('name')}, session={bool(sid)}",
    )
    record(
        bool(init_res.get("instructions"))
        and bool(sid)
        and "Mcp-Session-Id" in hdr_init.get("access-control-expose-headers", ""),
        "POST /mcp initialize instructions + CORS-exposed Mcp-Session-Id",
        f"instructions_len={len(init_res.get('instructions') or '')}",
        optional=True,
    )

    req_hdrs = {"Mcp-Session-Id": sid} if sid else {}
    st_notif, _, _ = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    record(st_notif == 202, "POST /mcp notifications/initialized -> 202 Accepted", f"HTTP {st_notif}")

    st_p, _, raw_p = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={"jsonrpc": "2.0", "id": 2, "method": "ping"},
    )
    p_obj = json.loads(raw_p.decode("utf-8")) if st_p == 200 else {}
    record(st_p == 200 and p_obj.get("result") == {}, "POST /mcp ping -> {}", f"HTTP {st_p}")

    st_tl, _, raw_tl = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    tl_obj = json.loads(raw_tl.decode("utf-8")) if st_tl == 200 else {}
    tools = (tl_obj.get("result") or {}).get("tools") or []
    record(st_tl == 200 and len(tools) >= 3, "POST /mcp tools/list", f"count={len(tools)}")
    record(
        all("annotations" in t for t in tools) and bool(tools),
        "POST /mcp tools/list MCP annotations (readOnlyHint / openWorldHint)",
        f"annotated={sum(1 for t in tools if 'annotations' in t)}/{len(tools)}",
        optional=True,
    )

    st_rl, _, raw_rl = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    )
    rl_obj = json.loads(raw_rl.decode("utf-8")) if st_rl == 200 else {}
    resources = (rl_obj.get("result") or {}).get("resources")
    st_rr, _, raw_rr = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={
            "jsonrpc": "2.0",
            "id": 41,
            "method": "resources/read",
            "params": {"uri": "ringscout://status/capabilities"},
        },
    )
    rr_ok = st_rr == 200 and bool(((json.loads(raw_rr.decode("utf-8")).get("result") or {}).get("contents")))
    record(
        st_rl == 200 and isinstance(resources, list) and len(resources) >= 3 and rr_ok,
        "POST /mcp resources/list & resources/read",
        f"resources={len(resources) if isinstance(resources, list) else 'unsupported'}",
        optional=True,
    )

    st_pl, _, raw_pl = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={"jsonrpc": "2.0", "id": 5, "method": "prompts/list"},
    )
    pl_obj = json.loads(raw_pl.decode("utf-8")) if st_pl == 200 else {}
    prompts = (pl_obj.get("result") or {}).get("prompts")
    st_pg, _, raw_pg = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={
            "jsonrpc": "2.0",
            "id": 51,
            "method": "prompts/get",
            "params": {"name": "scout_restaurant_table", "arguments": {"city": "Mumbai", "locality": "Bandra West"}},
        },
    )
    pg_ok = st_pg == 200 and bool(((json.loads(raw_pg.decode("utf-8")).get("result") or {}).get("messages")))
    record(
        st_pl == 200 and isinstance(prompts, list) and len(prompts) >= 3 and pg_ok,
        "POST /mcp prompts/list & prompts/get",
        f"prompts={len(prompts) if isinstance(prompts, list) else 'unsupported'}",
        optional=True,
    )

    st_batch, _, raw_batch = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body=[
            {"jsonrpc": "2.0", "id": 60, "method": "ping"},
            {"jsonrpc": "2.0", "id": 61, "method": "tools/list"},
        ],
    )
    batch_ok = False
    if st_batch == 200:
        b_arr = json.loads(raw_batch.decode("utf-8"))
        batch_ok = isinstance(b_arr, list) and len(b_arr) == 2
    record(batch_ok, "POST /mcp JSON-RPC 2.0 batch support", f"HTTP {st_batch}", optional=True)

    st_tc, _, raw_tc = _http_call(
        f"{clean}/mcp",
        method="POST",
        headers=req_hdrs,
        json_body={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "get_ringscout_pricing_and_status", "arguments": {}},
        },
    )
    tc_obj = json.loads(raw_tc.decode("utf-8")) if st_tc == 200 else {}
    tc_res = tc_obj.get("result") or {}
    record(
        st_tc == 200 and tc_res.get("isError") is False,
        "POST /mcp tools/call get_ringscout_pricing_and_status",
        f"HTTP {st_tc}",
    )

    st_del, _, _ = _http_call(f"{clean}/mcp", method="DELETE", headers=req_hdrs)
    record(st_del == 204, "DELETE /mcp -> 204 No Content", f"HTTP {st_del}", optional=True)

    st_sse, sse_event = _sse_first_event(f"{clean}/sse")
    record(
        st_sse == 200 and "event: endpoint" in sse_event and "/messages?session_id=" in sse_event,
        "GET /sse (Legacy 2024-11-05 HTTP+SSE handshake)",
        f"HTTP {st_sse}",
        optional=True,
    )

    st_sse_post, _, raw_sse_post = _http_call(
        f"{clean}/sse",
        method="POST",
        json_body={"jsonrpc": "2.0", "id": 70, "method": "ping"},
    )
    record(
        st_sse_post == 200 and json.loads(raw_sse_post.decode("utf-8")).get("result") == {},
        "POST /sse (Dual-Transport Prober Compatibility)",
        f"HTTP {st_sse_post}",
        optional=True,
    )

    st_root, hdr_root, raw_root = _http_call(f"{clean}/")
    link_hdr = hdr_root.get("link", "")
    record(
        st_root == 200
        and 'rel="ai-catalog"' in link_hdr
        and 'rel="mcp-server-card"' in link_hdr
        and 'rel="agent-card"' in link_hdr
        and b'rel="mcp-server-card"' in raw_root,
        "GET / RFC 8288 HTTP Link headers + HTML <link> auto-discovery",
        f"HTTP {st_root}",
        optional=True,
    )

    st_agj, _, raw_agj = _http_call(f"{clean}/.well-known/agents.json")
    st_aip, _, _ = _http_call(f"{clean}/.well-known/ai-plugin.json")
    st_sec, _, raw_sec = _http_call(f"{clean}/.well-known/security.txt")
    record(
        st_agj == 200 and st_aip == 200 and st_sec == 200 and b"ringscout" in raw_agj and b"Contact:" in raw_sec,
        "GET /.well-known/{agents.json, ai-plugin.json, security.txt}",
        f"HTTP {st_agj}/{st_aip}/{st_sec}",
        optional=True,
    )

    print(f"\nSummary for {clean}: {passed} passed, {warned} warnings, {failed} failed.")
    return 1 if failed > 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify RingScout MCP & Multi-Directory Discovery")
    parser.add_argument(
        "base_url",
        nargs="?",
        default="http://127.0.0.1:8088",
        help="Base URL of RingScout server",
    )
    parser.add_argument(
        "--legacy-only-ok",
        action="store_true",
        help="Treat newly added v3.1 routes as warnings instead of failures when auditing a pre-redeploy host",
    )
    args = parser.parse_args()
    sys.exit(run_audit(args.base_url, strict_v31=not args.legacy_only_ok))


if __name__ == "__main__":
    main()
