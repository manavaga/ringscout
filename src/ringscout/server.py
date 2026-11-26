"""FastAPI Server for RingScout v2.0:
- Track 1 (`/`): Modern Consumer H2A Website across 9 Google Maps Location Types
- Track 2 (`/developers`, `/mcp`, `/a2a`, `/openapi-actions.json`, `/.well-known/*`, `/llms.txt`):
  Out-of-the-Box Claude, Gemini & ChatGPT Connectors (Phase A Free) + Switchable Stripe MPP & x402 USDC (Phase B)
- Operator Console (`/admin`): Multi-DID Carrier Pool, Call Transcript/.wav Logs, Payment Gate Toggle, & Organic AI Traffic Analytics
"""

from __future__ import annotations

import asyncio
import base64
from collections import Counter, OrderedDict, defaultdict, deque
import datetime
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import random
import re
import time
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ringscout.a2a_gateway import (
    A2A_TASK_STORE,
    BASE_SEPOLIA_CAIP2_NETWORK,
    PaymentRequiredError,
    build_402_challenge,
    build_agent_card,
    build_agents_json_manifest,
    build_ai_catalog_manifest,
    build_ai_plugin_manifest,
    build_mcp_registry_manifest,
    build_mcp_server_card,
    build_official_server_json,
    build_openapi_actions_spec,
    build_payment_response_headers,
    build_x402_discovery_manifest,
    create_signed_demo_x402_payload,
    demo_buyer_wallet_address,
    execute_a2a_scout,
    handle_a2a_jsonrpc,
    handle_mcp_jsonrpc,
    handle_mcp_payload,
    verify_a2a_payment,
)
from ringscout.carriers import (
    BrowserMicAdapter,
    HinglishSwarmAdapter,
    LiveIndiaPstnAdapter,
    _normalize_public_urls,
)
from ringscout.cloud_run_bridge import (
    cloud_run_base_url,
    is_bridge_enabled,
    load_local_carrier_config,
    relay_quest_via_cloud_run,
    relay_self_test_via_cloud_run,
    save_local_carrier_config,
    sync_carrier_config_to_cloud_run,
)
from ringscout.google_lookup import discover_indian_shops, normalize_indian_phone
from ringscout.india_locations import INDIA_CITY_AREAS
from ringscout.models import (
    A2AScoutRequest,
    CallMode,
    CallOutcome,
    CallStatus,
    CarrierProvider,
    CustomPlaceAddRequest,
    DiscoverRequest,
    DiscoverResponse,
    IndianShopTarget,
    LiveCarrierConfig,
    PlaceCategory,
    QuestCreateRequest,
    QuestSession,
    SelfTestCallRequest,
    ShopCallState,
    VocalTone,
)
from ringscout.request_tracker import EndpointRequestTracker, normalize_endpoint_path
from ringscout.secrets_manager import get_secret
from ringscout.stripe_protocol import (
    create_signed_demo_stripe_mpp_credential,
    masked_payment_status,
    update_runtime_payment_config,
)
from ringscout.voice_engine import (
    GCP_PROJECT_ID,
    LIVE_AUDIO_MODEL,
    SIDE_BRAIN_MODEL,
    compile_conversational_call_plan,
    heuristic_conversational_call_plan,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_SAFE_SHOP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_GENERIC_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

INDIA_POC_PRESETS: list[dict[str, Any]] = [
    {
        "id": "hotels_bandra",
        "place_category": PlaceCategory.HOTEL.value,
        "badge": "🏨 Hotels & Stays · Mumbai",
        "title": "Tonight Room + 10 AM Early Check-In & Direct Tariff",
        "city": "Mumbai",
        "locality": "Bandra West",
        "lookup_query": "hotels boutique stays open now",
        "user_goal": "Deluxe double room for tonight, check if 10 AM early check-in is possible and confirm the direct phone tariff in ₹",
        "preferred_tone": VocalTone.POLITE_INDIAN_ENGLISH.value,
    },
    {
        "id": "restaurants_indiranagar",
        "place_category": PlaceCategory.RESTAURANT.value,
        "badge": "🍽️ Restaurants & Cafes · Bengaluru",
        "title": "Table for 4 Tonight at 8:30 PM + Outdoor Seating",
        "city": "Bengaluru",
        "locality": "Indiranagar",
        "lookup_query": "restaurants cafes dining open now",
        "user_goal": "Table for 4 tonight at 8:30 PM with outdoor seating and check live wait time",
        "preferred_tone": VocalTone.POLITE_INDIAN_ENGLISH.value,
    },
    {
        "id": "mri_bandra",
        "place_category": PlaceCategory.DIAGNOSTICS.value,
        "badge": "🩻 Diagnostics & MRI · Mumbai",
        "title": "Same-Day 1.5T Brain MRI Slot in Bandra West",
        "city": "Mumbai",
        "locality": "Bandra West",
        "lookup_query": "MRI scan center radiology imaging hospital",
        "user_goal": "Check if a 1.5T Brain MRI (plain) slot is available today and confirm the walk-in cash price in ₹",
        "preferred_tone": VocalTone.POLITE_INDIAN_ENGLISH.value,
    },
    {
        "id": "petrol_bandra",
        "place_category": PlaceCategory.PETROL.value,
        "badge": "⛽ Petrol & EV · Mumbai",
        "title": "XP95 High-Octane Petrol + Nitrogen Air Active Now",
        "city": "Mumbai",
        "locality": "Bandra West",
        "lookup_query": "petrol pump fuel station EV charging open now",
        "user_goal": "XP95 petrol stock and nitrogen tyre air counter working right now",
        "preferred_tone": VocalTone.CONVERSATIONAL_HINGLISH.value,
    },
    {
        "id": "tyres_hsr",
        "place_category": PlaceCategory.AUTO.value,
        "badge": "🔧 Auto & Tyres · Bengaluru",
        "title": "Bridgestone Sturdo 195/55 R16 Installed Quote",
        "city": "Bengaluru",
        "locality": "HSR Layout",
        "lookup_query": "car tyre dealer wheel alignment shop",
        "user_goal": "Check Bridgestone Sturdo 195/55 R16 tyre stock right now and get per-tyre installed price in ₹ including wheel alignment",
        "preferred_tone": VocalTone.CONVERSATIONAL_HINGLISH.value,
    },
    {
        "id": "medicine_indiranagar",
        "place_category": PlaceCategory.PHARMACY.value,
        "badge": "💊 Pharmacies · Bengaluru",
        "title": "Augmentin 625 Duo across Open Chemists",
        "city": "Bengaluru",
        "locality": "Indiranagar",
        "lookup_query": "pharmacy medical store chemist open now",
        "user_goal": "Check which open medical store has Augmentin 625 Duo (strip of 10) in stock right now and confirm the cash price in ₹",
        "preferred_tone": VocalTone.CONVERSATIONAL_HINGLISH.value,
    },
]

_AI_BOT_SIGNATURES: list[tuple[str, str]] = [
    ("oai-searchbot", "ChatGPT SearchBot (OpenAI)"),
    ("chatgpt-user", "ChatGPT Live User Fetch (OpenAI)"),
    ("gptbot", "GPTBot Crawler (OpenAI)"),
    ("claude-searchbot", "Claude SearchBot (Anthropic)"),
    ("claude-user", "Claude Live User Fetch (Anthropic)"),
    ("claudebot", "ClaudeBot (Anthropic)"),
    ("anthropic-ai", "Anthropic Crawler"),
    ("google-extended", "Gemini / Google-Extended"),
    ("googleother", "GoogleOther / AI Grounding"),
    ("googlebot", "Googlebot Search"),
    ("perplexitybot", "PerplexityBot"),
    ("perplexity-user", "Perplexity Live Fetch"),
    ("applebot", "Applebot / Siri"),
    ("bingbot", "Bingbot / Copilot"),
    ("amazonbot", "Amazonbot / Alexa AI"),
    ("duckassistbot", "DuckAssistBot"),
    ("youbot", "You.com AI Bot"),
    ("rnwy-toollistread", "RNWY MCP Directory Indexer"),
    ("mcpbeat", "MCPBeat Directory Monitor"),
    ("sentineloracle", "Glimind SentinelOracle MCP Probe"),
    ("alalanet-prober", "AlalaNet MCP Directory Prober"),
    ("glama", "Glama MCP Scanner"),
    ("smithery", "Smithery MCP Scanner"),
    ("pulsemcp", "PulseMCP Crawler"),
    ("mcp-directory-scanner", "MCP Directory Audit Scanner"),
]

_REFERRAL_SIGNATURES: list[tuple[str, str]] = [
    ("chatgpt.com", "ChatGPT Referral"),
    ("chat.openai.com", "ChatGPT Referral"),
    ("claude.ai", "Claude Referral"),
    ("gemini.google.com", "Google Gemini Referral"),
    ("aistudio.google.com", "Google AI Studio Referral"),
    ("perplexity.ai", "Perplexity Referral"),
    ("copilot.microsoft.com", "Microsoft Copilot Referral"),
    ("google.", "Google Organic Search"),
    ("bing.com", "Bing Organic Search"),
    ("reddit.com", "Reddit Organic"),
    ("news.ycombinator.com", "Hacker News Organic"),
    ("x.com", "X / Twitter"),
    ("t.co", "X / Twitter"),
    ("linkedin.com", "LinkedIn"),
]


class PaymentConfigUpdateRequest(BaseModel):
    gate_mode: str | None = Field(default=None)
    star_gate_mode: str | None = Field(default=None)
    free_daily_calls_per_client: int | None = Field(default=None, ge=1, le=500)
    global_daily_free_pstn_legs: int | None = Field(default=None, ge=1, le=2000)
    stripe_secret_key: str | None = Field(default=None)
    stripe_checkout_url: str | None = Field(default=None)


class TrafficAnalyticsTracker:
    """Server-side tracker for AI Search/Grounding Bots, AI Chat Referrals, MCP/A2A Tool Calls, & Web Quests."""

    def __init__(self, max_events: int = 80) -> None:
        self.ai_bot_hits: Counter[str] = Counter()
        self.ai_referrals: Counter[str] = Counter()
        self.protocol_calls: Counter[str] = Counter()
        self.page_views: Counter[str] = Counter()
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=max_events)

    def record_request(self, request: Request, status_code: int) -> None:
        path = request.url.path
        if path in ("/favicon.ico", "/api/health") or path.startswith("/api/admin/"):
            return

        ua = (request.headers.get("user-agent") or "").strip()
        ua_low = ua.lower()
        ref = (request.headers.get("referer") or "").strip()
        ref_low = ref.lower()
        utm_src = (request.query_params.get("utm_source") or "").strip().lower()

        bot_label: str | None = None
        for needle, label in _AI_BOT_SIGNATURES:
            if needle in ua_low:
                bot_label = label
                self.ai_bot_hits[label] += 1
                break

        ref_label: str | None = None
        combined_ref = f"{utm_src} {ref_low}"
        for needle, label in _REFERRAL_SIGNATURES:
            if needle in combined_ref:
                ref_label = label
                self.ai_referrals[label] += 1
                break

        proto_label: str | None = None
        if path == "/mcp" and request.method == "POST":
            proto_label = "MCP Connector Call (POST /mcp)"
        elif path in ("/sse", "/messages"):
            proto_label = "MCP Legacy SSE Transport (/sse)"
        elif path == "/a2a" and request.method == "POST":
            proto_label = "Google A2A Task Call (POST /a2a)"
        elif path == "/api/v1/scout" and request.method == "POST":
            proto_label = "OpenAPI / x402 / Stripe Scout (POST /api/v1/scout)"
        elif path == "/openapi-actions.json":
            proto_label = "ChatGPT Actions Schema Fetch (/openapi-actions.json)"
        elif path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            proto_label = "A2A Agent Card Discovery"
        elif path in ("/.well-known/mcp.json", "/.well-known/mcp", "/.well-known/mcp/server-card.json", "/mcp/server-card", "/server.json"):
            proto_label = "MCP Registry / Server Card Discovery"
        elif path in ("/.well-known/ai-catalog.json", "/.well-known/ai-catalog"):
            proto_label = "SEP-2127 AI Catalog Discovery"
        elif path in ("/.well-known/agents.json", "/agents.json"):
            proto_label = "Multi-Agent agents.json Discovery"
        elif path in ("/llms.txt", "/llms-full.txt", "/.well-known/llms.txt"):
            proto_label = "GEO /llms.txt Crawl"
        elif path in ("/.well-known/x402", "/api/v1/x402/discovery"):
            proto_label = "x402 / Bazaar Catalog Probe"

        if proto_label:
            self.protocol_calls[proto_label] += 1

        if request.method == "GET" and path in ("/", "/developers"):
            self.page_views[path] += 1
        elif request.method == "POST" and path == "/api/quests":
            self.page_views["human_quests_started"] += 1

        if bot_label or ref_label or proto_label or path in ("/", "/developers"):
            now_ev = time.time()
            self.purge_expired(now_ev)
            safe_path = normalize_endpoint_path(path)
            ev = {
                "epoch": now_ev,
                "ts": datetime.datetime.fromtimestamp(now_ev, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "method": request.method,
                "path": safe_path,
                "status": status_code,
                "bot": bot_label,
                "referral": ref_label,
                "protocol": proto_label,
                "ua_short": ua[:96] if ua else "unknown",
            }
            self.recent_events.appendleft(ev)
            if bot_label or ref_label or proto_label:
                log_ev = {k: v for k, v in ev.items() if k != "epoch"}
                logger.info("RING_ORGANIC_TELEMETRY %s", json.dumps(log_ev, separators=(",", ":")))

    def purge_expired(self, now_epoch: float | None = None, ttl_seconds: float = 86400.0) -> int:
        now = float(time.time() if now_epoch is None else now_epoch)
        removed = 0
        while self.recent_events and (now - float(self.recent_events[-1].get("epoch") or now)) >= ttl_seconds:
            self.recent_events.pop()
            removed += 1
        return removed

    def snapshot(self) -> dict[str, Any]:
        self.purge_expired()
        return {
            "totals": {
                "ai_bot_crawls": sum(self.ai_bot_hits.values()),
                "organic_and_ai_referrals": sum(self.ai_referrals.values()),
                "agent_protocol_calls": sum(self.protocol_calls.values()),
                "consumer_home_views": self.page_views.get("/", 0),
                "developer_hub_views": self.page_views.get("/developers", 0),
                "human_quests_started": self.page_views.get("human_quests_started", 0),
            },
            "ai_bots_by_agent": dict(self.ai_bot_hits.most_common(15)),
            "referrals_by_source": dict(self.ai_referrals.most_common(15)),
            "protocol_activity": dict(self.protocol_calls.most_common(15)),
            "recent_events": [
                {k: v for k, v in ev.items() if k != "epoch"} for ev in list(self.recent_events)[:35]
            ],
        }


_PUBLIC_PSTN_BUCKET: dict[str, list[float]] = defaultdict(list)


def _expected_admin_passcode() -> str:
    return (
        get_secret("RINGSCOUT_ADMIN_PASSCODE")
        or os.environ.get("RINGSCOUT_PASSCODE", "").strip()
    )


def _expected_passcode() -> str:
    """Consumer-surface passcode: empty (open to everyone) unless legacy RINGSCOUT_PASSCODE is set without public override."""
    if os.environ.get("RINGSCOUT_PUBLIC_CONSUMER_ACCESS", "").strip() == "1":
        return ""
    return os.environ.get("RINGSCOUT_PASSCODE", "").strip()


def _check_admin_passcode_value(provided: str | None) -> bool:
    expected = _expected_admin_passcode()
    if not expected:
        # Fail CLOSED on all live servers & public consumer deployments; allow unset only in legacy hermetic pytest
        if (
            os.environ.get("PYTEST_CURRENT_TEST")
            and os.environ.get("RINGSCOUT_PUBLIC_CONSUMER_ACCESS", "").strip() != "1"
        ):
            return True
        return False
    if not provided:
        return False
    return hmac.compare_digest(expected, provided.strip())


def _check_passcode_value(provided: str | None) -> bool:
    expected = _expected_passcode()
    if not expected:
        return True
    if _check_admin_passcode_value(provided):
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected, provided.strip())


