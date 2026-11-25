"""Persistent + Real-Time Endpoint & Agent Request Tracker for RingScout.

Combines two telemetry layers so request counts NEVER reset on Cloud Run scale-to-zero or redeploy:
1. Google Cloud Logging (`run.googleapis.com%2Frequests`, cached 45s TTL) for exact historical
   HTTP request counts, status codes, latencies, and user-agent signatures across all revisions.
2. Live sub-second in-memory delta (`EndpointRequestTracker`) capturing brand-new requests not yet
   flushed to Cloud Logging plus fine-grained MCP JSON-RPC method counts (`initialize`, `tools/list`,
   `tools/call`, `ping`, `resources/*`, `prompts/*`).
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
import datetime
import logging
import os
import re
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
import httpx

logger = logging.getLogger(__name__)

_QUEST_PATH_RE = re.compile(r"^/api/quests/([^/]+)(/.*)?$")
_PSTN_WEBHOOK_RE = re.compile(r"^/api/pstn/(answer|status)/([^/]+)/.*$")
_WS_STREAM_RE = re.compile(r"^/ws/(pstn/stream|mic-call|twilio-media)/.*$")

_AGENT_SIGNATURES: list[tuple[str, str, str]] = [
    ("sentineloracle", "Glimind SentinelOracle", "mcp_directory"),
    ("rnwy-toollistread", "RNWY MCP Indexer", "mcp_directory"),
    ("mcpbeat", "MCPBeat Monitor", "mcp_directory"),
    ("alalanet-prober", "AlalaNet MCP Prober", "mcp_directory"),
    ("brickbluebot", "Brick.Blue Agentic Registry", "mcp_directory"),
    ("protogrid-probe", "Protogrid MCP Probe", "mcp_directory"),
    ("mcpgw-indexer", "MCPGW Gateway Indexer", "mcp_directory"),
    ("ainet-probe", "AINet Registry Probe", "mcp_directory"),
    ("mcp-directory-scanner", "RingScout Discovery Auditor", "mcp_directory"),
    ("mcp-sse-scanner", "RingScout SSE Auditor", "mcp_directory"),
    ("glama", "Glama.ai Scanner", "mcp_directory"),
    ("smithery", "Smithery.ai Scanner", "mcp_directory"),
    ("pulsemcp", "PulseMCP Crawler", "mcp_directory"),
    ("claudebot", "ClaudeBot (Anthropic)", "ai_search_bot"),
    ("claude-searchbot", "Claude SearchBot (Anthropic)", "ai_search_bot"),
    ("claude-user", "Claude Live Fetch (Anthropic)", "ai_assistant"),
    ("anthropic-ai", "Anthropic Crawler", "ai_search_bot"),
    ("oai-searchbot", "ChatGPT SearchBot (OpenAI)", "ai_search_bot"),
    ("chatgpt-user", "ChatGPT Live Connector (OpenAI)", "ai_assistant"),
    ("gptbot", "GPTBot Crawler (OpenAI)", "ai_search_bot"),
    ("perplexitybot", "PerplexityBot", "ai_search_bot"),
    ("perplexity-user", "Perplexity Live Fetch", "ai_assistant"),
    ("google-extended", "Gemini / Google-Extended", "ai_search_bot"),
    ("googleother", "Google AI Grounding", "ai_search_bot"),
    ("googlebot", "Googlebot Search", "ai_search_bot"),
    ("applebot", "Applebot / Siri", "ai_search_bot"),
    ("bingbot", "Bingbot / Copilot", "ai_search_bot"),
    ("amazonbot", "Amazonbot / Alexa AI", "ai_search_bot"),
    ("duckassistbot", "DuckAssistBot", "ai_search_bot"),
    ("python-httpx", "Python httpx Agent / SDK", "sdk_client"),
    ("python-requests", "Python requests Agent / Scanner", "sdk_client"),
    ("python-urllib", "Python urllib Agent / CLI", "sdk_client"),
    ("go-http-client", "Go HTTP Agent / Scanner", "sdk_client"),
    ("node", "Node.js / TypeScript MCP Client", "sdk_client"),
    ("undici", "Node.js / MCP Inspector Fetch", "sdk_client"),
    ("curl/", "curl CLI Probe", "sdk_client"),
    ("mozilla/", "Web Browser (Human / Inspector)", "browser"),
]


def normalize_endpoint_path(raw_path: str) -> str:
    """Collapse high-cardinality IDs so per-endpoint leaderboards stay crisp and leak-free."""
    path = (raw_path or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/") and path not in ("/mcp/",):
        path = path.rstrip("/")

    m_q = _QUEST_PATH_RE.match(path)
    if m_q:
        rest = m_q.group(2) or ""
        if rest.startswith("/calls/") and rest.endswith("/audio.wav"):
            return "/api/quests/{quest_id}/calls/{shop_id}/audio.wav"
        if rest == "/events":
            return "/api/quests/{quest_id}/events"
        if rest == "/stop":
            return "/api/quests/{quest_id}/stop"
        return "/api/quests/{quest_id}"

    m_p = _PSTN_WEBHOOK_RE.match(path)
    if m_p:
        return f"/api/pstn/{m_p.group(1)}/{{provider}}/{{quest_id}}/{{shop_id}}"

    if _WS_STREAM_RE.match(path):
        return "/ws/pstn/stream/{session}"

    return path


def classify_endpoint_category(path: str) -> str:
    if path in ("/mcp", "/mcp/", "/sse", "/messages", "/mcp/health", "/ping"):
        return "mcp_protocol"
    if path in (
        "/.well-known/mcp.json",
        "/.well-known/mcp",
        "/.well-known/mcp/server-card.json",
        "/mcp/server-card",
        "/server.json",
        "/.well-known/ai-catalog.json",
        "/.well-known/ai-catalog",
        "/.well-known/agents.json",
        "/agents.json",
        "/.well-known/ai-plugin.json",
        "/.well-known/openapi.json",
        "/openapi-actions.json",
        "/.well-known/security.txt",
        "/.well-known/ard.json",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
    ):
        return "discovery_well_known"
    if path in ("/a2a", "/.well-known/agent-card.json", "/.well-known/agent.json"):
        return "a2a_protocol"
    if path in ("/.well-known/x402", "/api/v1/x402/discovery", "/api/v1/scout", "/api/v1/pricing"):
        return "agentic_payments"
    if path in ("/llms.txt", "/llms-full.txt", "/.well-known/llms.txt", "/robots.txt", "/sitemap.xml"):
        return "geo_ai_search"
    if path in ("/", "/dashboard", "/developers", "/tracker", "/admin") or path.startswith("/api/"):
        return "web_and_app_api"
    return "other_scanner_probes"


def classify_user_agent(ua: str) -> tuple[str, str]:
    low = (ua or "").strip().lower()
    if not low:
        return ("Unknown / Headless Probe", "sdk_client")
    for needle, label, group in _AGENT_SIGNATURES:
        if needle in low:
            return (label, group)
    return (ua.strip()[:48], "sdk_client")


def _parse_latency_ms(lat_str: str | None) -> float | None:
    if not lat_str:
        return None
    txt = lat_str.strip().rstrip("s")
    try:
        return round(float(txt) * 1000.0, 2)
    except ValueError:
        return None


def _get_gcp_bearer_token() -> str | None:
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/logging.read"])
        creds.refresh(GoogleAuthRequest())
        if creds.token:
            return str(creds.token)
    except Exception:
        pass
    if os.environ.get("K_SERVICE"):
        return None
    try:
        env = dict(os.environ)
        env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
        env.setdefault("CLOUDSDK_CORE_ACCOUNT", "admin@manavagarwal.altostrat.com")
        out = subprocess.check_output(
            ["gcloud", "auth", "print-access-token", "--quiet"],
            env=env,
            stderr=subprocess.DEVNULL,
            timeout=3.5,
            text=True,
        ).strip()
        return out or None
    except Exception:
        return None


class EndpointRequestTracker:
    """Real-time + Cloud-Logging-backed tracker of all RingScout HTTP & MCP/A2A requests."""

    def __init__(self, project_id: str = "manav-chub", service_name: str = "ringscout") -> None:
        self.project_id = project_id
        self.service_name = service_name
        self.live_records: deque[dict[str, Any]] = deque(maxlen=4000)
        self.mcp_rpc_methods: Counter[str] = Counter()
        self._cloud_cache_entries: list[dict[str, Any]] = []
        self._cloud_cache_ts: float = 0.0
        self._cloud_latest_epoch: float = 0.0
        self._cloud_source_ok: bool = False
        self._lock = asyncio.Lock()

    def record_http_hit(
        self,
        *,
        method: str,
        raw_path: str,
        status_code: int,
        user_agent: str,
        latency_ms: float,
    ) -> None:
        norm_path = normalize_endpoint_path(raw_path)
        if norm_path in ("/favicon.ico",):
            return
        now = time.time()
        agent_label, agent_group = classify_user_agent(user_agent)
        self.live_records.append(
            {
                "epoch": now,
                "ts": datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "method": method.upper(),
                "path": norm_path,
                "status": int(status_code),
                "latency_ms": round(latency_ms, 2),
                "agent_label": agent_label,
                "agent_group": agent_group,
                "ua_short": (user_agent or "unknown")[:90],
                "category": classify_endpoint_category(norm_path),
            }
        )

    def record_mcp_rpc(self, rpc_body: Any) -> None:
        items = rpc_body if isinstance(rpc_body, list) else [rpc_body]
        for item in items:
            if not isinstance(item, dict):
                continue
            m = str(item.get("method") or "").strip()
            if not m:
                continue
            if m == "tools/call":
                params = item.get("params") if isinstance(item.get("params"), dict) else {}
                tname = str(params.get("name") or "unknown")
                self.mcp_rpc_methods[f"tools/call ({tname})"] += 1
            else:
                self.mcp_rpc_methods[m] += 1

    async def _refresh_cloud_logging_if_stale(self, window_hours: int = 168) -> None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        now = time.time()
        if self._cloud_cache_entries and (now - self._cloud_cache_ts) < 45.0:
            return
        async with self._lock:
            if self._cloud_cache_entries and (time.time() - self._cloud_cache_ts) < 45.0:
                return
            token = await asyncio.to_thread(_get_gcp_bearer_token)
            if not token:
                self._cloud_cache_ts = time.time()
                return

            since_iso = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=max(1, min(window_hours, 720)))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            flt = (
                f'resource.type="cloud_run_revision" '
                f'AND resource.labels.service_name="{self.service_name}" '
                f'AND httpRequest.requestUrl!="" '
                f'AND timestamp>="{since_iso}"'
            )

            collected: list[dict[str, Any]] = []
            page_token: str | None = None
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    for _ in range(6):  # Up to 6,000 Cloud Run requests
                        body: dict[str, Any] = {
                            "resourceNames": [f"projects/{self.project_id}"],
                            "filter": flt,
                            "orderBy": "timestamp desc",
                            "pageSize": 1000,
                        }
                        if page_token:
                            body["pageToken"] = page_token
                        resp = await client.post(
                            "https://logging.googleapis.com/v2/entries:list",
                            headers={"Authorization": f"Bearer {token}"},
                            json=body,
                        )
                        if resp.status_code != 200:
                            break
                        payload = resp.json()
                        entries = payload.get("entries") or []
                        for ent in entries:
                            hr = ent.get("httpRequest") or {}
                            raw_url = str(hr.get("requestUrl") or "/")
                            parsed_path = urlparse(raw_url).path or "/"
                            norm_path = normalize_endpoint_path(parsed_path)
                            if norm_path == "/favicon.ico":
                                continue
                            ts_raw = str(ent.get("timestamp") or "")
                            try:
                                dt = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                                ep = dt.timestamp()
                                ts_clean = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                            except Exception:
                                ep = 0.0
                                ts_clean = ts_raw[:20]
                            ua = str(hr.get("userAgent") or "")
                            ag_lbl, ag_grp = classify_user_agent(ua)
                            collected.append(
                                {
                                    "epoch": ep,
                                    "ts": ts_clean,
                                    "method": str(hr.get("requestMethod") or "GET").upper(),
                                    "path": norm_path,
                                    "status": int(hr.get("status") or 200),
                                    "latency_ms": _parse_latency_ms(hr.get("latency")) or 0.0,
                                    "agent_label": ag_lbl,
                                    "agent_group": ag_grp,
                                    "ua_short": (ua or "unknown")[:90],
                                    "category": classify_endpoint_category(norm_path),
                                }
                            )
                        page_token = payload.get("nextPageToken")
                        if not page_token or not entries:
                            break

                if collected:
                    self._cloud_cache_entries = collected
                    self._cloud_latest_epoch = max((r["epoch"] for r in collected), default=0.0)
                    self._cloud_source_ok = True
                self._cloud_cache_ts = time.time()
            except Exception as exc:
                logger.debug("Cloud Logging tracker sync skipped: %s", exc)
                self._cloud_cache_ts = time.time()

    async def build_report(self, window_hours: int = 168) -> dict[str, Any]:
        await self._refresh_cloud_logging_if_stale(window_hours=window_hours)

        if self._cloud_source_ok and self._cloud_cache_entries:
            # Merge historical Cloud Run logs + any brand-new live requests newer than latest log timestamp
            cutoff = self._cloud_latest_epoch + 0.001
            delta = [r for r in self.live_records if r["epoch"] > cutoff]
            merged = list(reversed(delta)) + self._cloud_cache_entries
            source_mode = "cloud_logging_plus_live_stream"
        else:
            merged = list(reversed(self.live_records))
            source_mode = "live_in_memory_stream"

        total_requests = len(merged)
        ep_stats: dict[str, dict[str, Any]] = {}
        status_buckets: Counter[str] = Counter()
        category_counts: Counter[str] = Counter()
        agent_counts: Counter[str] = Counter()
        agent_groups: Counter[str] = Counter()
        hourly_counts: dict[str, int] = defaultdict(int)
        all_latencies: list[float] = []

        for r in merged:
            key = f"{r['method']} {r['path']}"
            st = r["status"]
            lat = float(r.get("latency_ms") or 0.0)
            if lat > 0:
                all_latencies.append(lat)

            if 200 <= st < 300:
                status_buckets["2xx"] += 1
            elif 300 <= st < 400:
                status_buckets["3xx"] += 1
            elif st == 402:
                status_buckets["402_payment_challenge"] += 1
            elif 400 <= st < 500:
                status_buckets["4xx"] += 1
            else:
                status_buckets["5xx"] += 1

            category_counts[r["category"]] += 1
            agent_counts[r["agent_label"]] += 1
            agent_groups[r["agent_group"]] += 1
            if r["ts"] and len(r["ts"]) >= 13:
                hourly_counts[r["ts"][:13] + ":00Z"] += 1

            row = ep_stats.get(key)
            if row is None:
                row = {
                    "endpoint": key,
                    "method": r["method"],
                    "path": r["path"],
                    "category": r["category"],
                    "hits": 0,
                    "ok_2xx": 0,
                    "err_4xx_5xx": 0,
                    "latency_sum_ms": 0.0,
                    "latency_count": 0,
                    "last_seen": r["ts"],
                }
                ep_stats[key] = row
            row["hits"] += 1
            if 200 <= st < 300:
                row["ok_2xx"] += 1
            elif st >= 400 and st != 402:
                row["err_4xx_5xx"] += 1
            if lat > 0:
                row["latency_sum_ms"] += lat
                row["latency_count"] += 1

        endpoints_ranked = sorted(ep_stats.values(), key=lambda x: x["hits"], reverse=True)
        for row in endpoints_ranked:
            cnt = row.pop("latency_count")
            lsum = row.pop("latency_sum_ms")
            row["avg_latency_ms"] = round(lsum / cnt, 1) if cnt else 0.0
            row["share_pct"] = round((row["hits"] / total_requests) * 100.0, 1) if total_requests else 0.0

        sorted_lat = sorted(all_latencies)
        median_ms = round(sorted_lat[len(sorted_lat) // 2], 1) if sorted_lat else 0.0
        p95_ms = round(sorted_lat[int(len(sorted_lat) * 0.95)], 1) if sorted_lat else 0.0
        ok_rate = (
            round((status_buckets["2xx"] / total_requests) * 100.0, 1)
            if total_requests
            else 100.0
        )

        # Build last 24 hourly buckets cleanly
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        hourly_series = []
        for offset in range(23, -1, -1):
            slot = (now_utc - datetime.timedelta(hours=offset)).strftime("%Y-%m-%dT%H:00Z")
            hourly_series.append({"hour_utc": slot, "requests": hourly_counts.get(slot, 0)})

        return {
            "source_mode": source_mode,
            "window_hours": window_hours,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "summary": {
                "total_requests": total_requests,
                "unique_endpoints_hit": len(endpoints_ranked),
                "mcp_protocol_hits": category_counts["mcp_protocol"],
                "discovery_manifest_hits": category_counts["discovery_well_known"],
                "a2a_and_payment_hits": category_counts["a2a_protocol"] + category_counts["agentic_payments"],
                "geo_search_hits": category_counts["geo_ai_search"],
                "directory_and_ai_bot_hits": agent_groups["mcp_directory"] + agent_groups["ai_search_bot"] + agent_groups["ai_assistant"],
                "success_2xx_pct": ok_rate,
                "median_latency_ms": median_ms,
                "p95_latency_ms": p95_ms,
            },
            "status_breakdown": dict(status_buckets),
            "category_breakdown": dict(category_counts),
            "mcp_jsonrpc_methods": dict(self.mcp_rpc_methods.most_common(20)),
            "top_callers": [
                {"agent": k, "hits": v} for k, v in agent_counts.most_common(20)
            ],
            "hourly_volume_24h": hourly_series,
            "endpoints": endpoints_ranked[:50],
            "recent_requests": [
                {
                    "ts": r["ts"],
                    "method": r["method"],
                    "path": r["path"],
                    "status": r["status"],
                    "latency_ms": r["latency_ms"],
                    "agent": r["agent_label"],
                    "category": r["category"],
                }
                for r in merged[:40]
            ],
        }