async def verify_admin_passcode(request: Request) -> None:
    """Protect operator & carrier credential endpoints with RINGSCOUT_ADMIN_PASSCODE (Secret Manager) or RINGSCOUT_PASSCODE."""
    provided = request.headers.get("x-ringscout-passcode") or request.query_params.get(
        "passcode"
    )
    if not _check_admin_passcode_value(provided):
        raise HTTPException(
            status_code=401,
            detail="Admin passcode required (set via #passcode=... or X-RingScout-Passcode).",
        )


async def verify_user_passcode(request: Request) -> None:
    """Consumer endpoints: open to everyone by default, or guarded if legacy RINGSCOUT_PASSCODE is set."""
    provided = request.headers.get("x-ringscout-passcode") or request.query_params.get(
        "passcode"
    )
    if not _check_passcode_value(provided):
        raise HTTPException(
            status_code=401,
            detail="Valid passcode required (set via #passcode=... or X-RingScout-Passcode).",
        )


def _mask_phone_for_public_dashboard(phone_display: str) -> str:
    """Mask middle digits on public dashboard (`+91 22 6668 ••34`) while preserving prefix & last 2 digits."""
    digits = [c for c in phone_display if c.isdigit()]
    if len(digits) < 6:
        return phone_display
    out = list(phone_display)
    digit_seen = 0
    total_d = len(digits)
    for i, ch in enumerate(out):
        if ch.isdigit():
            digit_seen += 1
            if total_d - 4 <= digit_seen <= total_d - 2:
                out[i] = "•"
    return "".join(out)


def _client_fingerprint(request: Request) -> str:
    # Use rightmost GFE-appended X-Forwarded-For IP (or socket peer IP), never client-spoofable leftmost XFF or User-Agent
    xff_raw = (request.headers.get("x-forwarded-for") or "").strip()
    xff_rightmost = xff_raw.split(",")[-1].strip() if xff_raw else ""
    ip = xff_rightmost or (request.client.host if request.client else "local")
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:14]


def _derive_public_base_url(request: Request, adapter: LiveIndiaPstnAdapter) -> str:
    cfg = adapter.resolve_config()
    if cfg and cfg.public_base_url and cfg.public_base_url.strip():
        return cfg.public_base_url.strip().rstrip("/")
    env_url = (
        os.environ.get("PUBLIC_BASE_URL", "").strip()
        or os.environ.get("PUBLIC_WSS_BASE_URL", "").strip()
    )
    if env_url:
        return env_url.rstrip("/")
    proto = (
        request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    ).split(",")[0].strip()
    host = (
        request.headers.get("host")
        or request.url.netloc
        or "localhost:8080"
    ).split(",")[0].strip()
    return f"{proto}://{host}"


def _is_publicly_reachable_base(base_url: str) -> bool:
    lowered = base_url.lower()
    private_markers = (
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        ".c.googlers.com",
        ".corp.google.com",
    )
    return lowered.startswith(("https://", "wss://")) and not any(
        m in lowered for m in private_markers
    )


async def _parse_webhook_body(request: Request) -> dict[str, Any]:
    """Parse JSON or form-encoded carrier webhook payloads using pure stdlib."""
    raw = await request.body()
    if not raw:
        return {}
    ctype = (request.headers.get("content-type") or "").lower()
    text = raw.decode("utf-8", errors="replace")
    if "application/json" in ctype or text.lstrip().startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return dict(parse_qsl(text, keep_blank_values=True))


CALL_DATA_RETENTION_TTL_SEC = int(os.environ.get("RINGSCOUT_RETENTION_TTL_SECONDS", "86400"))
OPERATIONAL_LOG_RETENTION_DAYS = 30


class BoundedAudioStore(OrderedDict[tuple[str, str], bytes]):
    """Bounded in-memory WAV store with hard 24-hour TTL auto-expiry and max-clip cap."""

    def __init__(
        self, max_entries: int = 64, ttl_seconds: float = float(CALL_DATA_RETENTION_TTL_SEC)
    ) -> None:
        super().__init__()
        self._max_entries = max_entries
        self._ttl_seconds = float(ttl_seconds)
        self._created_at: dict[tuple[str, str], float] = {}

    def purge_expired(self, now_epoch: float | None = None) -> int:
        now = float(time.time() if now_epoch is None else now_epoch)
        expired_keys = [
            k for k, ts in list(self._created_at.items()) if (now - ts) >= self._ttl_seconds
        ]
        for k in expired_keys:
            self._created_at.pop(k, None)
            super().pop(k, None)
        return len(expired_keys)

    def __setitem__(self, key: tuple[str, str], value: bytes) -> None:
        self.purge_expired()
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        self._created_at[key] = time.time()
        while len(self) > self._max_entries:
            evicted_key, _ = self.popitem(last=False)
            self._created_at.pop(evicted_key, None)

    def get(self, key: tuple[str, str], default: Any = None) -> Any:  # type: ignore[override]
        self.purge_expired()
        return super().get(key, default)

    def __getitem__(self, key: tuple[str, str]) -> bytes:
        self.purge_expired()
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self.purge_expired()
        return super().__contains__(key)

    def pop(self, key: tuple[str, str], *args: Any) -> Any:  # type: ignore[override]
        self._created_at.pop(key, None)
        return super().pop(key, *args)


def _public_safe_quote_dict(quote_dump: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip free-text conversational snippets from public aggregate quote views."""
    if not quote_dump:
        return None
    return {
        "in_stock": quote_dump.get("in_stock"),
        "availability_label": quote_dump.get("availability_label"),
        "price_inr": quote_dump.get("price_inr"),
        "eta_or_slot": quote_dump.get("eta_or_slot"),
        "notes": "",
        "key_insights": [],
    }


def _quest_payload(
    quest: QuestSession,
    *,
    include_token: bool = False,
    mask_phones: bool = False,
) -> dict[str, Any]:
    q_dump = quest.model_dump()
    ranked_dump = [c.model_dump() for c in quest.ranked_quotes()]
    if mask_phones:
        q_dump["caller_name"] = ""
        q_dump["user_goal"] = f"{quest.place_category.value} availability check"
        for s in q_dump.get("shops", []):
            s["phone_e164"] = ""
            s["phone_display"] = _mask_phone_for_public_dashboard(str(s.get("phone_display") or ""))
        for c in (q_dump.get("calls") or {}).values():
            if "shop" in c:
                c["shop"]["phone_e164"] = ""
                c["shop"]["phone_display"] = _mask_phone_for_public_dashboard(
                    str(c["shop"].get("phone_display") or "")
                )
            c["transcript"] = []
            c["audio_url"] = None
            c["quote"] = _public_safe_quote_dict(c.get("quote"))
        for c in ranked_dump:
            if "shop" in c:
                c["shop"]["phone_e164"] = ""
                c["shop"]["phone_display"] = _mask_phone_for_public_dashboard(
                    str(c["shop"].get("phone_display") or "")
                )
            c["transcript"] = []
            c["audio_url"] = None
            c["quote"] = _public_safe_quote_dict(c.get("quote"))
    else:
        for c in (q_dump.get("calls") or {}).values():
            raw_aud = c.get("audio_url")
            shop_id = (c.get("shop") or {}).get("shop_id") or ""
            if raw_aud and "sig=" not in str(raw_aud) and shop_id:
                sig = LiveIndiaPstnAdapter.sign_pstn_token("audio", quest.quest_id, shop_id)
                c["audio_url"] = f"{raw_aud}?sig={sig}"
        for c in ranked_dump:
            raw_aud = c.get("audio_url")
            shop_id = (c.get("shop") or {}).get("shop_id") or ""
            if raw_aud and "sig=" not in str(raw_aud) and shop_id:
                sig = LiveIndiaPstnAdapter.sign_pstn_token("audio", quest.quest_id, shop_id)
                c["audio_url"] = f"{raw_aud}?sig={sig}"
    out: dict[str, Any] = {
        "quest": q_dump,
        "ranked_quotes": ranked_dump,
    }
    if include_token:
        out["quest_token"] = LiveIndiaPstnAdapter.sign_pstn_token(
            "quest_owner", quest.quest_id, "owner"
        )
    return out


def _is_quest_owner_or_admin(request: Request, quest_id: str) -> bool:
    provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get(
        "passcode"
    )
    if _check_admin_passcode_value(provided_pass):
        return True
    qtok = request.headers.get("x-quest-token") or request.query_params.get("token")
    if LiveIndiaPstnAdapter.verify_pstn_token("quest_owner", quest_id, "owner", qtok):
        return True
    return False


def _build_llms_txt(base_url: str, full: bool = False) -> str:
    clean = base_url.rstrip("/")
    core = f"""# RingScout — Real-time local voice checks

> RingScout finds local businesses on Google Maps and calls them directly to verify what's happening right now. Instead of relying only on potentially outdated listings, RingScout uses AI-powered voice calls to contact real business phone numbers in India, ask questions in Indian English, Hindi, or Hinglish, and return verified responses, prices in ₹, key details, and recorded call audio when available (up to five businesses in parallel).

## What RingScout Can Check
- **Restaurants**: Whether a table is available and how long the wait is
- **Hotels**: Whether rooms, early check-in, or a better direct tariff are available
- **Salons & Spas**: Whether a same-day appointment is open
- **Clinics & Diagnostic Centres**: Whether a slot is available and what it charges in ₹
- **Petrol Pumps**: Whether a particular fuel (e.g. XP95) or service is available
- **Pharmacies & Auto Shops**: Whether the item or service you need is in stock

## Coverage & Important Limitations
- **Coverage**: India, with supported local business categories and locations.
- **Important limitations**: Results depend on businesses being reachable and answering. Availability and prices can change after a call. RingScout provides information; it does not itself make reservations, purchases, or appointments.

## Key Links & Machine-Readable Endpoints
- [Consumer Website (Human-to-Agent UI)]({clean}/): Select a Google Maps location category, enter area + question, and dispatch parallel live calls.
- [AI Connectors & Developer Hub]({clean}/developers): 1-click setup for Claude, Google Gemini, ChatGPT Custom GPTs, Google A2A, Stripe MPP, and x402.
- [Remote MCP Server (Streamable HTTP)]({clean}/mcp): Add `{clean}/mcp` in Claude (`Settings -> Connectors`), Gemini (`Connected Apps`), ChatGPT Developer Mode, or Cursor.
- [Legacy MCP SSE Transport]({clean}/sse): Backward-compatible `2024-11-05` HTTP+SSE transport (`GET /sse` + `POST /messages`) for older MCP clients.
- [SEP-2127 MCP Server Card]({clean}/.well-known/mcp/server-card.json): Auto-discovery Server Card (`application/mcp-server-card+json`) and `{clean}/.well-known/mcp.json` listing tools (`scout_places_on_google_maps`, `get_ringscout_pricing_and_status`, `scout_india_local_businesses`).
- [Domain AI Catalog (SEP-2127)]({clean}/.well-known/ai-catalog.json): Domain-level AI catalog indexing MCP, A2A, OpenAPI Actions, and `llms.txt`.
- [Official MCP Registry `server.json`]({clean}/server.json): Validated `2025-12-11` `registry.modelcontextprotocol.io` remote manifest.
- [ChatGPT Custom GPT OpenAPI 3.1 Schema]({clean}/openapi-actions.json): Import directly into ChatGPT `Configure -> Actions -> Import from URL`.
- [Official Google A2A v1.0 Agent Card]({clean}/.well-known/agent-card.json): JSON-RPC 2.0 `message/send` endpoint at `{clean}/a2a`.
- [Open-Source GitHub Repository](https://github.com/manavaga/ringscout): Source code, `server.json`, MCP setup, and MIT license (star to unlock bonus free live calls).
- [Privacy & AI Call Disclosure Policy]({clean}/privacy): Data handling and Google Maps business provenance policy for ChatGPT Actions, Claude, and Gemini connectors.
- [Payment Protocols (Phase A Open + Phase B Stripe MPP & x402)]({clean}/api/v1/pricing): Works out-of-the-box with zero payment wall in Phase A; supports Stripe Machine Payments Protocol (`WWW-Authenticate: Payment` / Shared Payment Tokens `spt_...`) and `x402` v2 Base USDC (`0.75 USDC`) in Phase B.
"""
    if not full:
        return core
    extra = f"""
## Example Tool Call (`POST {clean}/api/v1/scout`)
```json
{{
  "place_category": "hotel",
  "city": "Mumbai",
  "locality": "Bandra West",
  "item_question": "Deluxe room tonight, 10 AM early check-in availability, and direct front-desk rate in ₹",
  "caller_name": "Aarav",
  "max_shops": 3,
  "preferred_tone": "polite_indian_english"
}}
```
"""
    return core + extra


class QuestManager:
    """In-memory state, Google Maps phone provenance, SSE fan-out, panic-stop, and WAV store."""

    def __init__(
        self,
        synthesize_audio: bool = True,
        require_provenance: bool = True,
        retention_ttl_seconds: float = float(CALL_DATA_RETENTION_TTL_SEC),
        max_quests: int = 256,
    ) -> None:
        self.quests: dict[str, QuestSession] = {}
        self.stop_events: dict[str, asyncio.Event] = {}
        self.subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.retention_ttl_seconds = float(retention_ttl_seconds)
        self.max_quests = max_quests
        self.audio_store = BoundedAudioStore(
            max_entries=64, ttl_seconds=self.retention_ttl_seconds
        )
        self.verified_google_phones: set[str] = set()
        self.require_provenance = require_provenance
        self.swarm_adapter = HinglishSwarmAdapter(
            audio_store=self.audio_store,
            synthesize_audio=synthesize_audio,
        )
        self.mic_adapter = BrowserMicAdapter(audio_store=self.audio_store)
        self.pstn_adapter = LiveIndiaPstnAdapter(audio_store=self.audio_store)
        persisted_cfg = load_local_carrier_config()
        if persisted_cfg is not None:
            self.pstn_adapter.set_runtime_config(persisted_cfg)
        self.twilio_adapter = self.pstn_adapter

    def purge_expired_data(self, now_epoch: float | None = None) -> dict[str, int]:
        """Automatically purge quests, transcripts, caller names, A2A tasks, and WAV clips older than 24 hours."""
        now = float(time.time() if now_epoch is None else now_epoch)
        expired_audio = self.audio_store.purge_expired(now)
        expired_a2a = A2A_TASK_STORE.purge_expired(now)
        expired_quests = 0
        for qid, q in list(self.quests.items()):
            if (now - float(q.created_at_epoch)) >= self.retention_ttl_seconds:
                self._evict_quest(qid, q)
                expired_quests += 1
        while len(self.quests) > self.max_quests:
            oldest_qid = next(iter(self.quests))
            oldest_q = self.quests[oldest_qid]
            self._evict_quest(oldest_qid, oldest_q)
            expired_quests += 1
        return {
            "expired_quests": expired_quests,
            "expired_audio_clips": expired_audio,
            "expired_a2a_tasks": expired_a2a,
        }

    def _evict_quest(self, qid: str, q: QuestSession) -> None:
        for sid in list(q.calls.keys()):
            self.audio_store.pop((qid, sid), None)
        A2A_TASK_STORE.pop_by_quest_id(qid)
        ev = self.stop_events.pop(qid, None)
        if ev:
            ev.set()
        self.subscribers.pop(qid, None)
        self.quests.pop(qid, None)

    def register_discovered_shops(self, resp: DiscoverResponse) -> None:
        for shop in resp.shops:
            self.verified_google_phones.add(shop.phone_e164)

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        quest_id = str(payload.get("quest_id") or "")
        queues = self.subscribers.get(quest_id)
        if not queues:
            return
        envelope = {"event": event_type, "data": payload}
        for q in list(queues):
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                pass

    def create_quest(self, req: QuestCreateRequest) -> QuestSession:
        self.purge_expired_data()
        quest_id = f"q_{uuid.uuid4().hex[:10]}"
        validated_shops = []
        calls: dict[str, ShopCallState] = {}

        for idx, shop in enumerate(req.shops, start=1):
            e164, display, reason = normalize_indian_phone(shop.phone_e164)
            if reason != "ok" or not e164 or not display:
                raise HTTPException(
                    status_code=400,
                    detail=f"Blocked non-compliant or toll-free phone number for '{shop.name}': {shop.phone_e164}",
                )
            if self.require_provenance and e164 not in self.verified_google_phones:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Blocked unverified personal phone number '{e164}'. "
                        "Only Google Maps business listings discovered via /api/discover may be dialed in multi-shop quests "
                        "(use 'Test Call My Own +91 Number First' for a single self-test call)."
                    ),
                )
            safe_id = shop.shop_id if _SAFE_SHOP_ID_RE.match(shop.shop_id) else f"shop_{idx}"
            safe_maps = (
                shop.maps_uri
                if (shop.maps_uri and shop.maps_uri.startswith(("https://", "http://")))
                else None
            )
            clean_shop = shop.model_copy(
                update={
                    "shop_id": safe_id,
                    "phone_e164": e164,
                    "phone_display": display,
                    "maps_uri": safe_maps,
                }
            )
            validated_shops.append(clean_shop)
            calls[clean_shop.shop_id] = ShopCallState(
                shop=clean_shop,
                mode=req.mode,
                status=CallStatus.QUEUED,
                outcome=CallOutcome.PENDING,
                vocal_tone=req.preferred_tone,
            )

        quest = QuestSession(
            quest_id=quest_id,
            user_goal=req.user_goal,
            place_category=req.place_category,
            caller_name=req.caller_name,
            city=req.city,
            locality=req.locality,
            preferred_tone=req.preferred_tone,
            voice_name=req.voice_name,
            mode=req.mode,
            origin_channel=req.origin_channel,
            caller_agent=req.caller_agent,
            shops=validated_shops,
            calls=calls,
        )
        self.quests[quest_id] = quest
        self.stop_events[quest_id] = asyncio.Event()
        self.subscribers[quest_id] = set()
        return quest

    def create_self_test_quest(self, req: SelfTestCallRequest) -> QuestSession:
        """Create an isolated 1-call LIVE_PSTN quest to ring the operator's own +91 mobile."""
        self.purge_expired_data()
        e164, display, reason = normalize_indian_phone(req.my_phone)
        if reason != "ok" or not e164 or not display:
            raise HTTPException(
                status_code=400,
                detail="Enter a valid Indian +91 mobile or STD landline number (1800 toll-free numbers are blocked).",
            )
        quest_id = f"q_test_{uuid.uuid4().hex[:8]}"
        shop = IndianShopTarget(
            shop_id="self_test_1",
            name="My +91 Phone (Dry-Run Voice Test)",
            phone_e164=e164,
            phone_display=display,
            address="Linking Road, Bandra West",
            locality="Self-Test",
            city="India",
            open_status="SELF-TEST READY",
            is_open_now=True,
            category="Dry-Run Verification",
        )
        calls = {
            shop.shop_id: ShopCallState(
                shop=shop,
                mode=CallMode.LIVE_PSTN,
                status=CallStatus.QUEUED,
                outcome=CallOutcome.PENDING,
                vocal_tone=req.preferred_tone,
            )
        }
        quest = QuestSession(
            quest_id=quest_id,
            user_goal=req.user_goal,
            place_category=req.place_category,
            caller_name=req.caller_name,
            city="India",
            locality="Self-Test",
            preferred_tone=req.preferred_tone,
            voice_name=req.voice_name,
            mode=CallMode.LIVE_PSTN,
            origin_channel="admin_self_test",
            caller_agent="Operator Self-Test",
            shops=[shop],
            calls=calls,
        )
        self.quests[quest_id] = quest
        self.stop_events[quest_id] = asyncio.Event()
        self.subscribers[quest_id] = set()
        return quest

    async def stop_quest(self, quest_id: str) -> QuestSession:
        quest = self.quests.get(quest_id)
        if not quest:
            raise HTTPException(status_code=404, detail="Quest not found")
        ev = self.stop_events.get(quest_id)
        if ev:
            ev.set()
        quest.status = "stopped"
        for call in quest.calls.values():
            self.pstn_adapter._release_prewarm_slot(quest_id, call.shop.shop_id)
            if call.twilio_call_sid:
                asyncio.create_task(
                    self.pstn_adapter.terminate_india_call(
                        call.twilio_call_sid, call.carrier_provider
                    )
                )
            if call.status in (
                CallStatus.QUEUED,
                CallStatus.RINGING,
                CallStatus.IN_PROGRESS,
                CallStatus.IVR_MENU,
            ):
                call.status = CallStatus.STOPPED
                call.outcome = CallOutcome.STOPPED_BY_USER
                self.publish(
                    "call_update",
                    {
                        "quest_id": quest_id,
                        "shop_id": call.shop.shop_id,
                        "call": call.model_dump(),
                    },
                )
        self.publish("quest_complete", {"quest_id": quest_id, "status": "stopped"})
        return quest


def create_app(synthesize_audio: bool = True, require_provenance: bool = True) -> FastAPI:
    app = FastAPI(
        title="RingScout — Real-time local voice checks",
        version="3.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "HEAD", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[
            "WWW-Authenticate",
            "PAYMENT-REQUIRED",
            "PAYMENT-RESPONSE",
            "Payment-Receipt",
            "Mcp-Session-Id",
            "MCP-Protocol-Version",
            "Link",
        ],
    )
    manager = QuestManager(
        synthesize_audio=synthesize_audio,
        require_provenance=require_provenance,
    )
    analytics = TrafficAnalyticsTracker()
    endpoint_tracker = EndpointRequestTracker(project_id=GCP_PROJECT_ID, service_name="ringscout")
    app.state.manager = manager
    app.state.analytics = analytics
    app.state.endpoint_tracker = endpoint_tracker

    _RFC8288_AGENT_LINKS = (
        '</.well-known/ai-catalog.json>; rel="ai-catalog"; type="application/ai-catalog+json", '
        '</.well-known/mcp/server-card.json>; rel="mcp-server-card"; type="application/mcp-server-card+json", '
        '</.well-known/agent-card.json>; rel="agent-card"; type="application/a2a-agent-card+json", '
        '</.well-known/agents.json>; rel="agents"; type="application/json", '
        '</openapi-actions.json>; rel="service-desc"; type="application/vnd.oai.openapi+json", '
        '</llms.txt>; rel="llms-txt"; type="text/markdown", '
        '<https://github.com/manavaga/ringscout>; rel="vcs-git"'
    )

    @app.middleware("http")
    async def track_organic_and_ai_traffic(request: Request, call_next: Any) -> Response:
        t0 = time.perf_counter()
        response: Response = await call_next(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if request.url.path in ("/", "/developers", "/dashboard", "/tracker", "/ping", "/api/health"):
            response.headers.setdefault("Link", _RFC8288_AGENT_LINKS)
        try:
            analytics.record_request(request, response.status_code)
            if request.url.path != "/api/tracker/endpoints":
                endpoint_tracker.record_http_hit(
                    method=request.method,
                    raw_path=request.url.path,
                    status_code=response.status_code,
                    user_agent=request.headers.get("user-agent") or "",
                    latency_ms=elapsed_ms,
                )
        except Exception:
            pass
        return response

    async def _warm_call_plan_async(quest: QuestSession) -> None:
        """Refine heuristic call plan in the background during PSTN ring setup."""
        try:
            async with asyncio.timeout(3.8):
                quest.call_plan = await compile_conversational_call_plan(
                    quest.user_goal, quest.preferred_tone, quest.place_category
                )
        except Exception:
            pass

    async def _staggered_pstn_dial(
        delay_sec: float,
        quest: QuestSession,
        shop: IndianShopTarget,
        base_url: str,
        stop_ev: asyncio.Event,
    ) -> None:
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)
        if stop_ev.is_set() or quest.status != "running":
            return
        await manager.pstn_adapter.place_india_call(
            quest=quest,
            shop=shop,
            public_base_url=base_url,
            publish=manager.publish,
        )

    def _validate_single_cli_number(raw_num: str, provider: CarrierProvider) -> str:
        in_e164, _, reason = normalize_indian_phone(raw_num)
        if reason == "toll_free":
            raise HTTPException(
                status_code=400,
                detail="1800 / 1860 toll-free numbers cannot be used as outbound caller ID.",
            )
        if in_e164:
            return in_e164
        compact = re.sub(r"[\s\-().]", "", raw_num)
        if provider == CarrierProvider.TWILIO and _GENERIC_E164_RE.match(compact):
            return compact
        raise HTTPException(
            status_code=400,
            detail=f"Caller ID ('{raw_num}') must be a valid Indian +91 DID (or E.164 +number for Twilio).",
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    # --- Public GEO & AI Discovery Files (`/robots.txt`, `/llms.txt`, `/sitemap.xml`) ---

    @app.get("/robots.txt", include_in_schema=False)
    async def robots_txt(request: Request) -> PlainTextResponse:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        body = (
            "User-agent: OAI-SearchBot\nAllow: /\n\n"
            "User-agent: ChatGPT-User\nAllow: /\n\n"
            "User-agent: GPTBot\nAllow: /\n\n"
            "User-agent: Claude-SearchBot\nAllow: /\n\n"
            "User-agent: Claude-User\nAllow: /\n\n"
            "User-agent: ClaudeBot\nAllow: /\n\n"
            "User-agent: Google-Extended\nAllow: /\n\n"
            "User-agent: Googlebot\nAllow: /\n\n"
            "User-agent: PerplexityBot\nAllow: /\n\n"
            "User-agent: Applebot\nAllow: /\n\n"
            "User-agent: Amazonbot\nAllow: /\n\n"
            "User-agent: DuckAssistBot\nAllow: /\n\n"
            "User-agent: YouBot\nAllow: /\n\n"
            "User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/admin/\nDisallow: /api/pstn/\n\n"
            f"Sitemap: {base_url}/sitemap.xml\n"
        )
        return PlainTextResponse(content=body)

    @app.get("/llms.txt", include_in_schema=False)
    @app.get("/.well-known/llms.txt", include_in_schema=False)
    async def llms_txt(request: Request) -> Response:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return Response(
            content=_build_llms_txt(base_url, full=False),
            media_type="text/markdown; charset=utf-8",
        )

    @app.get("/llms-full.txt", include_in_schema=False)
    async def llms_full_txt(request: Request) -> Response:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return Response(
            content=_build_llms_txt(base_url, full=True),
            media_type="text/markdown; charset=utf-8",
        )

    @app.get("/.well-known/security.txt", include_in_schema=False)
    async def security_txt(request: Request) -> PlainTextResponse:
        base_url = _derive_public_base_url(request, manager.pstn_adapter).rstrip("/")
        body = (
            "Contact: https://github.com/manavaga/ringscout/issues\n"
            "Expires: 2027-12-31T23:59:59.000Z\n"
            "Preferred-Languages: en\n"
            f"Canonical: {base_url}/.well-known/security.txt\n"
            f"Policy: {base_url}/developers\n"
        )
        return PlainTextResponse(content=body)

    @app.get("/privacy", include_in_schema=False)
    async def privacy_policy_page(request: Request) -> HTMLResponse:
        """Public Privacy, Data Retention & AI Voice Disclosure Policy (Governing Law: India)."""
        base_url = _derive_public_base_url(request, manager.pstn_adapter).rstrip("/")
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Privacy &amp; AI Call Policy — RingScout</title>
  <link rel="canonical" href="{base_url}/privacy" />
  <style>
    body {{ background:#090c10; color:#f1f5f9; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; line-height:1.65; margin:0; padding:44px 20px; }}
    main {{ max-width:760px; margin:0 auto; background:#11161d; border:1px solid rgba(255,255,255,0.09); border-radius:14px; padding:32px; }}
    h1 {{ margin-top:0; font-size:1.6rem; }}
    h2 {{ font-size:1.08rem; color:#38bdf8; margin-top:24px; }}
    a {{ color:#10b981; }}
    p, li {{ color:#cbd5e1; font-size:0.95rem; }}
  </style>
</head>
<body>
  <main>
    <h1>RingScout Privacy &amp; Responsible Voice Calling Policy</h1>
    <p><strong>Last Updated:</strong> November 26, 2026 &middot; <strong>Governing Law:</strong> India &middot; <strong>Repository &amp; Full Policies:</strong> <a href="https://github.com/manavaga/ringscout">github.com/manavaga/ringscout</a></p>
    <h2>1. Proactive AI &amp; Recording Disclosure</h2>
    <p>On every outbound call, RingScout proactively identifies itself upfront as the <strong>RingScout AI assistant</strong> calling on a <strong>recorded line</strong> (never impersonating a human person). Inbound recipient audio recording begins only after this disclosure starts, and if a recipient objects to recording or automated calls, RingScout ends the call immediately and discards any audio buffer.</p>
    <h2>2. Public Business Provenance</h2>
    <p>RingScout helps users and AI assistants (ChatGPT, Claude, Google Gemini, MCP, and Google A2A clients) check real-time availability, wait times, and rates at open public businesses in India listed on Google Maps. RingScout blocks dialing unlisted personal phone numbers and 1800 toll-free numbers.</p>
    <h2>3. Access Controls &amp; Public Dashboard Privacy</h2>
    <p>Public dashboard and analytics views expose only non-sensitive aggregate telemetry and masked business phone numbers. Call transcripts, caller names, and signed 24kHz WAV audio clips are restricted strictly to the session/caller that initiated the request (via capability token) and the service administrator.</p>
    <h2>4. Predictable Retention &amp; Deletion Schedule</h2>
    <ul>
      <li><strong>Call Audio, Transcripts &amp; Quest Records (24-Hour Expiry):</strong> Held only in bounded application memory (max 64 audio clips / 256 quests) and automatically purged after <strong>24 hours</strong> (or earlier on instance restart or recipient opt-out).</li>
      <li><strong>Operational &amp; Request Logs (30-Day Retention):</strong> In-memory request counters expire within 24 hours; Google Cloud Run request logs in the verified <code>_Default</code> Cloud Logging bucket are retained for <strong>30 days</strong> and then automatically deleted by Google Cloud.</li>
    </ul>
    <h2>5. Third-Party Infrastructure &amp; Contact</h2>
    <p>RingScout runs on Google Cloud Run and Vertex AI (Google Maps Grounding &amp; Gemini Live) with Indian telephony termination via Vobiz. Full <a href="https://github.com/manavaga/ringscout/blob/main/PRIVACY.md">Privacy Notice</a>, <a href="https://github.com/manavaga/ringscout/blob/main/TERMS.md">Terms of Use</a>, <a href="https://github.com/manavaga/ringscout/blob/main/SUPPORT.md">Support</a>, and <a href="https://github.com/manavaga/ringscout/blob/main/SECURITY.md">Security Policy</a> are published on GitHub. Privacy/deletion contact: <strong>agarwalmanav91@gmail.com</strong>.</p>
  </main>
</body>
</html>"""
        return HTMLResponse(content=html)

    @app.get("/terms", include_in_schema=False)
    async def terms_redirect() -> PlainTextResponse:
        terms_path = Path(__file__).resolve().parents[2] / "TERMS.md"
        if terms_path.is_file():
            return PlainTextResponse(content=terms_path.read_text(encoding="utf-8"))
        return PlainTextResponse(content="See https://github.com/manavaga/ringscout/blob/main/TERMS.md")

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap_xml(request: Request) -> Response:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        urls = [
            f"{base_url}/",
            f"{base_url}/developers",
            f"{base_url}/dashboard",
            f"{base_url}/privacy",
            f"{base_url}/llms.txt",
            f"{base_url}/llms-full.txt",
            f"{base_url}/.well-known/ai-catalog.json",
            f"{base_url}/.well-known/mcp/server-card.json",
            f"{base_url}/.well-known/mcp.json",
            f"{base_url}/server.json",
            f"{base_url}/.well-known/agent-card.json",
            f"{base_url}/.well-known/agents.json",
            f"{base_url}/openapi-actions.json",
            f"{base_url}/.well-known/x402",
        ]
        items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
        xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
        return Response(content=xml, media_type="application/xml")

    @app.api_route("/ping", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/mcp/health", methods=["GET", "HEAD"], include_in_schema=False)
    async def mcp_directory_ping(request: Request) -> dict[str, Any]:
        """Fast `<2ms` uptime & liveness probe for Glama, Smithery, PulseMCP, and MCP Registry monitors."""
        base_url = _derive_public_base_url(request, manager.pstn_adapter).rstrip("/")
        return {
            "status": "ok",
            "service": "ringscout-mcp",
            "version": "3.0.0",
            "transport": "streamable-http",
            "endpoint": f"{base_url}/mcp",
            "sse_legacy_endpoint": f"{base_url}/sse",
            "server_card": f"{base_url}/.well-known/mcp/server-card.json",
        }

    async def _retention_sweeper_loop() -> None:
        while True:
            await asyncio.sleep(600.0)
            try:
                manager.purge_expired_data()
                endpoint_tracker.purge_expired_in_memory()
                analytics.purge_expired()
            except Exception:
                pass

    @app.on_event("startup")
    async def _sync_persisted_carrier_on_startup() -> None:
        manager.purge_expired_data()
        asyncio.create_task(_retention_sweeper_loop())
        cfg = manager.pstn_adapter.resolve_config()
        if cfg is not None and is_bridge_enabled():
            asyncio.create_task(sync_carrier_config_to_cloud_run(cfg))

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        manager.purge_expired_data()
        pstn_status = manager.pstn_adapter.masked_status()
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        pay_status = masked_payment_status()
        wss_ok = _is_publicly_reachable_base(base_url) or is_bridge_enabled()
        return {
            "status": "ok",
            "version": "3.0.0",
            "market": "IN",
            "gcp_project": GCP_PROJECT_ID,
            "live_model": LIVE_AUDIO_MODEL,
            "side_brain_model": SIDE_BRAIN_MODEL,
            "passcode_required": bool(_expected_passcode()),
            "live_pstn_configured": pstn_status["configured"],
            "live_pstn_provider": pstn_status["provider"],
            "live_pstn_from_number": pstn_status.get("from_number"),
            "pstn_pool_size": pstn_status.get("pool_size", 0),
            "twilio_pstn_configured": pstn_status["configured"],
            "public_wss_ready": wss_ok,
            "cloud_run_bridge_active": is_bridge_enabled(),
            "payment_gate_mode": pay_status["gate_mode"],
            "stripe_mode": pay_status["stripe_mode"],
            "retention_policy": {
                "call_audio_and_transcripts_ttl_seconds": int(manager.retention_ttl_seconds),
                "operational_logs_retention_days": OPERATIONAL_LOG_RETENTION_DAYS,
                "proactive_ai_and_recording_disclosure": True,
                "public_dashboard_redacts_transcripts_and_audio": True,
            },
        }

    @app.get("/api/locations")
    async def list_locations() -> dict[str, Any]:
        return {"cities": INDIA_CITY_AREAS}

    @app.get("/api/presets")
    async def list_presets() -> dict[str, Any]:
        return {"presets": INDIA_POC_PRESETS}

    # --- Operator / Admin Endpoints (`/api/pstn/*`, `/api/admin/*`, `GET /api/quests`) ---

    @app.get("/api/pstn/config", dependencies=[Depends(verify_admin_passcode)])
    async def get_pstn_config(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        data = manager.pstn_adapter.masked_status()
        data["detected_public_base_url"] = base_url
        data["public_wss_ready"] = _is_publicly_reachable_base(base_url) or is_bridge_enabled()
        return data

    @app.post("/api/pstn/config", dependencies=[Depends(verify_admin_passcode)])
    async def set_pstn_config(cfg: LiveCarrierConfig, request: Request) -> dict[str, Any]:
        clean_from = _validate_single_cli_number(cfg.from_number.strip(), cfg.provider)
        clean_pool: list[str] = [clean_from]
        for extra in cfg.from_numbers:
            for token in re.split(r"[,;\n]+", extra or ""):
                tok = token.strip()
                if not tok:
                    continue
                norm = _validate_single_cli_number(tok, cfg.provider)
                if norm not in clean_pool:
                    clean_pool.append(norm)

        clean_public = cfg.public_base_url.strip().rstrip("/") if cfg.public_base_url else None
        clean_cfg = cfg.model_copy(
            update={
                "auth_id": cfg.auth_id.strip(),
                "auth_token": cfg.auth_token.strip(),
                "from_number": clean_from,
                "from_numbers": clean_pool,
                "public_base_url": clean_public or None,
            }
        )
        manager.pstn_adapter.set_runtime_config(clean_cfg)
        save_local_carrier_config(clean_cfg)
        asyncio.create_task(sync_carrier_config_to_cloud_run(clean_cfg))
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        res = manager.pstn_adapter.masked_status()
        res["detected_public_base_url"] = base_url
        res["public_wss_ready"] = _is_publicly_reachable_base(base_url) or is_bridge_enabled()
        return res

    @app.get("/api/admin/payment-config", dependencies=[Depends(verify_admin_passcode)])
    async def get_admin_payment_config() -> dict[str, Any]:
        return masked_payment_status()

    @app.post("/api/admin/payment-config", dependencies=[Depends(verify_admin_passcode)])
    async def set_admin_payment_config(req: PaymentConfigUpdateRequest) -> dict[str, Any]:
        if req.gate_mode is not None and req.gate_mode.lower() not in ("off", "freemium", "required"):
            raise HTTPException(
                status_code=400,
                detail="gate_mode must be 'off' (Phase A Open), 'freemium', or 'required' (Phase B Paid).",
            )
        if req.star_gate_mode is not None and req.star_gate_mode.lower() not in ("off", "after_first_call", "strict"):
            raise HTTPException(
                status_code=400,
                detail="star_gate_mode must be 'off', 'after_first_call', or 'strict'.",
            )
        return update_runtime_payment_config(
            gate_mode=req.gate_mode,
            star_gate_mode=req.star_gate_mode,
            free_daily_calls_per_client=req.free_daily_calls_per_client,
            global_daily_free_pstn_legs=req.global_daily_free_pstn_legs,
            stripe_secret_key=req.stripe_secret_key,
            stripe_checkout_url=req.stripe_checkout_url,
        )

    @app.get("/api/admin/analytics", dependencies=[Depends(verify_admin_passcode)])
    async def get_admin_analytics() -> dict[str, Any]:
        return analytics.snapshot()

    @app.get("/api/quests", dependencies=[Depends(verify_admin_passcode)])
    async def list_recent_quests(request: Request) -> dict[str, Any]:
        """Return recent quests with transcripts and signed `.wav` links for the `/admin` Call Log Inspector."""
        base_url = _derive_public_base_url(request, manager.pstn_adapter).rstrip("/")
        items: list[dict[str, Any]] = []
        for q in reversed(list(manager.quests.values())[-25:]):
            calls_summary = []
            for c in q.ranked_quotes():
                signed_audio = None
                if c.audio_url:
                    sig = LiveIndiaPstnAdapter.sign_pstn_token("audio", q.quest_id, c.shop.shop_id)
                    signed_audio = f"{base_url}{c.audio_url}?sig={sig}"
                calls_summary.append(
                    {
                        "shop_id": c.shop.shop_id,
                        "name": c.shop.name,
                        "phone_display": c.shop.phone_display,
                        "address": c.shop.address,
                        "status": c.status.value,
                        "outcome": c.outcome.value,
                        "duration_sec": c.duration_sec,
                        "dtmf_sent": c.dtmf_sent,
                        "quote": c.quote.model_dump() if c.quote else None,
                        "audio_signed_url": signed_audio,
                        "transcript": [t.model_dump() for t in c.transcript],
                    }
                )
            created_iso = datetime.datetime.fromtimestamp(
                q.created_at_epoch, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            items.append(
                {
                    "quest_id": q.quest_id,
                    "created_at": created_iso,
                    "created_at_epoch": q.created_at_epoch,
                    "status": q.status,
                    "mode": q.mode.value,
                    "origin_channel": q.origin_channel,
                    "caller_agent": q.caller_agent,
                    "place_category": q.place_category.value,
                    "city": q.city,
                    "locality": q.locality,
                    "user_goal": q.user_goal,
                    "caller_name": q.caller_name,
                    "voice_name": q.voice_name,
                    "calls": calls_summary,
                }
            )
        return {"quests": items}

    @app.get("/api/dashboard/summary")
    async def get_live_dashboard_summary(request: Request) -> dict[str, Any]:
        """Public-safe live dashboard telemetry (redacts transcripts, caller names, and WAV audio unless admin)."""
        manager.purge_expired_data()
        base_url = _derive_public_base_url(request, manager.pstn_adapter).rstrip("/")
        provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get("passcode")
        is_admin = _check_admin_passcode_value(provided_pass)

        all_quests = [
            q
            for q in manager.quests.values()
            if is_admin or not (q.quest_id.startswith("q_test_") or q.origin_channel == "admin_self_test")
        ]
        channel_counts: Counter[str] = Counter()
        total_calls_dialed = 0
        pstn_calls_dialed = 0
        connected_calls = 0
        quotes_captured = 0
        active_calls_now = 0
        durations: list[float] = []

        connected_set = {
            CallOutcome.CONNECTED_QUOTE,
            CallOutcome.CONNECTED_NO_STOCK,
            CallOutcome.CONNECTED_INCONCLUSIVE,
            CallOutcome.IVR_NAVIGATED,
        }

        feed: list[dict[str, Any]] = []
        for q in reversed(all_quests[-40:]):
            ch = q.origin_channel or "web_consumer"
            channel_counts[ch] += 1
            call_rows = []
            for c in q.ranked_quotes():
                total_calls_dialed += 1
                if q.mode in (CallMode.LIVE_PSTN, CallMode.TWILIO_PSTN):
                    pstn_calls_dialed += 1
                if c.status in (CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS, CallStatus.IVR_MENU):
                    active_calls_now += 1
                if c.outcome in connected_set or (c.quote and c.quote.in_stock is not None):
                    connected_calls += 1
                if c.quote and c.quote.price_inr is not None:
                    quotes_captured += 1
                if c.duration_sec and c.duration_sec > 0:
                    durations.append(c.duration_sec)

                signed_audio = None
                if is_admin and c.audio_url:
                    sig = LiveIndiaPstnAdapter.sign_pstn_token("audio", q.quest_id, c.shop.shop_id)
                    signed_audio = f"{c.audio_url}?sig={sig}"
                raw_quote_dump = c.quote.model_dump() if c.quote else None
                call_rows.append(
                    {
                        "shop_id": c.shop.shop_id,
                        "name": c.shop.name,
                        "phone_display": c.shop.phone_display if is_admin else _mask_phone_for_public_dashboard(c.shop.phone_display),
                        "locality": c.shop.locality or q.locality,
                        "status": c.status.value,
                        "outcome": c.outcome.value,
                        "duration_sec": round(c.duration_sec, 1),
                        "dtmf_sent": c.dtmf_sent,
                        "carrier_provider": c.carrier_provider,
                        "call_uuid_short": (c.twilio_call_sid[:8] if (is_admin and c.twilio_call_sid) else None),
                        "quote": raw_quote_dump if is_admin else _public_safe_quote_dict(raw_quote_dump),
                        "audio_url": signed_audio,
                        "transcript": [t.model_dump() for t in c.transcript] if is_admin else [],
                    }
                )

            created_iso = datetime.datetime.fromtimestamp(
                q.created_at_epoch, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            feed.append(
                {
                    "quest_id": q.quest_id if is_admin else f"q_••••{q.quest_id[-4:]}",
                    "created_at": created_iso,
                    "status": q.status,
                    "mode": q.mode.value,
                    "origin_channel": ch,
                    "caller_agent": q.caller_agent,
                    "place_category": q.place_category.value,
                    "city": q.city,
                    "locality": q.locality,
                    "user_goal": q.user_goal if is_admin else f"{q.place_category.value} check ({q.locality or q.city})",
                    "calls": call_rows,
                }
            )

        traffic = analytics.snapshot()
        agentic_quests = sum(
            cnt for k, cnt in channel_counts.items() if k in ("mcp", "a2a", "api_scout", "chatgpt_action")
        )
        web_quests = channel_counts.get("web_consumer", 0)
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0.0

        return {
            "kpis": {
                "total_quests": len(all_quests),
                "agentic_quests": agentic_quests,
                "web_quests": web_quests,
                "total_calls_dialed": total_calls_dialed,
                "pstn_calls_dialed": pstn_calls_dialed,
                "connected_calls": connected_calls,
                "quotes_captured": quotes_captured,
                "active_calls_now": active_calls_now,
                "avg_duration_sec": avg_dur,
                "ai_bot_crawls": traffic["totals"]["ai_bot_crawls"],
                "organic_referrals": traffic["totals"]["organic_and_ai_referrals"],
                "protocol_requests": traffic["totals"]["agent_protocol_calls"],
            },
            "channels": dict(channel_counts),
            "traffic": traffic,
            "pstn": {
                "configured": manager.pstn_adapter.is_configured(),
                "provider": manager.pstn_adapter.masked_status().get("provider"),
            },
            "mcp_directories": [
                {"platform": "Official MCP Registry (modelcontextprotocol.io)", "manifest": f"{base_url}/server.json", "status": "Ready · server.json v1"},
                {"platform": "Smithery.ai Marketplace", "manifest": "smithery.yaml (GitHub root)", "status": "Ready · Auto-Build"},
                {"platform": "Glama.ai MCP Directory", "manifest": "glama.json (GitHub root)", "status": "Ready · Schema Verified"},
                {"platform": "Anthropic Claude (claude.ai & Desktop)", "manifest": f"{base_url}/mcp", "status": "Live · Streamable HTTP"},
                {"platform": "Google Gemini & Vertex A2A v1.0", "manifest": f"{base_url}/.well-known/agent-card.json", "status": "Live · A2A + MCP"},
                {"platform": "OpenAI ChatGPT Actions & GPT Store", "manifest": f"{base_url}/openapi-actions.json", "status": "Live · OpenAPI 3.1.0"},
                {"platform": "Cursor / Windsurf / PulseMCP / MCP.so", "manifest": f"{base_url}/.well-known/mcp.json", "status": "Live · Auto-Discovery"},
                {"platform": "AI Search GEO (ChatGPT / Perplexity / Claude)", "manifest": f"{base_url}/llms.txt", "status": "Live · Bots Allowed"},
            ],
            "quests": feed,
        }

    @app.post("/api/pstn/test-call", dependencies=[Depends(verify_admin_passcode)])
    async def trigger_self_test_call(
        req: SelfTestCallRequest, request: Request
    ) -> dict[str, Any]:
        if not manager.pstn_adapter.is_configured():
            raise HTTPException(
                status_code=400,
                detail="Configure Vobiz.ai, Plivo, or Twilio credentials first before placing a live self-test call.",
            )
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        direct_public = _is_publicly_reachable_base(base_url)
        if not direct_public and not is_bridge_enabled():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Current server URL ({base_url}) is private to Cloudtop. "
                    "Deploy to Cloud Run in `manav-chub` (`./deploy_cloud_run.sh`) or provide a public `https://` URL so the carrier can stream audio back."
                ),
            )
        quest = manager.create_self_test_quest(req)
        quest.call_plan = heuristic_conversational_call_plan(
            quest.user_goal, quest.preferred_tone, quest.place_category
        )
        asyncio.create_task(_warm_call_plan_async(quest))
        stop_ev = manager.stop_events[quest.quest_id]
        if is_bridge_enabled():
            asyncio.create_task(
                relay_self_test_via_cloud_run(
                    my_phone=req.my_phone,
                    quest=quest,
                    audio_store=manager.audio_store,
                    publish=manager.publish,
                    stop_event=stop_ev,
                    pstn_adapter=manager.pstn_adapter,
                )
            )
        else:
            asyncio.create_task(
                _staggered_pstn_dial(0.0, quest, quest.shops[0], base_url, stop_ev)
            )
        return _quest_payload(quest, include_token=True)

    # --- Consumer H2A Endpoints (`/api/discover`, `/api/quests`) ---

    @app.post(
        "/api/discover",
        response_model=DiscoverResponse,
        dependencies=[Depends(verify_user_passcode)],
    )
    async def discover_shops_endpoint(req: DiscoverRequest) -> DiscoverResponse:
        try:
            resp = await discover_indian_shops(req)
        except Exception as exc:
            msg = str(exc)
            if "403" in msg or "PERMISSION_DENIED" in msg or "Reauthentication" in msg:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Vertex AI session expired on local workstation. "
                        "Run `gcloud auth application-default login` or open your Cloud Run deployment."
                    ),
                ) from exc
            raise HTTPException(status_code=502, detail=f"Google Maps lookup error: {msg[:180]}") from exc
        manager.register_discovered_shops(resp)
        return resp

    @app.post("/api/places/add", dependencies=[Depends(verify_user_passcode)])
    async def add_specific_place_endpoint(
        req: CustomPlaceAddRequest, request: Request
    ) -> dict[str, Any]:
        """Search Google Maps for a specific named salon/spa/hotel OR add a verified business +91 number."""
        raw_phone = req.phone.strip()
        provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get(
            "passcode"
        )
        is_admin = _check_admin_passcode_value(provided_pass)

        if raw_phone:
            e164, display, reason = normalize_indian_phone(raw_phone)
            if reason != "ok" or not e164 or not display:
                raise HTTPException(
                    status_code=400,
                    detail="Enter a valid Indian +91 business mobile or STD landline number (1800 toll-free numbers are blocked).",
                )
            # P0 Provenance Guard: Unauthenticated public callers cannot inject arbitrary personal +91 numbers
            # into verified_google_phones unless the number matches a Google Maps business listing!
            if not is_admin and e164 not in manager.verified_google_phones:
                try:
                    verify_disc = await discover_indian_shops(
                        DiscoverRequest(
                            specific_place=req.place_name.strip(),
                            place_category=req.place_category,
                            city=req.city,
                            locality=req.locality,
                            max_shops=6,
                        )
                    )
                    manager.register_discovered_shops(verify_disc)
                except Exception:
                    pass
                if e164 not in manager.verified_google_phones:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Unlisted +91 phone numbers require Admin Passcode to prevent personal-number spam. "
                            "Leave the phone field blank to add any listed salon, spa, or business directly from Google Maps."
                        ),
                    )

            manager.verified_google_phones.add(e164)
            shop = IndianShopTarget(
                shop_id=f"custom_{uuid.uuid4().hex[:6]}",
                name=req.place_name.strip(),
                phone_e164=e164,
                phone_display=display,
                address=f"{req.locality}, {req.city}, India",
                locality=req.locality,
                city=req.city,
                open_status="ADDED BY USER",
                is_open_now=True,
                category=req.place_category.value.title(),
                locality_match_tier=0,
                locality_badge=f"In {req.locality}",
            )
            return {"shops": [shop.model_dump()], "source": "custom_business_number"}

        disc = await discover_indian_shops(
            DiscoverRequest(
                specific_place=req.place_name.strip(),
                place_category=req.place_category,
                city=req.city,
                locality=req.locality,
                max_shops=4,
            )
        )
        manager.register_discovered_shops(disc)
        return {"shops": [s.model_dump() for s in disc.shops], "source": disc.source}

    @app.post("/api/quests", dependencies=[Depends(verify_user_passcode)])
    async def start_quest(req: QuestCreateRequest, request: Request) -> dict[str, Any]:
        is_pstn_mode = req.mode in (CallMode.LIVE_PSTN, CallMode.TWILIO_PSTN)
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        direct_public = _is_publicly_reachable_base(base_url)
        if is_pstn_mode:
            if not manager.pstn_adapter.is_configured():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "CARRIER_NOT_CONFIGURED: Enter your Vobiz.ai, Plivo, or Twilio +91 credentials to dial real phone lines, "
                        "or select 'Simulated Sandbox Demo'."
                    ),
                )
            if not direct_public and not is_bridge_enabled():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Current server URL ({base_url}) is not reachable by external telecom carriers. "
                        "Open your Cloud Run URL (`./deploy_cloud_run.sh`) or set a public `https://` base URL in `/admin`."
                    ),
                )
            provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get("passcode")
            if not _check_admin_passcode_value(provided_pass) or not _expected_admin_passcode():
                fp = _client_fingerprint(request)
                now_ts = time.time()
                hourly_limit = int(os.environ.get("RINGSCOUT_PUBLIC_HOURLY_QUEST_LIMIT", "12"))
                recent_hits = [t for t in _PUBLIC_PSTN_BUCKET[fp] if now_ts - t < 3600.0]
                if len(recent_hits) >= hourly_limit:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Public live-call safety cap ({hourly_limit} quests/hour per visitor) reached. Try again shortly.",
                    )
                recent_hits.append(now_ts)
                _PUBLIC_PSTN_BUCKET[fp] = recent_hits

        quest = manager.create_quest(req)
        quest.call_plan = heuristic_conversational_call_plan(
            quest.user_goal, quest.preferred_tone, quest.place_category
        )
        if is_pstn_mode or synthesize_audio:
            asyncio.create_task(_warm_call_plan_async(quest))
        stop_ev = manager.stop_events[quest.quest_id]

        async def _run_swarm_with_bridge() -> None:
            if is_bridge_enabled():
                ok = await relay_quest_via_cloud_run(
                    quest=quest,
                    mode_str="hinglish_swarm",
                    audio_store=manager.audio_store,
                    publish=manager.publish,
                    stop_event=stop_ev,
                )
                if ok:
                    return
            await manager.swarm_adapter.run_quest(
                quest=quest,
                stop_event=stop_ev,
                publish=manager.publish,
            )

        if req.mode == CallMode.HINGLISH_SWARM:
            asyncio.create_task(_run_swarm_with_bridge())
        elif is_pstn_mode:
            if is_bridge_enabled():
                asyncio.create_task(
                    relay_quest_via_cloud_run(
                        quest=quest,
                        mode_str="live_pstn",
                        audio_store=manager.audio_store,
                        publish=manager.publish,
                        stop_event=stop_ev,
                        pstn_adapter=manager.pstn_adapter,
                    )
                )
            else:
                cum_delay = 0.0
                for idx, shop in enumerate(quest.shops):
                    if idx > 0:
                        cum_delay += random.uniform(1.8, 3.2)
                    asyncio.create_task(
                        _staggered_pstn_dial(
                            cum_delay, quest, shop, base_url, stop_ev
                        )
                    )

        return _quest_payload(quest, include_token=True)

    @app.get("/api/quests/{quest_id}", dependencies=[Depends(verify_user_passcode)])
    async def get_quest(quest_id: str, request: Request) -> dict[str, Any]:
        manager.purge_expired_data()
        quest = manager.quests.get(quest_id)
        if not quest:
            raise HTTPException(status_code=404, detail="Quest not found")
        authorized = _is_quest_owner_or_admin(request, quest_id)
        if (quest_id.startswith("q_test_") or quest.origin_channel == "admin_self_test") and not authorized:
            raise HTTPException(status_code=403, detail="Admin passcode required to inspect operator self-test quest.")
        return _quest_payload(quest, mask_phones=not authorized)

    @app.delete("/api/quests/{quest_id}", dependencies=[Depends(verify_user_passcode)])
    async def delete_quest_endpoint(quest_id: str, request: Request) -> dict[str, Any]:
        """Self-service immediate deletion of a quest, its transcripts, and any in-memory WAV audio clips."""
        manager.purge_expired_data()
        quest = manager.quests.get(quest_id)
        if not quest:
            raise HTTPException(status_code=404, detail="Quest not found or already deleted")
        if not _is_quest_owner_or_admin(request, quest_id):
            raise HTTPException(
                status_code=403,
                detail="Quest owner token (X-Quest-Token) or Admin Passcode required to delete quest data.",
            )
        manager._evict_quest(quest_id, quest)
        return {"deleted": True, "quest_id": quest_id}

    @app.post("/api/quests/{quest_id}/stop", dependencies=[Depends(verify_user_passcode)])
    async def stop_quest_endpoint(quest_id: str, request: Request) -> dict[str, Any]:
        manager.purge_expired_data()
        if quest_id not in manager.quests:
            raise HTTPException(status_code=404, detail="Quest not found")
        if not _is_quest_owner_or_admin(request, quest_id):
            raise HTTPException(
                status_code=403,
                detail="Quest owner token (X-Quest-Token) or Admin Passcode required to stop an active quest.",
            )
        quest = await manager.stop_quest(quest_id)
        return _quest_payload(quest)

    @app.get("/api/quests/{quest_id}/events", dependencies=[Depends(verify_user_passcode)])
    async def stream_quest_events(quest_id: str, request: Request) -> StreamingResponse:
        manager.purge_expired_data()
        quest = manager.quests.get(quest_id)
        if not quest:
            raise HTTPException(status_code=404, detail="Quest not found")
        authorized = _is_quest_owner_or_admin(request, quest_id)
        if (quest_id.startswith("q_test_") or quest.origin_channel == "admin_self_test") and not authorized:
            raise HTTPException(status_code=403, detail="Admin passcode required to stream operator self-test quest.")

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        manager.subscribers[quest_id].add(queue)

        async def event_generator() -> AsyncIterator[str]:
            try:
                yield f"event: snapshot\ndata: {json.dumps(_quest_payload(quest, mask_phones=not authorized))}\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                        ev_name = item["event"]
                        if not authorized and ev_name == "transcript":
                            continue
                        data = dict(item["data"])
                        masked_payload = _quest_payload(quest, mask_phones=not authorized)
                        data["ranked_quotes"] = masked_payload["ranked_quotes"]
                        if not authorized and "call" in data and isinstance(data["call"], dict):
                            call_copy = json.loads(json.dumps(data["call"]))
                            if "shop" in call_copy:
                                call_copy["shop"]["phone_e164"] = ""
                                call_copy["shop"]["phone_display"] = _mask_phone_for_public_dashboard(
                                    str(call_copy["shop"].get("phone_display") or "")
                                )
                            call_copy["transcript"] = []
                            call_copy["audio_url"] = None
                            call_copy["quote"] = _public_safe_quote_dict(call_copy.get("quote"))
                            data["call"] = call_copy
                        elif authorized and "call" in data and isinstance(data["call"], dict):
                            call_copy = json.loads(json.dumps(data["call"]))
                            raw_aud = call_copy.get("audio_url")
                            shop_id_ev = (call_copy.get("shop") or {}).get("shop_id") or ""
                            if raw_aud and "sig=" not in str(raw_aud) and shop_id_ev:
                                sig_ev = LiveIndiaPstnAdapter.sign_pstn_token("audio", quest.quest_id, shop_id_ev)
                                call_copy["audio_url"] = f"{raw_aud}?sig={sig_ev}"
                            data["call"] = call_copy
                        data["quest_status"] = quest.status
                        yield f"event: {ev_name}\ndata: {json.dumps(data)}\n\n"
                        if ev_name == "quest_complete" and quest.mode != CallMode.BROWSER_MIC:
                            break
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                manager.subscribers.get(quest_id, set()).discard(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.api_route("/api/quests/{quest_id}/calls/{shop_id}/audio.wav", methods=["GET", "HEAD"])
    async def get_call_audio_wav(
        quest_id: str, shop_id: str, request: Request
    ) -> Response:
        manager.purge_expired_data()
        sig = request.query_params.get("sig")
        if not _is_quest_owner_or_admin(request, quest_id) and not LiveIndiaPstnAdapter.verify_pstn_token(
            "audio", quest_id, shop_id, sig
        ):
            raise HTTPException(
                status_code=401,
                detail="Admin passcode, quest owner token, or signed caller audio receipt token required",
            )
        wav_bytes = manager.audio_store.get((quest_id, shop_id))
        if not wav_bytes:
            raise HTTPException(status_code=404, detail="Audio clip not available or expired (24h retention)")
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "Content-Length": str(len(wav_bytes)),
            },
        )

    # --- Track 2: Agent-to-Agent (A2A v1.0 / MCP / ChatGPT Actions / Stripe MPP / x402 v2) ---

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")
    async def a2a_agent_card(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return build_agent_card(base_url, manager.pstn_adapter.is_configured())

    @app.get("/.well-known/mcp.json")
    @app.get("/.well-known/mcp")
    async def mcp_registry_card(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return build_mcp_registry_manifest(base_url, live_pstn_ready=manager.pstn_adapter.is_configured())

    @app.get("/.well-known/mcp/server-card.json")
    @app.get("/mcp/server-card")
    async def mcp_sep2127_server_card(request: Request) -> Response:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        card = build_mcp_server_card(base_url, live_pstn_ready=manager.pstn_adapter.is_configured())
        accept = (request.headers.get("accept") or "").lower()
        media_type = (
            "application/json"
            if ("application/json" in accept and "application/mcp-server-card+json" not in accept and "*/*" not in accept)
            else "application/mcp-server-card+json"
        )
        return Response(
            content=json.dumps(card, indent=2),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/.well-known/ai-catalog.json")
    @app.get("/.well-known/ai-catalog")
    @app.get("/.well-known/ard.json")
    async def ai_catalog_well_known(request: Request) -> Response:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        catalog = build_ai_catalog_manifest(base_url)
        accept = (request.headers.get("accept") or "").lower()
        media_type = (
            "application/json"
            if ("application/json" in accept and "application/ai-catalog+json" not in accept and "*/*" not in accept)
            else "application/ai-catalog+json"
        )
        return Response(
            content=json.dumps(catalog, indent=2),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/.well-known/agents.json")
    @app.get("/agents.json")
    async def agents_json_manifest(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return build_agents_json_manifest(base_url)

    @app.get("/.well-known/ai-plugin.json")
    async def ai_plugin_manifest(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return build_ai_plugin_manifest(base_url)

    @app.get("/.well-known/mcp-registry-auth", include_in_schema=False)
    async def mcp_registry_http_domain_verification() -> PlainTextResponse:
        """Serve `v=MCPv1; k=ed25519; p=<PUBKEY>` for `mcp-publisher login http --domain ...` verification."""
        pub_key = (
            os.environ.get("MCP_REGISTRY_PUBLIC_KEY", "").strip()
            or get_secret("MCP_REGISTRY_PUBLIC_KEY")
        )
        if not pub_key:
            raise HTTPException(
                status_code=404,
                detail="MCP_REGISTRY_PUBLIC_KEY not configured (use GitHub login or set Ed25519 public key).",
            )
        proof = pub_key if pub_key.startswith("v=MCPv1;") else f"v=MCPv1; k=ed25519; p={pub_key}"
        return PlainTextResponse(content=proof, headers={"Cache-Control": "no-store"})

    @app.get("/openapi-actions.json")
    @app.get("/.well-known/openapi.json")
    async def chatgpt_openapi_actions(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return build_openapi_actions_spec(base_url)

    @app.get("/.well-known/x402")
    @app.get("/api/v1/x402/discovery")
    async def x402_discovery_catalog(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return build_x402_discovery_manifest(base_url, manager.pstn_adapter.is_configured())

    @app.get("/api/v1/pricing")
    async def a2a_pricing_spec(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        _, challenge_body = build_402_challenge(f"{base_url}/api/v1/scout")
        challenge_body["paymentStatus"] = masked_payment_status()
        return challenge_body

    @app.get("/api/v1/scout")
    async def x402_scout_crawler_probe(request: Request) -> Response:
        """Allow `x402scan`, Stripe MPP & Bazaar crawlers probing `GET /api/v1/scout` to inspect 402 terms."""
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        hdrs, body = build_402_challenge(f"{base_url}/api/v1/scout")
        return JSONResponse(status_code=402, content=body, headers=hdrs)

    @app.post("/api/v1/scout")
    async def a2a_paid_scout_endpoint(
        req: A2AScoutRequest, request: Request
    ) -> Response:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        resource_url = f"{base_url}/api/v1/scout"
        provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get(
            "passcode"
        )
        pass_ok = _check_admin_passcode_value(provided_pass)
        pay_sig = request.headers.get("payment-signature") or request.headers.get(
            "x-payment"
        )
        settlement, reason = await verify_a2a_payment(
            resource_url=resource_url,
            payment_sig_header=pay_sig,
            auth_header=request.headers.get("authorization"),
            sandbox_header=request.headers.get("x-sandbox-payment"),
            ap2_mandate=req.ap2_mandate,
            ap2_header_raw=request.headers.get("x-ap2-mandate"),
            owner_passcode_valid=pass_ok,
            stripe_token_header=request.headers.get("x-stripe-payment-token"),
            client_fingerprint=_client_fingerprint(request),
            user_agent=request.headers.get("user-agent"),
            execution_mode=req.execution_mode,
            github_username=request.headers.get("x-github-username") or req.github_username or None,
        )
        if settlement is None:
            hdrs, body = build_402_challenge(resource_url, detail=reason)
            return JSONResponse(status_code=402, content=body, headers=hdrs)

        try:
            result = await execute_a2a_scout(
                req=req,
                manager=manager,
                base_url=base_url,
                public_wss_ready=_is_publicly_reachable_base(base_url) or is_bridge_enabled(),
                settlement=settlement,
                owner_passcode_valid=pass_ok,
            )
            return JSONResponse(
                status_code=200,
                content=result,
                headers=build_payment_response_headers(settlement),
            )
        except PaymentRequiredError as pay_exc:
            hdrs, body = build_402_challenge(resource_url, detail=str(pay_exc))
            return JSONResponse(status_code=402, content=body, headers=hdrs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/a2a")
    async def a2a_info(request: Request) -> dict[str, Any]:
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return {
            "protocol": "google-a2a-1.0",
            "binding": "JSON-RPC-2.0",
            "endpoint": f"{base_url}/a2a",
            "agentCard": f"{base_url}/.well-known/agent-card.json",
            "mcp": f"{base_url}/mcp",
            "chatgptActions": f"{base_url}/openapi-actions.json",
            "x402Discovery": f"{base_url}/.well-known/x402",
            "paymentGateMode": masked_payment_status()["gate_mode"],
            "methods": ["message/send", "tasks/get", "tasks/cancel"],
        }

    @app.post("/a2a")
    async def a2a_jsonrpc_endpoint(request: Request) -> Response:
        try:
            rpc_body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid A2A JSON-RPC payload") from exc

        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get(
            "passcode"
        )
        pass_ok = _check_admin_passcode_value(provided_pass)
        pay_sig = request.headers.get("payment-signature") or request.headers.get(
            "x-payment"
        )
        status_code, resp_hdrs, resp_body = await handle_a2a_jsonrpc(
            rpc_body,
            manager=manager,
            base_url=base_url,
            public_wss_ready=_is_publicly_reachable_base(base_url) or is_bridge_enabled(),
            payment_sig_header=pay_sig,
            auth_header=request.headers.get("authorization"),
            sandbox_header=request.headers.get("x-sandbox-payment"),
            ap2_header_raw=request.headers.get("x-ap2-mandate"),
            owner_passcode_valid=pass_ok,
            stripe_token_header=request.headers.get("x-stripe-payment-token"),
            client_fingerprint=_client_fingerprint(request),
            user_agent=request.headers.get("user-agent"),
            github_username_header=request.headers.get("x-github-username"),
        )
        return JSONResponse(status_code=status_code, content=resp_body, headers=resp_hdrs)

    @app.post("/api/v1/demo/buyer-sign-x402", dependencies=[Depends(verify_user_passcode)])
    async def sign_demo_eip3009_x402() -> dict[str, Any]:
        """Generate a real secp256k1 EIP-712 `TransferWithAuthorization` (`EIP-3009`) x402 v2 signature."""
        payload = create_signed_demo_x402_payload(network=BASE_SEPOLIA_CAIP2_NETWORK)
        b64 = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return {
            "buyer_wallet": demo_buyer_wallet_address(),
            "payment_signature_b64": b64,
            "decoded_payload": payload,
        }

    @app.post("/api/v1/demo/buyer-sign-stripe", dependencies=[Depends(verify_user_passcode)])
    async def sign_demo_stripe_mpp(request: Request) -> dict[str, Any]:
        """Generate a spec-compliant Stripe `MPP` `Authorization: Payment <b64url>` SPT credential."""
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        b64_cred, decoded = create_signed_demo_stripe_mpp_credential(f"{base_url}/api/v1/scout")
        return {
            "authorization_header": f"Payment {b64_cred}",
            "credential_b64url": b64_cred,
            "decoded_credential": decoded,
            "stripe_status": masked_payment_status(),
        }

    sse_mcp_sessions: OrderedDict[str, asyncio.Queue[str]] = OrderedDict()

    @app.head("/mcp")
    async def mcp_head_liveness() -> Response:
        return Response(
            status_code=200,
            headers={
                "MCP-Protocol-Version": "2025-06-18",
                "Allow": "POST, GET, HEAD, DELETE, OPTIONS",
            },
        )

    @app.delete("/mcp")
    async def mcp_delete_session(request: Request) -> Response:
        """Cleanly acknowledge MCP Streamable HTTP session termination (Spec 2.1.5) and evict any matching SSE queue."""
        sid = (request.headers.get("mcp-session-id") or request.query_params.get("session_id") or "").strip()
        if sid:
            sse_mcp_sessions.pop(sid, None)
            if sid.startswith("mcp_"):
                sse_mcp_sessions.pop(sid[4:], None)
        return Response(status_code=204)

    @app.get("/mcp")
    @app.get("/mcp/", include_in_schema=False)
    async def mcp_info(request: Request) -> Response:
        accept = (request.headers.get("accept") or "").lower()
        if "text/event-stream" in accept:
            # Per MCP Streamable HTTP spec 2.1.3: return HTTP 405 when standalone server-push SSE is unused
            return Response(status_code=405, headers={"Allow": "POST, GET, HEAD, DELETE, OPTIONS"})
        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        return JSONResponse(
            content={
                "transport": "streamable-http",
                "protocol": "mcp-jsonrpc-2.0",
                "supported_protocol_versions": ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"],
                "endpoint": f"{base_url}/mcp",
                "legacy_sse_endpoint": f"{base_url}/sse",
                "server_card": f"{base_url}/.well-known/mcp/server-card.json",
                "mcp_manifest": f"{base_url}/.well-known/mcp.json",
                "official_registry_manifest": f"{base_url}/server.json",
                "ai_catalog": f"{base_url}/.well-known/ai-catalog.json",
                "agent_card": f"{base_url}/.well-known/agent.json",
                "payment_gate_mode": masked_payment_status()["gate_mode"],
            },
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )

    async def _dispatch_mcp_request(request: Request) -> tuple[Any, dict[str, str]]:
        try:
            rpc_body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON-RPC payload") from exc
        endpoint_tracker.record_mcp_rpc(rpc_body, user_agent=request.headers.get("user-agent") or "")

        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get(
            "passcode"
        )
        pass_ok = _check_admin_passcode_value(provided_pass)
        pay_sig = request.headers.get("payment-signature") or request.headers.get(
            "x-payment"
        )
        incoming_sid = (
            request.headers.get("mcp-session-id")
            or request.query_params.get("session_id")
            or None
        )
        mcp_headers: dict[str, str] = {}
        rpc_resp = await handle_mcp_payload(
            rpc_body,
            manager=manager,
            base_url=base_url,
            public_wss_ready=_is_publicly_reachable_base(base_url) or is_bridge_enabled(),
            payment_sig_header=pay_sig,
            auth_header=request.headers.get("authorization"),
            sandbox_header=request.headers.get("x-sandbox-payment"),
            ap2_header_raw=request.headers.get("x-ap2-mandate"),
            owner_passcode_valid=pass_ok,
            stripe_token_header=request.headers.get("x-stripe-payment-token"),
            client_fingerprint=_client_fingerprint(request),
            user_agent=request.headers.get("user-agent"),
            github_username_header=request.headers.get("x-github-username"),
            out_headers=mcp_headers,
            incoming_session_id=incoming_sid,
        )
        return rpc_resp, mcp_headers

    @app.post("/mcp")
    @app.post("/mcp/", include_in_schema=False)
    async def mcp_streamable_http(request: Request) -> Response:
        rpc_resp, mcp_headers = await _dispatch_mcp_request(request)
        if rpc_resp is None:
            return Response(status_code=202, headers=mcp_headers)
        return JSONResponse(status_code=200, content=rpc_resp, headers=mcp_headers)

    @app.head("/sse")
    async def mcp_legacy_sse_head() -> Response:
        return Response(
            status_code=200,
            headers={"Allow": "GET, POST, HEAD, OPTIONS", "MCP-Protocol-Version": "2025-06-18"},
        )

    @app.post("/sse")
    async def mcp_dual_sse_post(request: Request) -> Response:
        """Accept Streamable HTTP JSON-RPC POSTs on `/sse` for dual-transport scanners (e.g. `alalanet-prober`)."""
        rpc_resp, mcp_headers = await _dispatch_mcp_request(request)
        if rpc_resp is None:
            return Response(status_code=202, headers=mcp_headers)
        return JSONResponse(status_code=200, content=rpc_resp, headers=mcp_headers)

    @app.get("/sse")
    async def mcp_legacy_sse_connect(request: Request) -> StreamingResponse:
        """Backward-compatible MCP `2024-11-05` HTTP+SSE transport (`GET /sse` + `POST /messages`)."""
        sid = uuid.uuid4().hex
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        sse_mcp_sessions[sid] = q
        while len(sse_mcp_sessions) > 128:
            sse_mcp_sessions.popitem(last=False)

        async def sse_gen() -> AsyncIterator[str]:
            try:
                yield f"event: endpoint\ndata: /messages?session_id={sid}\n\n"
                while sse_mcp_sessions.get(sid) is q:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"event: message\ndata: {msg}\n\n"
                    except asyncio.TimeoutError:
                        if sse_mcp_sessions.get(sid) is not q:
                            break
                        yield ": keepalive\n\n"
            finally:
                if sse_mcp_sessions.get(sid) is q:
                    sse_mcp_sessions.pop(sid, None)

        return StreamingResponse(
            sse_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/messages")
    async def mcp_legacy_sse_messages(request: Request) -> Response:
        """Receive JSON-RPC messages for legacy MCP `2024-11-05` HTTP+SSE clients."""
        sid = (request.query_params.get("session_id") or "").strip()
        if sid and sid not in sse_mcp_sessions:
            raise HTTPException(status_code=404, detail="Unknown or expired SSE session_id")
        rpc_resp, mcp_headers = await _dispatch_mcp_request(request)
        if sid:
            if rpc_resp is not None:
                payload_list = rpc_resp if isinstance(rpc_resp, list) else [rpc_resp]
                for item in payload_list:
                    try:
                        sse_mcp_sessions[sid].put_nowait(json.dumps(item))
                    except (KeyError, asyncio.QueueFull):
                        pass
            return Response(status_code=202, content="Accepted", headers=mcp_headers)
        if rpc_resp is None:
            return Response(status_code=202, headers=mcp_headers)
        return JSONResponse(status_code=200, content=rpc_resp, headers=mcp_headers)

    # --- HMAC-Signed Telecom Carrier Webhooks & Bidirectional Audio Streams ---

    @app.api_route(
        "/api/pstn/answer/{provider}/{quest_id}/{shop_id}",
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    async def pstn_answer_xml(
        provider: str, quest_id: str, shop_id: str, request: Request
    ) -> Response:
        sig = request.query_params.get("sig")
        if provider not in {p.value for p in CarrierProvider} or not LiveIndiaPstnAdapter.verify_pstn_token(
            provider, quest_id, shop_id, sig
        ):
            raise HTTPException(status_code=403, detail="Invalid carrier webhook signature")

        quest = manager.quests.get(quest_id)
        if not quest or shop_id not in quest.calls:
            raise HTTPException(status_code=404, detail="Call session not found")

        stop_ev = manager.stop_events.get(quest_id)
        if quest.status == "stopped" or (stop_ev and stop_ev.is_set()):
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                media_type="application/xml",
            )

        if request.method == "POST":
            body_data = await _parse_webhook_body(request)
            call_uuid = str(
                body_data.get("CallUUID") or body_data.get("CallSid") or ""
            ).strip()
            if call_uuid:
                quest.calls[shop_id].twilio_call_sid = call_uuid

        base_url = _derive_public_base_url(request, manager.pstn_adapter)
        _, ws_base = _normalize_public_urls(base_url)
        ws_stream_url = (
            f"{ws_base}/ws/pstn/stream/{provider}/{quest_id}/{shop_id}?sig={sig}"
        )
        xml_body = LiveIndiaPstnAdapter.build_answer_xml(
            CarrierProvider(provider), ws_stream_url
        )
        return Response(content=xml_body, media_type="application/xml")

    @app.post(
        "/api/pstn/status/{provider}/{quest_id}/{shop_id}",
        include_in_schema=False,
    )
    async def pstn_status_webhook(
        provider: str, quest_id: str, shop_id: str, request: Request
    ) -> dict[str, Any]:
        sig = request.query_params.get("sig")
        if provider not in {p.value for p in CarrierProvider} or not LiveIndiaPstnAdapter.verify_pstn_token(
            provider, quest_id, shop_id, sig
        ):
            raise HTTPException(status_code=403, detail="Invalid carrier webhook signature")

        quest = manager.quests.get(quest_id)
        if not quest or shop_id not in quest.calls:
            return {"ok": False}

        payload: dict[str, Any] = dict(request.query_params)
        payload.update(await _parse_webhook_body(request))

        manager.pstn_adapter.handle_status_webhook(
            quest=quest,
            call=quest.calls[shop_id],
            payload=payload,
            publish=manager.publish,
        )
        return {"ok": True}

    @app.websocket("/ws/mic-call/{quest_id}/{shop_id}")
    async def browser_mic_websocket(websocket: WebSocket, quest_id: str, shop_id: str) -> None:
        provided_pass = websocket.query_params.get("passcode")
        qtok = websocket.query_params.get("token")
        if not _check_admin_passcode_value(provided_pass) and not LiveIndiaPstnAdapter.verify_pstn_token(
            "quest_owner", quest_id, "owner", qtok
        ):
            await websocket.close(code=1008)
            return
        quest = manager.quests.get(quest_id)
        if not quest or shop_id not in quest.calls:
            await websocket.close(code=1008)
            return
        await manager.mic_adapter.handle_websocket(
            websocket=websocket,
            quest=quest,
            call=quest.calls[shop_id],
            stop_event=manager.stop_events[quest_id],
            publish=manager.publish,
        )

    @app.websocket("/ws/pstn/stream/{provider}/{quest_id}/{shop_id}")
    async def pstn_media_websocket(
        websocket: WebSocket, provider: str, quest_id: str, shop_id: str
    ) -> None:
        sig = websocket.query_params.get("sig")
        if provider not in {p.value for p in CarrierProvider} or not LiveIndiaPstnAdapter.verify_pstn_token(
            provider, quest_id, shop_id, sig
        ):
            await websocket.close(code=1008)
            return
        quest = manager.quests.get(quest_id)
        if not quest or shop_id not in quest.calls:
            await websocket.close(code=1008)
            return
        await manager.pstn_adapter.handle_media_websocket(
            websocket=websocket,
            quest=quest,
            call=quest.calls[shop_id],
            stop_event=manager.stop_events[quest_id],
            publish=manager.publish,
            provider=CarrierProvider(provider),
        )

    @app.websocket("/ws/twilio-media/{quest_id}/{shop_id}")
    async def twilio_media_websocket(websocket: WebSocket, quest_id: str, shop_id: str) -> None:
        sig = websocket.query_params.get("sig")
        if not LiveIndiaPstnAdapter.verify_pstn_token(
            CarrierProvider.TWILIO.value, quest_id, shop_id, sig
        ):
            await websocket.close(code=1008)
            return
        quest = manager.quests.get(quest_id)
        if not quest or shop_id not in quest.calls:
            await websocket.close(code=1008)
            return
        await manager.pstn_adapter.handle_media_websocket(
            websocket=websocket,
            quest=quest,
            call=quest.calls[shop_id],
            stop_event=manager.stop_events[quest_id],
            publish=manager.publish,
            provider=CarrierProvider.TWILIO,
        )

    @app.get("/server.json")
    async def mcp_official_server_json(request: Request) -> dict[str, Any]:
        """Official Model Context Protocol Registry (`registry.modelcontextprotocol.io`) `server.json` manifest."""
        base_url = _derive_public_base_url(request, manager.pstn_adapter).rstrip("/")
        ns_name = os.environ.get("MCP_REGISTRY_NAMESPACE", "io.github.manavaga/ringscout").strip()
        return build_official_server_json(base_url, namespace_name=ns_name)

    # --- Four Cleanly Separated Frontend Surfaces ---

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def consumer_website_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/tracker/endpoints")
    async def get_endpoint_request_tracker(
        request: Request, window_hours: int = 168
    ) -> dict[str, Any]:
        """Return persistent Cloud-Logging-merged + real-time funnel (redacts transcripts & audio unless admin)."""
        manager.purge_expired_data()
        endpoint_tracker.purge_expired_in_memory()
        safe_hours = max(1, min(int(window_hours), 720))
        provided_pass = request.headers.get("x-ringscout-passcode") or request.query_params.get("passcode")
        is_admin = _check_admin_passcode_value(provided_pass)

        live_feed: list[dict[str, Any]] = []
        for q in reversed(list(manager.quests.values())[-25:]):
            if not is_admin and (q.quest_id.startswith("q_test_") or q.origin_channel == "admin_self_test"):
                continue
            call_rows = []
            for c in q.ranked_quotes():
                signed_audio = None
                if is_admin and c.audio_url:
                    sig = LiveIndiaPstnAdapter.sign_pstn_token("audio", q.quest_id, c.shop.shop_id)
                    signed_audio = f"{c.audio_url}?sig={sig}"
                raw_quote_dump = c.quote.model_dump() if c.quote else None
                call_rows.append(
                    {
                        "shop_id": c.shop.shop_id,
                        "name": c.shop.name,
                        "phone_display": c.shop.phone_display if is_admin else _mask_phone_for_public_dashboard(c.shop.phone_display),
                        "status": c.status.value,
                        "outcome": c.outcome.value,
                        "duration_sec": round(c.duration_sec, 1),
                        "quote": raw_quote_dump if is_admin else _public_safe_quote_dict(raw_quote_dump),
                        "audio_url": signed_audio,
                        "transcript": [t.model_dump() for t in c.transcript] if is_admin else [],
                    }
                )
            created_iso = datetime.datetime.fromtimestamp(
                q.created_at_epoch, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            live_feed.append(
                {
                    "raw_quest_id": q.quest_id,
                    "quest_id": q.quest_id if is_admin else f"q_••••{q.quest_id[-4:]}",
                    "created_at": created_iso,
                    "status": q.status,
                    "mode": q.mode.value,
                    "origin_channel": q.origin_channel or "web_consumer",
                    "caller_agent": q.caller_agent,
                    "city": q.city,
                    "locality": q.locality,
                    "user_goal": q.user_goal if is_admin else f"{q.place_category.value} check ({q.locality or q.city})",
                    "calls": call_rows,
                }
            )
        report = await endpoint_tracker.build_report(window_hours=safe_hours, live_quests=live_feed)
        for q_obj in report.get("live_quests", []):
            q_obj.pop("raw_quest_id", None)
        return report

    @app.get("/dashboard", include_in_schema=False)
    async def live_calls_dashboard_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.get("/tracker", include_in_schema=False)
    async def endpoint_tracker_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "tracker.html")

    @app.get("/developers", include_in_schema=False)
    async def developers_hub_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "developers.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_operator_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html")

    return app


app = create_app()
