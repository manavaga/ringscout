"""Agent-to-Agent (A2A v1.0) & x402 v2 Protocol Gateway for RingScout:
1. `GET /.well-known/agent-card.json` & `GET /.well-known/agent.json` — Official Google A2A v1.0 Agent Card
2. `POST /a2a` — Official A2A JSON-RPC 2.0 Task Server (`message/send`, `tasks/get`, `tasks/cancel`) gated by `x402`
3. `GET /.well-known/x402` & `GET /api/v1/x402/discovery` — `x402` v2 / Bazaar / `x402scan` Resource Discovery Manifest
4. `POST /api/v1/scout` — HTTP `402 Payment Required` Voice Oracle (`x402` v2 Base Mainnet + Base Sepolia USDC, `MPP`, `AP2`)
5. `POST /mcp` — Streamable HTTP Model Context Protocol (MCP) JSON-RPC 2.0 Server
"""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from typing import TYPE_CHECKING, Any
import uuid

from google.genai import types
import httpx
from pydantic import BaseModel, Field

from ringscout.carriers import LiveIndiaPstnAdapter
from ringscout.cloud_run_bridge import is_bridge_enabled, relay_quest_via_cloud_run
from ringscout.google_lookup import discover_indian_shops, infer_place_category
from ringscout.india_locations import INDIA_CITY_AREAS
from ringscout.models import (
    A2AScoutRequest,
    AP2SpendMandate,
    CallMode,
    CallOutcome,
    DiscoverRequest,
    PlaceCategory,
    QuestCreateRequest,
    VocalTone,
)
from ringscout.stripe_protocol import (
    GITHUB_REPO_URL,
    build_stripe_mpp_challenge,
    build_stripe_payment_receipt_header,
    check_and_increment_free_quota,
    check_and_increment_global_free_pstn_legs,
    check_and_increment_stargazer_quota,
    get_client_daily_used,
    get_effective_checkout_url,
    get_payment_gate_mode,
    get_star_gate_mode,
    is_automated_scanner_user_agent,
    masked_payment_status,
    verify_github_stargazer,
    verify_stripe_agent_payment,
)
from ringscout.voice_engine import (
    GCP_LOCATION,
    SIDE_BRAIN_MODEL,
    compile_conversational_call_plan,
    get_vertex_client,
)
from ringscout.x402_crypto import (
    generate_evm_keypair,
    sign_x402_exact_evm_payload,
    verify_x402_eip3009_signature_locally,
)

if TYPE_CHECKING:
    from ringscout.server import QuestManager

logger = logging.getLogger(__name__)

# Official Base Mainnet (`eip155:8453`) & Base Sepolia (`eip155:84532`) Circle USDC contracts
BASE_CAIP2_NETWORK = "eip155:8453"
BASE_USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_SEPOLIA_CAIP2_NETWORK = "eip155:84532"
BASE_SEPOLIA_USDC_CONTRACT = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

SCOUT_PRICE_USD = 0.75
SCOUT_PRICE_USD_STR = "0.75"
SCOUT_ATOMIC_USDC = "750000"  # 0.75 USDC (6 decimals)
DEFAULT_X402_FACILITATOR_VERIFY_URL = "https://www.x402.org/facilitator/verify"
DEFAULT_X402_FACILITATOR_SETTLE_URL = "https://www.x402.org/facilitator/settle"
DEFAULT_PAY_TO_WALLET = "0x9C8f2A6b4E10d35c7F89B2D14E60a53C8190b7E4"

# Ephemeral process demo buyer key (used only for authenticated UI/CLI EIP-3009 handshake demos)
_DEMO_BUYER_PRIV_HEX, _DEMO_BUYER_ADDRESS = generate_evm_keypair()


@dataclass
class PaymentSettlement:
    """Verified A2A / x402 payment & mandate metadata attached to the oracle receipt."""

    rail: str
    charged_usd: float
    payer: str
    network: str = BASE_CAIP2_NETWORK
    tx_hash: str | None = None
    ap2_mandate_id: str | None = None
    ap2_mandate_sha256: str | None = None
    sandbox: bool = False


class BoundedTaskStore(OrderedDict[str, dict[str, Any]]):
    """Bounded in-memory A2A Task store for `tasks/get` and `tasks/cancel`."""

    def __init__(self, max_entries: int = 128) -> None:
        super().__init__()
        self._max_entries = max_entries

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._max_entries:
            self.popitem(last=False)


A2A_TASK_STORE = BoundedTaskStore(max_entries=128)
SPENT_X402_NONCES: OrderedDict[str, float] = OrderedDict()


def _record_or_reject_x402_replay(network: str, payer: str, nonce_or_sig: str) -> bool:
    """Return True if fresh (and record in bounded replay cache), False if already spent."""
    key = f"{network.lower()}:{payer.lower()}:{nonce_or_sig.lower()}"
    if key in SPENT_X402_NONCES:
        return False
    SPENT_X402_NONCES[key] = time.time()
    while len(SPENT_X402_NONCES) > 2048:
        SPENT_X402_NONCES.popitem(last=False)
    return True


def configured_pay_to_wallet() -> str:
    return os.environ.get("X402_PAY_TO_ADDRESS", "").strip() or DEFAULT_PAY_TO_WALLET


def demo_buyer_wallet_address() -> str:
    return _DEMO_BUYER_ADDRESS


def create_signed_demo_x402_payload(network: str = BASE_SEPOLIA_CAIP2_NETWORK) -> dict[str, Any]:
    """Create a genuine secp256k1 EIP-712 `TransferWithAuthorization` (`EIP-3009`) x402 v2 payload."""
    reqs = build_x402_accepts_list()
    chosen = next((r for r in reqs if r["network"] == network), reqs[-1])
    return sign_x402_exact_evm_payload(
        buyer_private_key_hex=_DEMO_BUYER_PRIV_HEX,
        accepted_requirement=chosen,
    )


def _bazaar_io_schema() -> dict[str, Any]:
    """Coinbase Bazaar / `x402scan` machine-readable input/output schema metadata."""
    return {
        "input": {
            "type": "http",
            "method": "POST",
            "bodyType": "json",
            "bodyFields": {
                "query": {
                    "type": "string",
                    "required": False,
                    "description": "Google Maps category search in India (e.g. 'MRI scan center radiology hospital' or 'pharmacy chemist open now')",
                },
                "item_question": {
                    "type": "string",
                    "required": True,
                    "description": "Medicine, MRI/CT scan, or product availability + walk-in ₹ price question to ask by voice",
                },
                "city": {"type": "string", "required": False, "description": "Indian city (e.g. 'Mumbai', 'Bengaluru')"},
                "locality": {"type": "string", "required": False, "description": "Neighbourhood (e.g. 'Bandra West', 'Indiranagar')"},
                "preferred_tone": {
                    "type": "string",
                    "required": False,
                    "description": "'polite_indian_english' (en-IN) or 'conversational_hinglish' (hi-IN)",
                },
            },
        },
        "output": {
            "type": "object",
            "description": "Ranked ₹ INR quotes, stock/slot availability, transcripts, and signed 24kHz .wav audio proof URLs",
        },
    }


def build_x402_payment_requirements(
    pay_to: str | None = None,
    network: str = BASE_CAIP2_NETWORK,
) -> dict[str, Any]:
    target_wallet = pay_to or configured_pay_to_wallet()
    asset = (
        BASE_SEPOLIA_USDC_CONTRACT
        if network == BASE_SEPOLIA_CAIP2_NETWORK
        else BASE_USDC_CONTRACT
    )
    return {
        "scheme": "exact",
        "network": network,
        "asset": asset,
        "amount": SCOUT_ATOMIC_USDC,
        "payTo": target_wallet,
        "maxTimeoutSeconds": 120,
        "outputSchema": _bazaar_io_schema(),
        "extra": {
            "name": "USDC",
            "version": "2",
            "refundPolicy": "full_refund_if_zero_stores_connect",
        },
    }


def build_x402_accepts_list(pay_to: str | None = None) -> list[dict[str, Any]]:
    """Advertise both Base Mainnet (`eip155:8453`) and Base Sepolia (`eip155:84532`) USDC."""
    return [
        build_x402_payment_requirements(pay_to=pay_to, network=BASE_CAIP2_NETWORK),
        build_x402_payment_requirements(pay_to=pay_to, network=BASE_SEPOLIA_CAIP2_NETWORK),
    ]


def build_402_challenge(
    resource_url: str,
    detail: str = "Payment Required ($0.75 USD via Stripe MPP / SPT or x402 Base USDC)",
) -> tuple[dict[str, str], dict[str, Any]]:
    """Construct dual `Stripe MPP` (`WWW-Authenticate: Payment`) + `x402` v2 + `AP2` HTTP 402 challenge."""
    accepts = build_x402_accepts_list()
    mpp_ch = build_stripe_mpp_challenge(resource_url)
    checkout_url = get_effective_checkout_url()
    x402_envelope: dict[str, Any] = {
        "x402Version": 2,
        "error": detail,
        "resource": {
            "url": resource_url,
            "description": "Parallel en-IN / hi-IN voice insights across Google Maps hotels, restaurants, clinics, petrol stations, MRI centers & shops",
            "mimeType": "application/json",
        },
        "accepts": accepts,
        "extensions": {
            "bazaar": {
                "discoverable": True,
                "category": "real_world_voice_oracle",
                "country": "IN",
                "schema": _bazaar_io_schema(),
            }
        },
        "mpp": {
            "protocol": "mpp-1.0",
            "id": mpp_ch["id"],
            "realm": mpp_ch["realm"],
            "method": "stripe",
            "intent": "charge",
            "amount": SCOUT_PRICE_USD_STR,
            "currency": "USD",
            "request": mpp_ch["request"],
            "expires": mpp_ch["expires"],
            "methods": ["stripe_spt", "stripe_payment_intent", "ap2_mandate"],
            "checkoutUrl": checkout_url or None,
        },
        "ap2": {
            "protocol": "google-ap2-1.0",
            "required_min_amount_usd": SCOUT_PRICE_USD,
            "header": "X-AP2-Mandate",
        },
    }
    b64_required = base64.b64encode(
        json.dumps(x402_envelope, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    headers = {
        "PAYMENT-REQUIRED": b64_required,
        "X-PAYMENT-REQUIRED": b64_required,
        "WWW-Authenticate": mpp_ch["www_authenticate"],
    }
    return headers, x402_envelope


def build_payment_response_header(settlement: PaymentSettlement) -> str:
    """Encode `x402` v2 `PAYMENT-RESPONSE` Base64 receipt header on `200 OK`."""
    receipt = {
        "x402Version": 2,
        "success": True,
        "rail": settlement.rail,
        "network": settlement.network,
        "payer": settlement.payer,
        "transaction": settlement.tx_hash or "",
        "chargedUsd": settlement.charged_usd,
    }
    return base64.b64encode(
        json.dumps(receipt, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def build_payment_response_headers(settlement: PaymentSettlement) -> dict[str, str]:
    """Build HTTP settlement receipt headers (`PAYMENT-RESPONSE` + Stripe `Payment-Receipt` when on Stripe)."""
    pay_hdr = build_payment_response_header(settlement)
    headers = {
        "PAYMENT-RESPONSE": pay_hdr,
        "X-PAYMENT-RESPONSE": pay_hdr,
    }
    if settlement.network == "stripe:usd":
        headers["Payment-Receipt"] = build_stripe_payment_receipt_header(
            rail=settlement.rail,
            payer=settlement.payer,
            tx_or_pi=settlement.tx_hash,
            charged_usd=settlement.charged_usd,
        )
    return headers


def build_x402_discovery_manifest(base_url: str, live_pstn_ready: bool) -> dict[str, Any]:
    """Return `/.well-known/x402` & `/api/v1/x402/discovery` Bazaar / `x402scan` resource catalog."""
    clean = base_url.rstrip("/")
    return {
        "x402Version": 2,
        "service": "RingScout India +91 Voice Oracle",
        "payTo": configured_pay_to_wallet(),
        "facilitator": os.environ.get("X402_FACILITATOR_URL", DEFAULT_X402_FACILITATOR_VERIFY_URL),
        "liveIndiaPstnConfigured": live_pstn_ready,
        "a2aAgentCard": f"{clean}/.well-known/agent-card.json",
        "resources": [
            {
                "url": f"{clean}/api/v1/scout",
                "method": "POST",
                "protocol": "x402-http-rest",
                "priceUsd": SCOUT_PRICE_USD,
                "accepts": build_x402_accepts_list(),
            },
            {
                "url": f"{clean}/a2a",
                "method": "POST",
                "protocol": "google-a2a-1.0+x402-v2",
                "priceUsd": SCOUT_PRICE_USD,
                "accepts": build_x402_accepts_list(),
            },
            {
                "url": f"{clean}/mcp",
                "method": "POST",
                "protocol": "mcp-streamable-http+x402-v2",
                "priceUsd": SCOUT_PRICE_USD,
                "accepts": build_x402_accepts_list(),
            },
        ],
    }


def _parse_b64_or_json(raw: str) -> dict[str, Any] | None:
    txt = raw.strip()
    if not txt:
        return None
    if txt.startswith("{"):
        try:
            obj = json.loads(txt)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    try:
        padded = txt + "=" * (-len(txt) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        obj = json.loads(decoded)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def validate_ap2_mandate(
    body_mandate: AP2SpendMandate | None, header_mandate_raw: str | None
) -> tuple[AP2SpendMandate | None, str | None, str | None]:
    """Validate optional Google AP2 spend mandate; return `(mandate, sha256_prefix, error_msg)`."""
    mandate = body_mandate
    if mandate is None and header_mandate_raw:
        parsed = _parse_b64_or_json(header_mandate_raw)
        if not parsed:
            return None, None, "Malformed X-AP2-Mandate header"
        try:
            mandate = AP2SpendMandate.model_validate(parsed)
        except Exception as exc:
            return None, None, f"Invalid AP2 mandate schema: {exc}"

    if mandate is None:
        return None, None, None

    if mandate.max_amount_usd < SCOUT_PRICE_USD:
        return (
            None,
            None,
            f"AP2 mandate max_amount_usd (${mandate.max_amount_usd:.2f}) is below required ${SCOUT_PRICE_USD:.2f}",
        )
    if mandate.expires_at_epoch is not None and mandate.expires_at_epoch < time.time():
        return None, None, "AP2 spend mandate has expired"

    canonical = json.dumps(mandate.model_dump(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return mandate, digest, None


class PaymentRequiredError(Exception):
    """Raised when global free-tier PSTN circuit breaker or payment gate requires HTTP 402."""


async def verify_a2a_payment(
    *,
    resource_url: str,
    payment_sig_header: str | None,
    auth_header: str | None,
    sandbox_header: str | None,
    ap2_mandate: AP2SpendMandate | None,
    ap2_header_raw: str | None,
    owner_passcode_valid: bool,
    stripe_token_header: str | None = None,
    client_fingerprint: str = "default_agent",
    user_agent: str | None = None,
    execution_mode: str = "auto",
    github_username: str | None = None,
) -> tuple[PaymentSettlement | None, str]:
    """Verify Phase A open quota OR Phase B `x402` v2 EIP-3009 / `Stripe MPP` (`SPT`/`PaymentIntent`) / `AP2`."""
    mandate, mandate_digest, mandate_err = validate_ap2_mandate(ap2_mandate, ap2_header_raw)
    if mandate_err:
        return None, mandate_err

    # 1. Check x402 `PAYMENT-SIGNATURE` / `X-PAYMENT` header
    if payment_sig_header:
        payload_obj = _parse_b64_or_json(payment_sig_header)
        if not payload_obj:
            return None, "Malformed PAYMENT-SIGNATURE / X-PAYMENT header (expected Base64 JSON)"

        if owner_passcode_valid and payload_obj.get("sandbox") is True:
            payer = str(
                (payload_obj.get("payload") or {})
                .get("authorization", {})
                .get("from", "0xSandboxDemoAgent")
            )
            return (
                PaymentSettlement(
                    rail="x402-v2-sandbox",
                    charged_usd=SCOUT_PRICE_USD,
                    payer=payer,
                    network=str((payload_obj.get("accepted") or {}).get("network") or BASE_CAIP2_NETWORK),
                    ap2_mandate_id=mandate.mandate_id if mandate else None,
                    ap2_mandate_sha256=mandate_digest,
                    sandbox=True,
                ),
                "ok",
            )

        explicit_wallet = os.environ.get("X402_PAY_TO_ADDRESS", "").strip()
        accepted = payload_obj.get("accepted") or {}
        net = str(accepted.get("network") or BASE_CAIP2_NETWORK)
        if net not in (BASE_CAIP2_NETWORK, BASE_SEPOLIA_CAIP2_NETWORK):
            return None, f"Unsupported x402 network '{net}' (supported: {BASE_CAIP2_NETWORK}, {BASE_SEPOLIA_CAIP2_NETWORK})"

        testnet_allowed = owner_passcode_valid or (
            os.environ.get("X402_ALLOW_TESTNET", "").strip() == "1"
        )
        if net == BASE_SEPOLIA_CAIP2_NETWORK and not testnet_allowed:
            return (
                None,
                "Base Sepolia testnet (`eip155:84532`) requires owner passcode (or X402_ALLOW_TESTNET=1); unauthenticated callers must pay Base Mainnet USDC (`eip155:8453`).",
            )
        if net == BASE_CAIP2_NETWORK and not explicit_wallet:
            return (
                None,
                "Live x402 mainnet settlement requires X402_PAY_TO_ADDRESS configured on server.",
            )

        target_wallet = explicit_wallet or configured_pay_to_wallet()
        if str(accepted.get("payTo", "")).lower() != target_wallet.lower():
            return None, f"x402 payTo address mismatch (expected {target_wallet})"
        inner_payload = payload_obj.get("payload") or {}
        inner_auth = inner_payload.get("authorization") or {}
        if inner_auth.get("to") and str(inner_auth.get("to")).lower() != target_wallet.lower():
            return None, "x402 authorization.to does not match payTo wallet"
        try:
            if int(str(accepted.get("amount", "0"))) < int(SCOUT_ATOMIC_USDC):
                return None, "x402 atomic USDC amount below 750000 ($0.75)"
            if inner_auth.get("value") is not None and int(str(inner_auth["value"])) < int(SCOUT_ATOMIC_USDC):
                return None, "x402 authorization.value below 750000 ($0.75)"
        except ValueError:
            return None, "Invalid x402 amount"

        replay_token = str(
            inner_auth.get("nonce") or inner_payload.get("signature") or ""
        ).strip()
        claimed_from = str(inner_auth.get("from") or "unknown")
        if not replay_token:
            return None, "Missing x402 signature / nonce"

        req_for_verify = {
            "scheme": "exact",
            "network": net,
            "asset": (
                BASE_SEPOLIA_USDC_CONTRACT
                if net == BASE_SEPOLIA_CAIP2_NETWORK
                else BASE_USDC_CONTRACT
            ),
            "amount": SCOUT_ATOMIC_USDC,
            "payTo": target_wallet,
            "maxTimeoutSeconds": 120,
            "extra": {"name": "USDC", "version": "2"},
        }
        facilitator_url = os.environ.get(
            "X402_FACILITATOR_URL", DEFAULT_X402_FACILITATOR_VERIFY_URL
        ).strip()
        verify_body = {
            "x402Version": 2,
            "paymentPayload": payload_obj,
            "paymentRequirements": req_for_verify,
        }
        headers = {"Content-Type": "application/json"}
        cdp_token = os.environ.get("X402_FACILITATOR_BEARER_TOKEN", "").strip()
        if cdp_token:
            headers["Authorization"] = f"Bearer {cdp_token}"
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as http:
                resp = await http.post(facilitator_url, json=verify_body, headers=headers)
                resp.raise_for_status()
                ver = resp.json()

            if ver.get("isValid") is True or ver.get("valid") is True:
                payer = str(ver.get("payer") or claimed_from)
                tx_hash: str | None = None
                if os.environ.get("X402_ENABLE_AUTO_SETTLE", "").strip() == "1":
                    settle_url = os.environ.get(
                        "X402_FACILITATOR_SETTLE_URL", DEFAULT_X402_FACILITATOR_SETTLE_URL
                    ).strip()
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_s:
                        s_resp = await http_s.post(settle_url, json=verify_body, headers=headers)
                        s_resp.raise_for_status()
                        s_json = s_resp.json()
                        if s_json.get("success") is False:
                            return None, f"x402 settlement failed: {s_json.get('errorReason', 'settle_failed')}"
                        tx_hash = str(s_json.get("transaction") or s_json.get("txHash") or "") or None

                if not _record_or_reject_x402_replay(net, payer, replay_token):
                    return None, "x402 payment authorization nonce has already been spent (replay blocked)"

                rail_name = (
                    "x402-v2-base-sepolia-usdc"
                    if net == BASE_SEPOLIA_CAIP2_NETWORK
                    else "x402-v2-base-usdc"
                )
                return (
                    PaymentSettlement(
                        rail=rail_name,
                        charged_usd=SCOUT_PRICE_USD,
                        payer=payer,
                        network=net,
                        tx_hash=tx_hash,
                        ap2_mandate_id=mandate.mandate_id if mandate else None,
                        ap2_mandate_sha256=mandate_digest,
                        sandbox=False,
                    ),
                    "ok",
                )

            inv_reason = str(ver.get("invalidReason") or "invalid")
            if (
                owner_passcode_valid
                and net == BASE_SEPOLIA_CAIP2_NETWORK
                and inv_reason == "invalid_exact_evm_insufficient_balance"
            ):
                sig_ok, recovered_addr, _ = verify_x402_eip3009_signature_locally(payload_obj)
                if sig_ok and recovered_addr:
                    if not _record_or_reject_x402_replay(net, recovered_addr, replay_token):
                        return None, "x402 payment authorization nonce has already been spent (replay blocked)"
                    return (
                        PaymentSettlement(
                            rail="x402-v2-eip3009-sepolia-verified",
                            charged_usd=SCOUT_PRICE_USD,
                            payer=recovered_addr,
                            network=net,
                            ap2_mandate_id=mandate.mandate_id if mandate else None,
                            ap2_mandate_sha256=mandate_digest,
                            sandbox=True,
                        ),
                        "ok",
                    )

            return None, f"x402 facilitator rejected signature: {inv_reason}"
        except Exception as exc:
            logger.warning("x402 facilitator verification error: %s", exc)
            return None, "x402 facilitator verification failed"

    # 2. Check Stripe Protocol (`Authorization: Payment <b64url_mpp>` OR `X-Stripe-Payment-Token`)
    has_stripe_hdr = bool(
        (auth_header and auth_header.strip().lower().startswith("payment "))
        or (stripe_token_header and stripe_token_header.strip())
    )
    if has_stripe_hdr:
        stripe_ok, stripe_reason = await verify_stripe_agent_payment(
            auth_header=auth_header,
            stripe_token_header=stripe_token_header,
            owner_passcode_valid=owner_passcode_valid,
            resource_url=resource_url,
        )
        if stripe_ok is None:
            return None, stripe_reason
        s_rail, s_payer, s_pi, s_sandbox = stripe_ok
        return (
            PaymentSettlement(
                rail=s_rail,
                charged_usd=SCOUT_PRICE_USD,
                payer=s_payer,
                network="stripe:usd",
                tx_hash=s_pi,
                ap2_mandate_id=mandate.mandate_id if mandate else None,
                ap2_mandate_sha256=mandate_digest,
                sandbox=s_sandbox,
            ),
            "ok",
        )

    # 3. Explicit owner sandbox bypass (`X-Sandbox-Payment: simulate` with valid passcode)
    if owner_passcode_valid and (sandbox_header or "").strip().lower() == "simulate":
        return (
            PaymentSettlement(
                rail="owner-passcode-sandbox",
                charged_usd=0.0,
                payer="owner_passcode",
                ap2_mandate_id=mandate.mandate_id if mandate else None,
                ap2_mandate_sha256=mandate_digest,
                sandbox=True,
            ),
            "ok",
        )

    # 4. Phase A Open Out-of-the-Box Mode (`gate_mode == "off"`) or Phase B Freemium Quota (`gate_mode == "freemium"`)
    gate_mode = get_payment_gate_mode()
    if gate_mode in ("off", "freemium"):
        if owner_passcode_valid:
            return (
                PaymentSettlement(
                    rail="owner-passcode-direct",
                    charged_usd=0.0,
                    payer="owner_passcode",
                    network="ringscout:owner",
                    ap2_mandate_id=mandate.mandate_id if mandate else None,
                    ap2_mandate_sha256=mandate_digest,
                    sandbox=False,
                ),
                "ok",
            )
        if execution_mode != "swarm":
            if is_automated_scanner_user_agent(user_agent):
                return (
                    None,
                    "Automated directory scanner detected: attach Stripe MPP (`Authorization: Payment`) or x402 (`PAYMENT-SIGNATURE`) to place real-time +91 PSTN phone calls.",
                )
            if os.environ.get("RINGSCOUT_ALLOW_FREE_LIVE_PSTN", "1").strip() == "0":
                return (
                    None,
                    "Payment Required ($0.75 USD via Stripe MPP / SPT or x402 Base USDC) to place real-time +91 PSTN phone calls.",
                )

        # Optional GitHub Star-Gate ("after_first_call" = 1 free call then star; "strict" = star required from call 1)
        star_mode = get_star_gate_mode()
        if execution_mode != "swarm" and star_mode in ("after_first_call", "strict"):
            used_today = get_client_daily_used(client_fingerprint)
            needs_star = (star_mode == "strict") or (used_today >= 1)
            if needs_star:
                clean_gh = (github_username or "").strip().lstrip("@")
                if not clean_gh or not await verify_github_stargazer(clean_gh):
                    return (
                        None,
                        f"Star {GITHUB_REPO_URL} on GitHub and pass your `github_username` (or `X-GitHub-Username` header) to unlock free live +91 phone calls, or attach $0.75 Stripe MPP / x402 payment.",
                    )
                gh_ok, _gh_used, gh_limit = check_and_increment_stargazer_quota(clean_gh)
                if not gh_ok:
                    return (
                        None,
                        f"Daily GitHub Stargazer free quota ({gh_limit} calls/day for @{clean_gh}) reached; attach Stripe MPP or x402 payment header to continue.",
                    )

        within_quota, _used, limit = check_and_increment_free_quota(client_fingerprint)
        if within_quota:
            return (
                PaymentSettlement(
                    rail="open-a2a-phase-a" if gate_mode == "off" else "freemium-free-tier",
                    charged_usd=0.0,
                    payer=client_fingerprint,
                    network="ringscout:free-tier",
                    ap2_mandate_id=mandate.mandate_id if mandate else None,
                    ap2_mandate_sha256=mandate_digest,
                    sandbox=True,
                ),
                "ok",
            )
        if gate_mode == "off":
            return (
                None,
                f"Daily free out-of-the-box safety cap ({limit} scouts/day) reached; attach Stripe MPP or x402 payment header to continue.",
            )

    return (
        None,
        "Payment Required ($0.75 USD via Stripe `Authorization: Payment` / `X-Stripe-Payment-Token` or x402 `PAYMENT-SIGNATURE`)",
    )


def build_agent_card(base_url: str, live_pstn_ready: bool) -> dict[str, Any]:
    """Build official Google A2A v1.0 Agent Card (`/.well-known/agent-card.json` & `/.well-known/agent.json`)."""
    clean_base = base_url.rstrip("/")
    pay_wallet = configured_pay_to_wallet()
    pay_state = masked_payment_status()
    return {
        "protocolVersion": "1.0",
        "name": "RingScout — Real-time local voice checks",
        "description": (
            "RingScout finds local businesses on Google Maps and calls them directly in India (Indian English, "
            "Hindi, or Hinglish) to verify what's happening right now—live restaurant tables & wait times, hotel "
            "rooms & direct tariffs, salon/spa appointments, clinic/diagnostic slots & charges, petrol pump fuel/services, "
            "and pharmacy or auto shop availability. Contacts up to five businesses in parallel and returns verified "
            "responses, prices in ₹, key details, and recorded call audio when available. RingScout provides "
            "information; it does not itself make reservations, purchases, or appointments."
        ),
        "url": f"{clean_base}/a2a",
        "version": "3.0.0",
        "provider": {
            "organization": "RingScout",
            "url": clean_base,
        },
        "defaultInputModes": ["text", "application/json"],
        "defaultOutputModes": ["text", "application/json", "audio/wav"],
        "supportedInterfaces": [
            {
                "url": f"{clean_base}/a2a",
                "protocolBinding": "JSON-RPC",
                "protocolVersion": "1.0",
            },
            {
                "url": f"{clean_base}/mcp",
                "protocolBinding": "MCP-Streamable-HTTP",
                "protocolVersion": "2025-06-18",
            },
            {
                "url": f"{clean_base}/openapi-actions.json",
                "protocolBinding": "OpenAPI-3.1-Actions",
                "protocolVersion": "3.1.0",
            },
            {
                "url": f"{clean_base}/api/v1/scout",
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "x402-v2+stripe-mpp-1.0",
            },
        ],
        "endpoints": {
            "a2a_jsonrpc": f"{clean_base}/a2a",
            "mcp": f"{clean_base}/mcp",
            "mcp_sse_legacy": f"{clean_base}/sse",
            "mcp_server_card": f"{clean_base}/.well-known/mcp/server-card.json",
            "mcp_registry_card": f"{clean_base}/.well-known/mcp.json",
            "official_mcp_server_json": f"{clean_base}/server.json",
            "ai_catalog": f"{clean_base}/.well-known/ai-catalog.json",
            "chatgpt_openapi_actions": f"{clean_base}/openapi-actions.json",
            "llms_txt": f"{clean_base}/llms.txt",
            "health_ping": f"{clean_base}/ping",
            "x402_scout": f"{clean_base}/api/v1/scout",
            "x402_discovery": f"{clean_base}/.well-known/x402",
            "pricing": f"{clean_base}/api/v1/pricing",
        },
        "protocols": [
            "a2a-1.0",
            "mcp-streamable-http",
            "chatgpt-openapi-actions-3.1",
            "stripe-mpp-1.0",
            "x402-v2",
            "google-ap2-1.0",
        ],
        "payment": {
            "gate_mode": pay_state["gate_mode"],
            "free_daily_calls": pay_state["free_daily_calls_per_client"],
            "price_usd": SCOUT_PRICE_USD,
            "supported_rails": ["free_phase_a", "stripe_mpp_spt", "x402_base_usdc"],
            "stripe_mode": pay_state["stripe_mode"],
            "x402_network": BASE_CAIP2_NETWORK,
            "x402_testnet_network": BASE_SEPOLIA_CAIP2_NETWORK,
            "x402_asset": BASE_USDC_CONTRACT,
            "x402_testnet_asset": BASE_SEPOLIA_USDC_CONTRACT,
            "x402_atomic_amount": SCOUT_ATOMIC_USDC,
            "pay_to": pay_wallet,
            "facilitator": os.environ.get("X402_FACILITATOR_URL", DEFAULT_X402_FACILITATOR_VERIFY_URL),
            "guarantee": "Full refund / zero charge if 0 places answer",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "liveIndiaPstnConfigured": live_pstn_ready,
            "supportedCategories": [c.value for c in PlaceCategory],
            "languages": ["en-IN (Indian English)", "hi-IN (Hindi / Hinglish)"],
            "maxParallelCalls": 5,
        },
        "skills": [
            {
                "id": "scout_places_on_google_maps",
                "name": "Real-time local voice checks on Google Maps",
                "description": (
                    "Finds local businesses on Google Maps in India (restaurants, hotels, salons, spas, clinics, "
                    "diagnostic centres, petrol pumps, pharmacies, auto shops) and calls up to five businesses in "
                    "parallel in Indian English, Hindi, or Hinglish to verify live availability, wait times, and "
                    "prices in ₹, with recorded call audio when available. Results depend on businesses being "
                    "reachable and answering; RingScout provides information and does not make reservations, "
                    "purchases, or appointments."
                ),
                "tags": ["google-maps", "voice-ai", "hotels", "restaurants", "spas", "clinics", "petrol", "mri", "pharmacy", "mcp", "a2a"],
                "inputModes": ["text", "application/json"],
                "outputModes": ["text", "application/json", "audio/wav"],
                "inputSchema": _SCOUT_MCP_INPUT_SCHEMA,
                "examples": [
                    "Call Bastian in Bandra West, Mumbai and ask if a table for 4 is available tonight at 8:30 PM.",
                    "Call top 3 hotels in Bandra West, Mumbai to check room availability tonight, 10 AM early check-in, and direct phone tariff in ₹.",
                    "Check which petrol pump in Bandra West has XP95 petrol and nitrogen air working right now.",
                ],
            }
        ],
    }


async def execute_a2a_scout(
    req: A2AScoutRequest,
    manager: "QuestManager",
    base_url: str,
    public_wss_ready: bool,
    settlement: PaymentSettlement,
    owner_passcode_valid: bool = False,
    origin_channel: str = "api_scout",
    caller_agent: str = "External AI Agent",
) -> dict[str, Any]:
    """Run end-to-end Google Maps discovery + real-time +91 PSTN voice calls and return verifiable A2A JSON."""
    paid_or_owner = owner_passcode_valid or (
        not settlement.sandbox and settlement.charged_usd > 0
    )
    free_live_pstn_enabled = (
        os.environ.get("RINGSCOUT_ALLOW_FREE_LIVE_PSTN", "1").strip() != "0"
    )
    auto_live_pstn_enabled = (
        os.environ.get("A2A_AUTO_LIVE_PSTN", "1").strip() != "0"
    )
    pstn_infra_ready = manager.pstn_adapter.is_configured() and public_wss_ready

    use_live_pstn = False
    if req.execution_mode == "swarm":
        use_live_pstn = False
    elif req.execution_mode == "live_pstn":
        if not paid_or_owner and not free_live_pstn_enabled:
            raise ValueError(
                "execution_mode='live_pstn' requires verified Stripe MPP / x402 payment or owner passcode."
            )
        if not pstn_infra_ready:
            raise ValueError(
                "execution_mode='live_pstn' requires active PSTN carrier credentials and public wss:// reachability."
            )
        use_live_pstn = True
    else:
        # Default execution_mode == "auto": ALWAYS dial real-time +91 PSTN lines whenever carrier is configured!
        if pstn_infra_ready and auto_live_pstn_enabled:
            if not paid_or_owner and not free_live_pstn_enabled:
                raise PaymentRequiredError(
                    "Real-time +91 PSTN calls require verified Stripe MPP / x402 payment or owner passcode."
                )
            use_live_pstn = True
        elif os.environ.get("PYTEST_CURRENT_TEST") or not auto_live_pstn_enabled:
            use_live_pstn = False
        else:
            raise ValueError(
                "Live +91 PSTN carrier is not configured on this server; refusing to return simulated audio for a live call request."
            )

    eff_max_shops = (
        min(req.max_shops, 2) if (use_live_pstn and not paid_or_owner) else req.max_shops
    )

    disc = await discover_indian_shops(
        DiscoverRequest(
            query=req.query,
            place_category=req.place_category,
            purpose=req.item_question,
            specific_place=req.specific_place,
            city=req.city,
            locality=req.locality,
            max_shops=eff_max_shops,
        )
    )
    manager.register_discovered_shops(disc)
    if not disc.shops:
        return {
            "status": "no_open_shops_found",
            "place_category": disc.place_category.value,
            "execution_mode": CallMode.LIVE_PSTN.value if use_live_pstn else CallMode.HINGLISH_SWARM.value,
            "is_live_pstn": use_live_pstn,
            "simulated_sandbox": not use_live_pstn,
            "settlement": {
                "rail": settlement.rail,
                "network": settlement.network,
                "charged_usd": 0.0,
                "refund_eligible": True,
                "reason": "0 open +91 listings matched on Google Maps",
                "ap2_mandate_id": settlement.ap2_mandate_id,
                "ap2_mandate_sha256": settlement.ap2_mandate_sha256,
            },
            "best_match": None,
            "ranked_quotes": [],
        }

    selected_shops = disc.shops[:eff_max_shops]
    if use_live_pstn and not paid_or_owner:
        within_global, _used_legs, global_cap = check_and_increment_global_free_pstn_legs(
            len(selected_shops)
        )
        if not within_global:
            raise PaymentRequiredError(
                f"Global daily free live-PSTN safety cap ({global_cap} phone legs/day) reached; "
                "attach Stripe MPP (`Authorization: Payment`) or x402 (`PAYMENT-SIGNATURE`) to continue."
            )

    mode = CallMode.LIVE_PSTN if use_live_pstn else CallMode.HINGLISH_SWARM
    quest = manager.create_quest(
        QuestCreateRequest(
            user_goal=req.item_question,
            place_category=disc.place_category,
            caller_name=req.caller_name,
            city=req.city,
            locality=req.locality,
            preferred_tone=req.preferred_tone,
            mode=mode,
            origin_channel=origin_channel,
            caller_agent=caller_agent,
            shops=selected_shops,
        )
    )
    try:
        quest.call_plan = await compile_conversational_call_plan(
            req.item_question, req.preferred_tone, disc.place_category
        )
    except TypeError:
        quest.call_plan = await compile_conversational_call_plan(
            req.item_question, req.preferred_tone
        )
    stop_ev = manager.stop_events[quest.quest_id]

    if mode == CallMode.HINGLISH_SWARM:
        relayed = False
        if is_bridge_enabled():
            relayed = await relay_quest_via_cloud_run(
                quest=quest,
                mode_str="hinglish_swarm",
                audio_store=manager.audio_store,
                publish=manager.publish,
                stop_event=stop_ev,
            )
        if not relayed:
            await manager.swarm_adapter.run_quest(
                quest=quest,
                stop_event=stop_ev,
                publish=manager.publish,
            )
    else:
        if is_bridge_enabled():
            relayed_pstn = await relay_quest_via_cloud_run(
                quest=quest,
                mode_str="live_pstn",
                audio_store=manager.audio_store,
                publish=manager.publish,
                stop_event=stop_ev,
                pstn_adapter=manager.pstn_adapter,
            )
            deadline = time.monotonic() if relayed_pstn else (time.monotonic() + 165.0)
        else:
            deadline = time.monotonic() + 165.0
        if quest.status == "running" and deadline > time.monotonic():
            for idx, shop in enumerate(quest.shops):
                if stop_ev.is_set() or quest.status != "running":
                    break
                if idx > 0:
                    await asyncio.sleep(random.uniform(1.8, 3.2))
                asyncio.create_task(
                    manager.pstn_adapter.place_india_call(
                        quest=quest,
                        shop=shop,
                        public_base_url=base_url,
                        publish=manager.publish,
                    )
                )
            while quest.status == "running" and time.monotonic() < deadline:
                await asyncio.sleep(0.4)
            if quest.status == "running":
                await manager.stop_quest(quest.quest_id)

    ranked = quest.ranked_quotes()
    clean_base = base_url.rstrip("/")
    connected_outcomes = {
        CallOutcome.CONNECTED_QUOTE,
        CallOutcome.CONNECTED_NO_STOCK,
        CallOutcome.CONNECTED_INCONCLUSIVE,
        CallOutcome.IVR_NAVIGATED,
    }
    connected_count = sum(1 for c in ranked if c.outcome in connected_outcomes)
    is_real_pstn = mode == CallMode.LIVE_PSTN

    serialized_quotes: list[dict[str, Any]] = []
    for c in ranked:
        audio_proof_url = None
        if c.audio_url:
            audio_sig = LiveIndiaPstnAdapter.sign_pstn_token(
                "audio", quest.quest_id, c.shop.shop_id
            )
            audio_proof_url = f"{clean_base}{c.audio_url}?sig={audio_sig}"
        serialized_quotes.append(
            {
                "shop_id": c.shop.shop_id,
                "name": c.shop.name,
                "phone_e164": c.shop.phone_e164,
                "phone_display": c.shop.phone_display,
                "address": c.shop.address,
                "locality_badge": c.shop.locality_badge,
                "open_status": c.shop.open_status,
                "rating": c.shop.rating,
                "maps_uri": c.shop.maps_uri,
                "is_live_pstn": is_real_pstn,
                "carrier_provider": c.carrier_provider,
                "call_uuid": c.twilio_call_sid,
                "call_outcome": c.outcome.value,
                "dtmf_sent": c.dtmf_sent,
                "duration_sec": c.duration_sec,
                "in_stock": c.quote.in_stock if c.quote else None,
                "availability_label": c.quote.availability_label if c.quote else "",
                "price_inr": c.quote.price_inr if c.quote else None,
                "price_display": c.quote.price_display if c.quote else None,
                "unit_or_pack": c.quote.unit_or_pack if c.quote else None,
                "eta_or_slot": c.quote.eta_or_slot if c.quote else None,
                "key_insights": list(c.quote.key_insights) if c.quote else [],
                "notes": c.quote.notes if c.quote else "",
                "audio_proof_url": audio_proof_url,
                "transcript": [t.model_dump() for t in c.transcript],
            }
        )

    best_match = next(
        (q for q in serialized_quotes if q["in_stock"] is True),
        None,
    )
    effective_charge = settlement.charged_usd if connected_count > 0 else 0.0

    return {
        "quest_id": quest.quest_id,
        "place_category": disc.place_category.value,
        "execution_mode": mode.value,
        "is_live_pstn": is_real_pstn,
        "simulated_sandbox": not is_real_pstn,
        "call_plan": quest.call_plan.model_dump() if quest.call_plan else None,
        "settlement": {
            "rail": settlement.rail,
            "network": settlement.network,
            "payer": settlement.payer,
            "tx_hash": settlement.tx_hash,
            "charged_usd": effective_charge,
            "stores_dialed": len(quest.shops),
            "stores_connected": connected_count,
            "refund_eligible": connected_count == 0,
            "ap2_mandate_id": settlement.ap2_mandate_id,
            "ap2_mandate_sha256": settlement.ap2_mandate_sha256,
            "sandbox": settlement.sandbox,
        },
        "best_match": best_match,
        "ranked_quotes": serialized_quotes,
    }


# --- Official Google A2A v1.0 JSON-RPC (`POST /a2a`: `message/send`, `tasks/get`, `tasks/cancel`) ---


class _ParsedA2AIntent(BaseModel):
    place_category: PlaceCategory = PlaceCategory.CUSTOM
    query: str = Field(default="")
    item_question: str
    specific_place: str = Field(default="")
    city: str = Field(default="Mumbai")
    locality: str = Field(default="Bandra West")
    max_shops: int = Field(default=3, ge=1, le=5)
    preferred_tone: VocalTone = VocalTone.POLITE_INDIAN_ENGLISH


_CATEGORY_DEFAULT_QUERY: dict[PlaceCategory, tuple[str, VocalTone]] = {
    PlaceCategory.HOTEL: ("hotels boutique stays open now", VocalTone.POLITE_INDIAN_ENGLISH),
    PlaceCategory.RESTAURANT: ("restaurants cafes dining open now", VocalTone.POLITE_INDIAN_ENGLISH),
    PlaceCategory.CLINIC: ("doctor clinics outpatient medical centers open now", VocalTone.POLITE_INDIAN_ENGLISH),
    PlaceCategory.DIAGNOSTICS: ("MRI scan center radiology imaging hospital", VocalTone.POLITE_INDIAN_ENGLISH),
    PlaceCategory.PETROL: ("petrol pump fuel station EV charging open now", VocalTone.CONVERSATIONAL_HINGLISH),
    PlaceCategory.AUTO: ("car tyre dealer wheel alignment auto service shop", VocalTone.CONVERSATIONAL_HINGLISH),
    PlaceCategory.SPA: ("day spa body massage thai spa wellness spa open now", VocalTone.POLITE_INDIAN_ENGLISH),
    PlaceCategory.SALON: ("hair beauty salon grooming studio open now", VocalTone.POLITE_INDIAN_ENGLISH),
    PlaceCategory.PHARMACY: ("pharmacy medical store chemist open now", VocalTone.CONVERSATIONAL_HINGLISH),
    PlaceCategory.CUSTOM: ("open local places on Google Maps", VocalTone.POLITE_INDIAN_ENGLISH),
}


def _heuristic_parse_a2a_text(text: str) -> A2AScoutRequest:
    low = text.lower()
    if any(k in low for k in ("mumbai", "bandra", "khar", "andheri", "dadar", "juhu", "colaba", "powai")):
        city = "Mumbai"
        locality = "Juhu" if "juhu" in low else ("Andheri West" if "andheri" in low else "Bandra West")
    elif any(k in low for k in ("delhi", "connaught", "saket", "hauz khas", "gurgaon", "gurugram")):
        city = "New Delhi"
        locality = "Connaught Place"
    else:
        city = "Bengaluru"
        locality = "HSR Layout" if "hsr" in low else ("Koramangala" if "koramangala" in low else "Indiranagar")

    cat = infer_place_category("", text, PlaceCategory.CUSTOM)
    if cat == PlaceCategory.CUSTOM:
        cat = PlaceCategory.PHARMACY
    query, tone = _CATEGORY_DEFAULT_QUERY[cat]
    return A2AScoutRequest(
        place_category=cat,
        query=query,
        item_question=text.strip()[:480] or "Check live availability and price in ₹",
        city=city,
        locality=locality,
        max_shops=3,
        preferred_tone=tone,
        execution_mode="auto",
    )


async def parse_a2a_message_to_scout_request(
    message_obj: dict[str, Any], metadata: dict[str, Any]
) -> A2AScoutRequest:
    """Convert an A2A `message` (`parts` with `text` or `data`) into an `A2AScoutRequest`."""
    parts = message_obj.get("parts") or []
    data_dict: dict[str, Any] = {}
    text_chunks: list[str] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        p_kind = str(part.get("kind") or part.get("type") or "")
        if p_kind == "data" and isinstance(part.get("data"), dict):
            data_dict.update(part["data"])
        elif part.get("text"):
            raw_t = str(part["text"]).strip()
            if raw_t.startswith("{") and raw_t.endswith("}"):
                try:
                    parsed_j = json.loads(raw_t)
                    if isinstance(parsed_j, dict) and (
                        "item_question" in parsed_j or "purpose" in parsed_j or "query" in parsed_j
                    ):
                        data_dict.update(parsed_j)
                        continue
                except json.JSONDecodeError:
                    pass
            text_chunks.append(raw_t)

    exec_mode = str(
        data_dict.get("execution_mode") or metadata.get("execution_mode") or "auto"
    )
    if exec_mode not in ("auto", "live_pstn", "swarm"):
        exec_mode = "auto"

    ap2_obj: AP2SpendMandate | None = None
    raw_mandate = data_dict.get("ap2_mandate") or metadata.get("ap2Mandate") or metadata.get("ap2_mandate")
    if isinstance(raw_mandate, dict):
        ap2_obj = AP2SpendMandate.model_validate(raw_mandate)

    eff_question = str(data_dict.get("item_question") or data_dict.get("purpose") or "").strip()
    if eff_question:
        raw_cat = str(data_dict.get("place_category") or "custom").lower()
        cat_enum = (
            PlaceCategory(raw_cat)
            if raw_cat in {c.value for c in PlaceCategory}
            else infer_place_category(str(data_dict.get("query") or ""), eff_question)
        )
        return A2AScoutRequest(
            place_category=cat_enum,
            query=str(data_dict.get("query") or ""),
            item_question=eff_question,
            specific_place=str(data_dict.get("specific_place") or ""),
            caller_name=str(data_dict.get("caller_name") or ""),
            city=str(data_dict.get("city") or "Bengaluru"),
            locality=str(data_dict.get("locality") or "Indiranagar"),
            max_shops=min(max(int(data_dict.get("max_shops") or 3), 1), 10),
            preferred_tone=VocalTone(
                str(data_dict.get("preferred_tone") or VocalTone.POLITE_INDIAN_ENGLISH.value)
            ),
            execution_mode=exec_mode,  # type: ignore[arg-type]
            ap2_mandate=ap2_obj,
        )

    joined_text = " ".join(text_chunks).strip()
    if not joined_text:
        raise ValueError("A2A message must contain a non-empty text or structured data part.")

    fallback_req = _heuristic_parse_a2a_text(joined_text)
    fallback_req.execution_mode = exec_mode  # type: ignore[assignment]
    fallback_req.ap2_mandate = ap2_obj

    if os.environ.get("PYTEST_CURRENT_TEST"):
        if metadata.get("max_shops"):
            fallback_req.max_shops = min(max(int(metadata["max_shops"]), 1), 10)
        return fallback_req

    try:
        client = get_vertex_client(GCP_LOCATION)
        resp = await client.aio.models.generate_content(
            model=SIDE_BRAIN_MODEL,
            contents=(
                "Extract structured Google Maps voice-scouting fields from this Agent2Agent request:\n"
                f"{joined_text!r}\n"
                "Choose `place_category` ('hotel', 'restaurant', 'clinic', 'diagnostics', 'petrol', "
                "'auto', 'salon', 'pharmacy', or 'custom'), `query` (concise Google Maps search phrase), "
                "`specific_place` (only if one exact named venue was specified), `city`, `locality`, "
                "`max_shops` (1-5, default 3), and `preferred_tone` ('polite_indian_english' or 'conversational_hinglish')."
            ),
            config=types.GenerateContentConfig(
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
                response_schema=_ParsedA2AIntent,
            ),
        )
        if resp.text:
            parsed = _ParsedA2AIntent.model_validate_json(resp.text)
            return A2AScoutRequest(
                place_category=parsed.place_category,
                query=parsed.query,
                item_question=parsed.item_question,
                specific_place=parsed.specific_place,
                city=parsed.city,
                locality=parsed.locality,
                max_shops=int(metadata.get("max_shops") or parsed.max_shops),
                preferred_tone=parsed.preferred_tone,
                execution_mode=exec_mode,  # type: ignore[arg-type]
                ap2_mandate=ap2_obj,
            )
    except Exception as exc:
        logger.info("A2A message parser using deterministic fallback (%s)", exc)

    if metadata.get("max_shops"):
        fallback_req.max_shops = min(max(int(metadata["max_shops"]), 1), 10)
    return fallback_req


def _peek_a2a_execution_mode(message_obj: dict[str, Any], metadata: dict[str, Any]) -> str:
    if metadata.get("execution_mode") in ("auto", "live_pstn", "swarm"):
        return str(metadata["execution_mode"])
    for part in message_obj.get("parts") or []:
        if isinstance(part, dict):
            data = part.get("data")
            if isinstance(data, dict) and data.get("execution_mode") in ("auto", "live_pstn", "swarm"):
                return str(data["execution_mode"])
    return "auto"


def _format_a2a_summary_text(scout_result: dict[str, Any]) -> str:
    best = scout_result.get("best_match")
    quotes = scout_result.get("ranked_quotes") or []
    stl = scout_result.get("settlement") or {}
    mode_lbl = scout_result.get("execution_mode")
    prefix = "[SIMULATED SANDBOX — NO PHONE DIALED] " if scout_result.get("simulated_sandbox") else ""
    lines = [
        f"{prefix}RingScout completed {len(quotes)} parallel Google Maps (+91) calls via `{mode_lbl}` "
        f"(rail: `{stl.get('rail')}`, charged: `${stl.get('charged_usd', 0):.2f}`)."
    ]
    if best:
        lines.append(
            f"Best Available Match: {best['name']} ({best['phone_display']}) — "
            f"{best.get('availability_label') or 'AVAILABLE'} · {best.get('price_display') or ''} {best.get('eta_or_slot') or ''}".strip()
        )
    for idx, q in enumerate(quotes, 1):
        avail_lbl = q.get("availability_label") or ("AVAILABLE" if q.get("in_stock") is True else "UNAVAILABLE")
        status_str = (
            f"{avail_lbl} · {q.get('price_display') or '₹ TBD'}"
            if q.get("in_stock") is True
            else (avail_lbl if q.get("in_stock") is False else q.get("call_outcome", "inconclusive"))
        )
        extra = f" ({q['eta_or_slot']})" if q.get("eta_or_slot") else ""
        lines.append(f"{idx}. {q['name']} ({q['phone_display']}): {status_str}{extra}")
    return "\n".join(lines)


async def handle_a2a_jsonrpc(
    rpc_body: dict[str, Any],
    *,
    manager: "QuestManager",
    base_url: str,
    public_wss_ready: bool,
    payment_sig_header: str | None,
    auth_header: str | None,
    sandbox_header: str | None,
    ap2_header_raw: str | None,
    owner_passcode_valid: bool,
    stripe_token_header: str | None = None,
    client_fingerprint: str = "a2a_agent",
    user_agent: str | None = None,
    github_username_header: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Handle official Google A2A v1.0 JSON-RPC (`message/send`, `tasks/get`, `tasks/cancel`)."""
    method = str(rpc_body.get("method") or "")
    req_id = rpc_body.get("id")
    params = rpc_body.get("params") if isinstance(rpc_body.get("params"), dict) else {}
    resource_url = f"{base_url.rstrip('/')}/a2a"

    if method == "tasks/get":
        task_id = str(params.get("id") or params.get("taskId") or "")
        task_obj = A2A_TASK_STORE.get(task_id)
        if not task_obj:
            return (
                200,
                {},
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32001, "message": f"Task not found: {task_id}"},
                },
            )
        return 200, {}, {"jsonrpc": "2.0", "id": req_id, "result": {"task": task_obj}}

    if method == "tasks/cancel":
        task_id = str(params.get("id") or params.get("taskId") or "")
        task_obj = A2A_TASK_STORE.get(task_id)
        if not task_obj:
            return (
                200,
                {},
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32001, "message": f"Task not found: {task_id}"},
                },
            )
        qid = str(task_obj.get("metadata", {}).get("questId") or "")
        if qid and qid in manager.quests:
            await manager.stop_quest(qid)
        task_obj["status"] = {"state": "CANCELED"}
        return 200, {}, {"jsonrpc": "2.0", "id": req_id, "result": {"task": task_obj}}

    if method in ("message/send", "tasks/send"):
        message_obj = params.get("message") if isinstance(params.get("message"), dict) else {}
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        eff_sig = payment_sig_header or str(metadata.get("x402PaymentSignature") or "").strip() or None
        eff_stripe_tok = stripe_token_header or str(metadata.get("stripePaymentToken") or "").strip() or None
        eff_auth = auth_header or (
            f"Payment {metadata['stripeMppCredential']}"
            if metadata.get("stripeMppCredential")
            else None
        )

        ap2_from_meta: AP2SpendMandate | None = None
        raw_m = metadata.get("ap2Mandate") or metadata.get("ap2_mandate")
        if isinstance(raw_m, dict):
            try:
                ap2_from_meta = AP2SpendMandate.model_validate(raw_m)
            except Exception as exc:
                hdrs, body = build_402_challenge(resource_url, detail=f"Invalid AP2 mandate: {exc}")
                return (
                    402,
                    hdrs,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": 402, "message": str(exc), "data": body},
                    },
                )

        peek_mode = _peek_a2a_execution_mode(message_obj, metadata)
        eff_gh_user = (
            github_username_header
            or str(metadata.get("githubUsername") or metadata.get("github_username") or "").strip()
            or None
        )
        if not eff_gh_user:
            for part in message_obj.get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("data"), dict):
                    cand_gh = str(part["data"].get("github_username") or "").strip()
                    if cand_gh:
                        eff_gh_user = cand_gh
                        break
        settlement, reason = await verify_a2a_payment(
            resource_url=resource_url,
            payment_sig_header=eff_sig,
            auth_header=eff_auth,
            sandbox_header=sandbox_header,
            ap2_mandate=ap2_from_meta,
            ap2_header_raw=ap2_header_raw,
            owner_passcode_valid=owner_passcode_valid,
            stripe_token_header=eff_stripe_tok,
            client_fingerprint=client_fingerprint,
            user_agent=user_agent,
            execution_mode=peek_mode,
            github_username=eff_gh_user,
        )
        if settlement is None:
            hdrs, challenge_body = build_402_challenge(resource_url, detail=reason)
            task_id = f"task_{uuid.uuid4().hex[:10]}"
            pending_task = {
                "id": task_id,
                "taskId": task_id,
                "status": {
                    "state": "INPUT_REQUIRED",
                    "message": {
                        "messageId": f"msg_{uuid.uuid4().hex[:8]}",
                        "role": "agent",
                        "parts": [
                            {"kind": "text", "text": reason},
                            {"kind": "data", "data": challenge_body},
                        ],
                    },
                },
            }
            return (
                402,
                hdrs,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": 402,
                        "message": reason,
                        "data": {"x402": challenge_body, "task": pending_task},
                    },
                },
            )

        try:
            scout_req = await parse_a2a_message_to_scout_request(message_obj, metadata)
            if scout_req.ap2_mandate and not ap2_from_meta:
                _, m_dig, m_err = validate_ap2_mandate(scout_req.ap2_mandate, None)
                if m_err:
                    hdrs, body = build_402_challenge(resource_url, detail=m_err)
                    return (
                        402,
                        hdrs,
                        {"jsonrpc": "2.0", "id": req_id, "error": {"code": 402, "message": m_err, "data": body}},
                    )
                settlement.ap2_mandate_id = scout_req.ap2_mandate.mandate_id
                settlement.ap2_mandate_sha256 = m_dig

            scout_result = await execute_a2a_scout(
                req=scout_req,
                manager=manager,
                base_url=base_url,
                public_wss_ready=public_wss_ready,
                settlement=settlement,
                owner_passcode_valid=owner_passcode_valid,
                origin_channel="a2a",
                caller_agent="Google A2A Agent",
            )
        except PaymentRequiredError as pay_exc:
            hdrs, body = build_402_challenge(resource_url, detail=str(pay_exc))
            return (
                402,
                hdrs,
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": 402, "message": str(pay_exc), "data": body}},
            )
        except Exception as exc:
            return (
                400,
                {},
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": str(exc)},
                },
            )

        task_id = f"task_{uuid.uuid4().hex[:10]}"
        summary_text = _format_a2a_summary_text(scout_result)
        artifacts: list[dict[str, Any]] = [
            {
                "artifactId": "ranked_quotes_json",
                "name": "Google Maps Voice Scout Insights & Settlement Receipt",
                "parts": [{"kind": "data", "data": scout_result}],
            }
        ]
        for q in scout_result.get("ranked_quotes") or []:
            if q.get("audio_proof_url"):
                artifacts.append(
                    {
                        "artifactId": f"audio_{q['shop_id']}",
                        "name": f"24kHz Call Audio Proof — {q['name']}",
                        "parts": [
                            {
                                "kind": "file",
                                "file": {
                                    "name": f"{q['shop_id']}.wav",
                                    "mimeType": "audio/wav",
                                    "uri": q["audio_proof_url"],
                                },
                            }
                        ],
                    }
                )

        completed_task = {
            "id": task_id,
            "taskId": task_id,
            "status": {"state": "COMPLETED"},
            "history": [
                message_obj,
                {
                    "messageId": f"msg_{uuid.uuid4().hex[:8]}",
                    "role": "agent",
                    "parts": [
                        {"kind": "text", "text": summary_text},
                        {"kind": "data", "data": scout_result},
                    ],
                },
            ],
            "artifacts": artifacts,
            "metadata": {
                "questId": scout_result.get("quest_id"),
                "settlement": scout_result.get("settlement"),
            },
        }
        A2A_TASK_STORE[task_id] = completed_task
        return (
            200,
            build_payment_response_headers(settlement),
            {"jsonrpc": "2.0", "id": req_id, "result": {"task": completed_task}},
        )

    return (
        200,
        {},
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unsupported A2A method: {method}"},
        },
    )


_SCOUT_MCP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "place_category": {
            "type": "string",
            "enum": [c.value for c in PlaceCategory],
            "description": "Type of Google Maps place: 'hotel', 'restaurant', 'clinic', 'diagnostics', 'petrol', 'auto', 'salon', 'pharmacy', or 'custom'.",
            "default": "custom",
        },
        "item_question": {
            "type": "string",
            "description": "Exact question / purpose to ask on the call (e.g. 'Room tonight with 10 AM early check-in and direct tariff in ₹', 'Table for 4 at 8:30 PM', 'XP95 petrol + nitrogen air working', or '1.5T Brain MRI slot today').",
        },
        "city": {
            "type": "string",
            "description": "City name, e.g. 'Mumbai', 'Bengaluru', 'New Delhi', 'Hyderabad', 'Pune'.",
            "default": "Mumbai",
        },
        "locality": {
            "type": "string",
            "description": "Neighbourhood / area, e.g. 'Bandra West', 'Indiranagar', 'Connaught Place'.",
            "default": "Bandra West",
        },
        "specific_place": {
            "type": "string",
            "description": "Optional exact hotel, restaurant, clinic, or business name if calling a specific Google Maps place.",
            "default": "",
        },
        "query": {
            "type": "string",
            "description": "Optional custom Google Maps search phrase when place_category='custom'.",
            "default": "",
        },
        "caller_name": {
            "type": "string",
            "description": "Optional caller first name (e.g. 'Rahul') used as a polite human confidence marker.",
            "default": "",
        },
        "github_username": {
            "type": "string",
            "description": "Optional GitHub handle that starred https://github.com/manavaga/ringscout to unlock bonus free live calls.",
            "default": "",
        },
        "max_shops": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "default": 3,
        },
        "preferred_tone": {
            "type": "string",
            "enum": [
                "polite_indian_english",
                "conversational_hinglish",
                "natural_hindi",
            ],
            "default": "polite_indian_english",
        },
        "execution_mode": {
            "type": "string",
            "enum": ["auto", "live_pstn", "swarm"],
            "default": "auto",
        },
        "stripe_payment_token": {
            "type": "string",
            "description": "Optional Stripe Shared Payment Token (`spt_...`), PaymentMethod (`pm_...`), or confirmed PaymentIntent (`pi_...`) when Phase B payment gate is active.",
        },
        "x402_payment_signature": {
            "type": "string",
            "description": "Optional Base64 x402 EIP-3009 USDC paymentPayload when Phase B payment gate is active.",
        },
    },
    "required": ["item_question"],
}

OFFICIAL_MCP_SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
SEP2127_SERVER_CARD_SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
MAX_MCP_BATCH_SIZE = 32
MCP_SHORT_DESCRIPTION = (
    "Real-time local voice checks — calls Google Maps places in India to verify availability & rates."
)
SUPPORTED_MCP_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
MCP_SERVER_INSTRUCTIONS = (
    "RingScout finds local businesses on Google Maps in India and calls them directly in Indian English, "
    "Hindi, or Hinglish to verify what's happening right now: restaurant table availability & wait times, "
    "hotel rooms, early check-in & direct tariffs, same-day salon/spa appointments, clinic/diagnostic slots & "
    "charges, petrol pump fuel/services, and pharmacy or auto shop items. Contacts up to five businesses in "
    "parallel and returns verified responses, prices in ₹, key details, and recorded call audio when available. "
    "Important limitations: Results depend on businesses being reachable and answering, and availability/prices "
    "can change after a call. RingScout provides information; it does not make reservations, purchases, or appointments."
)

MCP_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "get_ringscout_pricing_and_status",
        "title": "Check RingScout Coverage, Capabilities & Status",
        "description": (
            "Read-only free tool returning RingScout's coverage (India), supported local business categories "
            "(restaurants, hotels, salons, spas, clinics, diagnostic centres, petrol pumps, pharmacies, auto shops), "
            "active free/paid call status, and live telephony readiness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Check RingScout Coverage & Status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "scout_places_on_google_maps",
        "title": "Real-time local voice checks for Google Maps businesses",
        "description": (
            "Finds local businesses on Google Maps in India and calls up to five in parallel in Indian English, "
            "Hindi, or Hinglish to verify what's happening right now: restaurant tables & wait times, hotel rooms, "
            "early check-in & direct tariffs, same-day salon/spa appointments, clinic/diagnostic slots & charges, "
            "petrol pump fuel/services, or pharmacy/auto shop stock. Returns verified responses, prices in ₹, key "
            "details, and recorded call audio when available. RingScout provides information only; it does not make "
            "reservations, purchases, or appointments."
        ),
        "inputSchema": _SCOUT_MCP_INPUT_SCHEMA,
        "annotations": {
            "title": "Real-Time Local Voice Check (Google Maps)",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "scout_india_local_businesses",
        "title": "Call Local Businesses in India (Indian English / Hindi / Hinglish)",
        "description": (
            "Alias for `scout_places_on_google_maps`: contacts up to five local businesses on Google Maps in India "
            "in parallel to verify live availability, wait times, and ₹ prices, with recorded call audio when available. "
            "Provides information only; does not make reservations, purchases, or appointments."
        ),
        "inputSchema": _SCOUT_MCP_INPUT_SCHEMA,
        "annotations": {
            "title": "Scout India Local Businesses (+91)",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
]

MCP_RESOURCES_SPEC: list[dict[str, Any]] = [
    {
        "uri": "ringscout://status/capabilities",
        "name": "RingScout Live Capabilities & Pricing",
        "description": "Real-time Agent Card, supported Google Maps categories, PSTN carrier readiness, and Phase A/B payment gate status.",
        "mimeType": "application/json",
    },
    {
        "uri": "ringscout://locations/india-neighbourhoods",
        "name": "Supported Indian Cities & PIN-Grounded Neighbourhoods",
        "description": "Directory of 10 major Indian cities and ~130 PIN-grounded neighbourhoods for hyperlocal Google Maps voice scouting.",
        "mimeType": "application/json",
    },
    {
        "uri": "ringscout://docs/discovery-endpoints",
        "name": "RingScout Multi-Protocol Discovery Endpoints",
        "description": "Machine-readable index of MCP, SEP-2127 Server Card, AI Catalog, Google A2A v1.0, OpenAPI Actions, and x402 endpoints.",
        "mimeType": "application/json",
    },
]

MCP_PROMPTS_SPEC: list[dict[str, Any]] = [
    {
        "name": "scout_restaurant_table",
        "description": "Call open restaurants in a neighbourhood in parallel to check live table availability, wait time, and seating preferences.",
        "arguments": [
            {"name": "city", "description": "Indian city (e.g. Mumbai, Bengaluru, New Delhi)", "required": True},
            {"name": "locality", "description": "Neighbourhood (e.g. Bandra West, Indiranagar)", "required": True},
            {"name": "party_size", "description": "Number of guests (e.g. 4)", "required": False},
            {"name": "time_slot", "description": "Target dining time (e.g. tonight at 8:30 PM)", "required": False},
        ],
    },
    {
        "name": "scout_hotel_direct_tariff",
        "description": "Call open hotels in parallel to check same-night room availability, early check-in, and direct front-desk tariff in ₹.",
        "arguments": [
            {"name": "city", "description": "Indian city (e.g. Mumbai, Goa, Bengaluru)", "required": True},
            {"name": "locality", "description": "Neighbourhood (e.g. Bandra West, Candolim)", "required": True},
            {"name": "checkin_note", "description": "Room type & check-in request (e.g. Deluxe double tonight + 10 AM early check-in)", "required": False},
        ],
    },
    {
        "name": "scout_urgent_pharmacy_or_mri",
        "description": "Call open pharmacies or diagnostic/MRI centers in parallel to verify urgent stock/slot availability and walk-in ₹ price.",
        "arguments": [
            {"name": "city", "description": "Indian city (e.g. Bengaluru, Mumbai)", "required": True},
            {"name": "locality", "description": "Neighbourhood (e.g. Indiranagar, Bandra West)", "required": True},
            {"name": "item_or_scan", "description": "Exact medicine or scan needed (e.g. Augmentin 625 Duo or 1.5T Brain MRI plain)", "required": True},
        ],
    },
]


def _negotiate_mcp_protocol_version(client_version: str | None) -> str:
    cleaned = (client_version or "").strip()
    if cleaned in SUPPORTED_MCP_PROTOCOL_VERSIONS:
        return cleaned
    return "2025-06-18"


def _render_mcp_prompt(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    city = str(args.get("city") or "Mumbai")
    locality = str(args.get("locality") or "Bandra West")
    if name == "scout_restaurant_table":
        party = str(args.get("party_size") or "4")
        slot = str(args.get("time_slot") or "tonight at 8:30 PM")
        text = (
            f"Use `scout_places_on_google_maps` with `place_category='restaurant'`, `city='{city}'`, "
            f"`locality='{locality}'`, and `item_question='Table for {party} {slot} and live wait time'` "
            "to call top open restaurants in parallel and summarize which ones have confirmed seating."
        )
        return {
            "description": f"Parallel restaurant table check in {locality}, {city}",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }
    if name == "scout_hotel_direct_tariff":
        note = str(args.get("checkin_note") or "Deluxe double room tonight, 10 AM early check-in, and direct phone tariff in ₹")
        text = (
            f"Use `scout_places_on_google_maps` with `place_category='hotel'`, `city='{city}'`, "
            f"`locality='{locality}'`, and `item_question='{note}'` to compare verified direct hotel rates."
        )
        return {
            "description": f"Parallel hotel tariff & early check-in check in {locality}, {city}",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }
    if name == "scout_urgent_pharmacy_or_mri":
        item = str(args.get("item_or_scan") or "1.5T Brain MRI slot today and walk-in cash price in ₹")
        cat = "diagnostics" if any(k in item.lower() for k in ("mri", "ct", "scan", "xray", "x-ray", "ultrasound")) else "pharmacy"
        text = (
            f"Use `scout_places_on_google_maps` with `place_category='{cat}'`, `city='{city}'`, "
            f"`locality='{locality}'`, and `item_question='{item}'` to find the fastest confirmed option and ₹ price."
        )
        return {
            "description": f"Urgent {cat} availability & ₹ price check in {locality}, {city}",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }
    return None


async def handle_mcp_jsonrpc(
    rpc_body: dict[str, Any],
    *,
    manager: "QuestManager",
    base_url: str,
    public_wss_ready: bool,
    payment_sig_header: str | None,
    auth_header: str | None,
    sandbox_header: str | None,
    ap2_header_raw: str | None,
    owner_passcode_valid: bool,
    stripe_token_header: str | None = None,
    client_fingerprint: str = "mcp_connector",
    user_agent: str | None = None,
    github_username_header: str | None = None,
    out_headers: dict[str, str] | None = None,
    incoming_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Process a single MCP Streamable HTTP JSON-RPC 2.0 request (Claude, Gemini, ChatGPT, Cursor, Registries)."""
    if rpc_body.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": rpc_body.get("id"),
            "error": {"code": -32600, "message": "Invalid Request: jsonrpc must be '2.0'"},
        }

    if "method" not in rpc_body and ("result" in rpc_body or "error" in rpc_body):
        # Per MCP Streamable HTTP §2.1.2: client-sent JSON-RPC responses receive 202 Accepted
        return None

    method = str(rpc_body.get("method") or "")
    req_id = rpc_body.get("id")
    params = rpc_body.get("params") if isinstance(rpc_body.get("params"), dict) else {}
    clean_base = base_url.rstrip("/")

    if incoming_session_id and out_headers is not None:
        out_headers.setdefault("Mcp-Session-Id", incoming_session_id)

    if "id" not in rpc_body or req_id is None:
        # Per JSON-RPC 2.0 & MCP Streamable HTTP §2.1.2: all notifications receive 202 Accepted with no body
        return None

    if method == "initialize":
        negotiated_proto = _negotiate_mcp_protocol_version(
            str(params.get("protocolVersion") or "")
        )
        session_id = incoming_session_id or f"mcp_{uuid.uuid4().hex}"
        if out_headers is not None:
            out_headers["Mcp-Session-Id"] = session_id
            out_headers["MCP-Protocol-Version"] = negotiated_proto
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated_proto,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "ringscout-india-voice-oracle",
                    "title": "RingScout — Parallel Google Maps Voice Scout",
                    "version": "3.0.0",
                    "websiteUrl": clean_base,
                },
                "instructions": MCP_SERVER_INSTRUCTIONS,
            },
        }

    if method in ("ping", "logging/setLevel"):
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "completion/complete":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"completion": {"values": [], "total": 0, "hasMore": False}},
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS_SPEC},
        }

    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resources": MCP_RESOURCES_SPEC},
        }

    if method == "resources/templates/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resourceTemplates": []},
        }

    if method == "resources/read":
        uri = str(params.get("uri") or "")
        if uri == "ringscout://status/capabilities":
            card = build_agent_card(base_url, manager.pstn_adapter.is_configured())
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(card, indent=2),
                        }
                    ]
                },
            }
        if uri == "ringscout://locations/india-neighbourhoods":
            summary = {
                city: [{"name": a["name"], "pins": a["pins"]} for a in areas]
                for city, areas in INDIA_CITY_AREAS.items()
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(summary, indent=2),
                        }
                    ]
                },
            }
        if uri == "ringscout://docs/discovery-endpoints":
            cat = build_ai_catalog_manifest(base_url)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(cat, indent=2),
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32002, "message": f"Resource not found: {uri}"},
        }

    if method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"prompts": MCP_PROMPTS_SPEC},
        }

    if method == "prompts/get":
        p_name = str(params.get("name") or "")
        p_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        rendered = _render_mcp_prompt(p_name, p_args)
        if rendered is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown prompt: {p_name}"},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": rendered}

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if tool_name == "get_ringscout_pricing_and_status":
            card = build_agent_card(base_url, manager.pstn_adapter.is_configured())
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(card, indent=2),
                        }
                    ],
                    "isError": False,
                },
            }

        if tool_name in ("scout_places_on_google_maps", "scout_india_local_businesses"):
            eff_payment_sig = payment_sig_header or args.get("x402_payment_signature")
            eff_stripe_tok = stripe_token_header or args.get("stripe_payment_token")
            eff_sandbox = sandbox_header or (
                "simulate"
                if (
                    owner_passcode_valid
                    and not eff_payment_sig
                    and not auth_header
                    and not eff_stripe_tok
                    and get_payment_gate_mode() == "required"
                )
                else None
            )
            mcp_exec_mode = str(args.get("execution_mode") or "auto")
            mcp_gh_user = github_username_header or str(args.get("github_username") or "").strip() or None
            settlement, reason = await verify_a2a_payment(
                resource_url=f"{clean_base}/mcp",
                payment_sig_header=eff_payment_sig,
                auth_header=auth_header,
                sandbox_header=eff_sandbox,
                ap2_mandate=None,
                ap2_header_raw=ap2_header_raw,
                owner_passcode_valid=owner_passcode_valid,
                stripe_token_header=eff_stripe_tok,
                client_fingerprint=client_fingerprint,
                user_agent=user_agent,
                execution_mode=mcp_exec_mode,
                github_username=mcp_gh_user,
            )
            if settlement is None:
                _, challenge_body = build_402_challenge(
                    f"{clean_base}/mcp", detail=reason
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(challenge_body, indent=2),
                            }
                        ],
                        "isError": True,
                    },
                }

            try:
                raw_cat = str(args.get("place_category") or "custom").lower()
                cat_enum = (
                    PlaceCategory(raw_cat)
                    if raw_cat in {c.value for c in PlaceCategory}
                    else PlaceCategory.CUSTOM
                )
                default_tone = (
                    "conversational_hinglish"
                    if tool_name == "scout_india_local_businesses" and not args.get("preferred_tone")
                    else "polite_indian_english"
                )
                scout_req = A2AScoutRequest(
                    place_category=cat_enum,
                    query=str(args.get("query") or ""),
                    item_question=str(args.get("item_question") or ""),
                    specific_place=str(args.get("specific_place") or ""),
                    caller_name=str(args.get("caller_name") or ""),
                    city=str(args.get("city") or "Bengaluru"),
                    locality=str(args.get("locality") or "Indiranagar"),
                    max_shops=min(max(int(args.get("max_shops") or 3), 1), 5),
                    preferred_tone=VocalTone(
                        str(args.get("preferred_tone") or default_tone)
                    ),
                    execution_mode=mcp_exec_mode,  # type: ignore[arg-type]
                )
                result_payload = await execute_a2a_scout(
                    req=scout_req,
                    manager=manager,
                    base_url=base_url,
                    public_wss_ready=public_wss_ready,
                    settlement=settlement,
                    owner_passcode_valid=owner_passcode_valid,
                    origin_channel="mcp",
                    caller_agent="MCP Connector Agent",
                )
                if out_headers is not None:
                    out_headers.update(build_payment_response_headers(settlement))
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result_payload, indent=2),
                            }
                        ],
                        "isError": False,
                    },
                }
            except PaymentRequiredError as pay_exc:
                _, challenge_body = build_402_challenge(
                    f"{clean_base}/mcp", detail=str(pay_exc)
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(challenge_body, indent=2),
                            }
                        ],
                        "isError": True,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Scout error: {exc}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


async def handle_mcp_payload(
    rpc_payload: dict[str, Any] | list[Any],
    **kwargs: Any,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Process either a single JSON-RPC 2.0 request object or a bounded JSON-RPC 2.0 batch array."""
    if isinstance(rpc_payload, dict):
        return await handle_mcp_jsonrpc(rpc_payload, **kwargs)
    if isinstance(rpc_payload, list):
        if not rpc_payload:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Empty JSON-RPC batch"},
            }
        if len(rpc_payload) > MAX_MCP_BATCH_SIZE:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": f"JSON-RPC batch size ({len(rpc_payload)}) exceeds maximum ({MAX_MCP_BATCH_SIZE})",
                },
            }
        responses: list[dict[str, Any]] = []
        for item in rpc_payload:
            if not isinstance(item, dict):
                responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid JSON-RPC batch item"},
                    }
                )
                continue
            res = await handle_mcp_jsonrpc(item, **kwargs)
            if res is not None:
                responses.append(res)
        return responses if responses else None
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid JSON-RPC payload"},
    }


def build_openapi_actions_spec(base_url: str) -> dict[str, Any]:
    """Build OpenAPI 3.1.0 schema for 1-click import into ChatGPT Custom GPT Actions & OpenAPI Agents."""
    clean = base_url.rstrip("/")
    quote_item_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "shop_id": {"type": "string"},
            "name": {"type": "string"},
            "phone_display": {"type": "string"},
            "address": {"type": "string"},
            "is_live_pstn": {"type": "boolean"},
            "carrier_provider": {"type": ["string", "null"]},
            "call_uuid": {"type": ["string", "null"]},
            "call_outcome": {"type": "string"},
            "duration_sec": {"type": "number"},
            "in_stock": {"type": ["boolean", "null"]},
            "availability_label": {"type": "string"},
            "price_display": {"type": ["string", "null"]},
            "eta_or_slot": {"type": ["string", "null"]},
            "notes": {"type": "string"},
            "audio_proof_url": {"type": ["string", "null"]},
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "RingScout — Real-time local voice checks",
            "version": "3.0.0",
            "description": (
                "RingScout finds local businesses on Google Maps in India and calls them directly in Indian English, "
                "Hindi, or Hinglish to verify what's happening right now (up to five businesses in parallel, returning "
                "verified responses, prices in ₹, key details, and recorded call audio when available). RingScout "
                "provides information; it does not make reservations, purchases, or appointments."
            ),
        },
        "externalDocs": {
            "description": "RingScout Open-Source Repository (Star on GitHub)",
            "url": GITHUB_REPO_URL,
        },
        "servers": [{"url": clean}],
        "paths": {
            "/api/v1/scout": {
                "post": {
                    "operationId": "scoutPlacesOnGoogleMaps",
                    "summary": "Find open places on Google Maps and call them in parallel to verify live availability & ₹ rates",
                    "description": (
                        "Discovers open Google Maps places matching the category/area and conducts brief, real-time "
                        "+91 voice calls to verify live availability, wait times, rates in ₹, and specific questions."
                    ),
                    "x-openai-isConsequential": False,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["item_question"],
                                    "properties": {
                                        "place_category": {
                                            "type": "string",
                                            "enum": [c.value for c in PlaceCategory],
                                            "default": "restaurant",
                                            "description": "Location category on Google Maps.",
                                        },
                                        "item_question": {
                                            "type": "string",
                                            "description": "Exact question to ask the business on the live phone call.",
                                        },
                                        "city": {
                                            "type": "string",
                                            "default": "Mumbai",
                                            "description": "City name in India.",
                                        },
                                        "locality": {
                                            "type": "string",
                                            "default": "Bandra West",
                                            "description": "Neighbourhood or locality.",
                                        },
                                        "specific_place": {
                                            "type": "string",
                                            "default": "",
                                            "description": "Optional specific business name on Google Maps.",
                                        },
                                        "caller_name": {
                                            "type": "string",
                                            "default": "",
                                            "description": "Optional caller first name for natural greeting.",
                                        },
                                        "github_username": {
                                            "type": "string",
                                            "default": "",
                                            "description": "Optional GitHub username that starred https://github.com/manavaga/ringscout.",
                                        },
                                        "max_shops": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 5,
                                            "default": 2,
                                            "description": "Number of open places to call in parallel (1 to 5).",
                                        },
                                        "preferred_tone": {
                                            "type": "string",
                                            "enum": [
                                                "polite_indian_english",
                                                "conversational_hinglish",
                                                "natural_hindi",
                                            ],
                                            "default": "polite_indian_english",
                                            "description": "Spoken voice accent and language.",
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Ranked verified insights, ₹ quotes, transcripts, and 24kHz .wav audio URLs.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "quest_id": {"type": "string"},
                                            "place_category": {"type": "string"},
                                            "execution_mode": {"type": "string"},
                                            "is_live_pstn": {"type": "boolean"},
                                            "best_match": {"anyOf": [quote_item_schema, {"type": "null"}]},
                                            "ranked_quotes": {"type": "array", "items": quote_item_schema},
                                        },
                                    }
                                }
                            },
                        },
                        "402": {
                            "description": "Star-Gate or Payment Required when free quota is exceeded.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "x402Version": {"type": "integer"},
                                            "error": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                    },
                }
            },
            "/api/v1/pricing": {
                "get": {
                    "operationId": "getRingScoutStatusAndPricing",
                    "summary": "Check active free tier, GitHub Star-Gate, and PSTN voice capabilities",
                    "x-openai-isConsequential": False,
                    "responses": {
                        "200": {
                            "description": "Pricing and protocol capabilities.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "x402Version": {"type": "integer"},
                                            "paymentStatus": {
                                                "type": "object",
                                                "properties": {
                                                    "gate_mode": {"type": "string"},
                                                    "star_gate_mode": {"type": "string"},
                                                    "github_repo": {"type": "string"},
                                                    "free_daily_calls_per_client": {"type": "integer"},
                                                },
                                            },
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    }


def build_official_server_json(
    base_url: str,
    namespace_name: str = "io.github.manavaga/ringscout",
) -> dict[str, Any]:
    """Build strict `2025-12-11` Official MCP Registry `server.json` (`description` <= 100 chars)."""
    clean = base_url.rstrip("/")
    return {
        "$schema": OFFICIAL_MCP_SCHEMA_URL,
        "name": namespace_name,
        "title": "RingScout — Real-time local voice checks",
        "description": MCP_SHORT_DESCRIPTION,
        "version": "3.0.0",
        "websiteUrl": clean,
        "repository": {
            "url": "https://github.com/manavaga/ringscout",
            "source": "github",
        },
        "remotes": [
            {
                "type": "streamable-http",
                "url": f"{clean}/mcp",
            },
            {
                "type": "sse",
                "url": f"{clean}/sse",
            },
        ],
    }


def build_mcp_server_card(base_url: str, live_pstn_ready: bool = True) -> dict[str, Any]:
    """Build SEP-2127 MCP Server Card (`/.well-known/mcp/server-card.json` & `/mcp/server-card`)."""
    clean = base_url.rstrip("/")
    pay_state = masked_payment_status()
    return {
        "$schema": SEP2127_SERVER_CARD_SCHEMA_URL,
        "name": "io.github.manavaga/ringscout",
        "title": "RingScout — Real-time local voice checks",
        "version": "3.0.0",
        "description": MCP_SHORT_DESCRIPTION,
        "websiteUrl": clean,
        "repository": {
            "url": "https://github.com/manavaga/ringscout",
            "source": "github",
        },
        "remotes": [
            {
                "type": "streamable-http",
                "url": f"{clean}/mcp",
                "supportedProtocolVersions": list(SUPPORTED_MCP_PROTOCOL_VERSIONS),
            },
            {
                "type": "sse",
                "url": f"{clean}/sse",
                "supportedProtocolVersions": ["2024-11-05"],
            },
        ],
        "authentication": {
            "required": pay_state["gate_mode"] == "required",
            "mode": pay_state["gate_mode"],
            "schemes": ["none", "stripe-mpp", "x402-eip3009"],
        },
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
            "liveIndiaPstnConfigured": live_pstn_ready,
        },
        "instructions": MCP_SERVER_INSTRUCTIONS,
        "tools": [t["name"] for t in MCP_TOOLS_SPEC],
        "prompts": [p["name"] for p in MCP_PROMPTS_SPEC],
        "resources": [r["uri"] for r in MCP_RESOURCES_SPEC],
        "_meta": {
            "io.ringscout/discovery": {
                "documentationUrl": f"{clean}/developers",
                "summary": (
                    "Remote Streamable HTTP MCP server that calls real open spas, salons, hotels, restaurants, "
                    "clinics, MRI centers, petrol stations, auto garages, and pharmacies on Google Maps in parallel "
                    "(en-IN / hi-IN) and returns verified availability, ₹ prices, and 24kHz .wav audio proof."
                ),
                "toolDefinitions": MCP_TOOLS_SPEC,
            }
        },
    }


def build_mcp_registry_manifest(base_url: str, live_pstn_ready: bool = True) -> dict[str, Any]:
    """Build `/.well-known/mcp.json` discovery manifest combining Official Registry + SEP-2127 Server Card metadata."""
    card = build_mcp_server_card(base_url, live_pstn_ready=live_pstn_ready)
    card["$schema"] = OFFICIAL_MCP_SCHEMA_URL
    return card


def build_ai_catalog_manifest(base_url: str) -> dict[str, Any]:
    """Build RFC 8615 / SEP-2127 Domain AI Catalog (`/.well-known/ai-catalog.json`)."""
    clean = base_url.rstrip("/")
    entries = [
        {
            "identifier": "urn:air:manavaga.github.io:mcp:ringscout",
            "name": "io.github.manavaga/ringscout",
            "kind": "mcp-server",
            "type": "application/mcp-server-card+json",
            "mediaType": "application/mcp-server-card+json",
            "url": f"{clean}/.well-known/mcp/server-card.json",
            "endpoint": f"{clean}/mcp",
            "transport": "streamable-http",
        },
        {
            "identifier": "urn:air:manavaga.github.io:a2a:ringscout",
            "name": "ringscout-a2a-agent",
            "kind": "a2a-agent",
            "type": "application/a2a-agent-card+json",
            "mediaType": "application/a2a-agent-card+json",
            "url": f"{clean}/.well-known/agent-card.json",
            "endpoint": f"{clean}/a2a",
            "protocolVersion": "1.0",
        },
        {
            "identifier": "urn:air:manavaga.github.io:openapi:ringscout",
            "name": "ringscout-openapi-actions",
            "kind": "openapi-actions",
            "type": "application/vnd.oai.openapi+json",
            "mediaType": "application/vnd.oai.openapi+json",
            "url": f"{clean}/openapi-actions.json",
            "endpoint": f"{clean}/api/v1/scout",
        },
        {
            "identifier": "urn:air:manavaga.github.io:llms:ringscout",
            "name": "ringscout-llms-guide",
            "kind": "llms-txt",
            "type": "text/markdown",
            "mediaType": "text/markdown",
            "url": f"{clean}/llms.txt",
        },
    ]
    return {
        "specVersion": "1.0",
        "version": "1.0",
        "domain": clean,
        "service": "RingScout — Parallel Google Maps Voice Scout",
        "updatedAt": "2026-11-25T00:00:00Z",
        "entries": entries,
        "catalog": entries,
    }


def build_agents_json_manifest(base_url: str) -> dict[str, Any]:
    """Build `/.well-known/agents.json` multi-agent & OpenAPI workflow manifest (`agents.json` standard)."""
    clean = base_url.rstrip("/")
    return {
        "schemaVersion": "1.0.0",
        "name": "RingScout — Parallel Google Maps Voice Scout",
        "description": MCP_SERVER_INSTRUCTIONS,
        "url": clean,
        "sources": [
            {
                "id": "ringscout_openapi",
                "type": "openapi",
                "url": f"{clean}/openapi-actions.json",
            },
            {
                "id": "ringscout_mcp",
                "type": "mcp-streamable-http",
                "url": f"{clean}/mcp",
                "serverCard": f"{clean}/.well-known/mcp/server-card.json",
            },
            {
                "id": "ringscout_a2a",
                "type": "google-a2a",
                "url": f"{clean}/a2a",
                "agentCard": f"{clean}/.well-known/agent-card.json",
            },
        ],
        "agents": [
            {
                "id": "ringscout-voice-oracle",
                "name": "RingScout Google Maps Voice Scout",
                "protocol": "a2a+mcp",
                "mcpEndpoint": f"{clean}/mcp",
                "a2aEndpoint": f"{clean}/a2a",
                "agentCardUrl": f"{clean}/.well-known/agent-card.json",
                "serverCardUrl": f"{clean}/.well-known/mcp/server-card.json",
                "skills": ["get_ringscout_pricing_and_status", "scout_places_on_google_maps"],
            }
        ],
        "flows": [
            {
                "id": "verify_live_availability_and_inr_rates",
                "description": (
                    "Check status/pricing via `getRingScoutStatusAndPricing`, then place parallel voice calls "
                    "to open Google Maps businesses via `scoutPlacesOnGoogleMaps` and return ranked ₹ quotes + 24kHz WAV proof."
                ),
                "actions": [
                    {"sourceId": "ringscout_openapi", "operationId": "getRingScoutStatusAndPricing"},
                    {"sourceId": "ringscout_openapi", "operationId": "scoutPlacesOnGoogleMaps"},
                ],
            }
        ],
    }


def build_ai_plugin_manifest(base_url: str) -> dict[str, Any]:
    """Build `/.well-known/ai-plugin.json` manifest for OpenAPI-based agent & plugin loaders."""
    clean = base_url.rstrip("/")
    return {
        "schema_version": "v1",
        "name_for_human": "RingScout",
        "name_for_model": "ringscout_google_maps_voice",
        "description_for_human": "Real-time local voice checks — calls Google Maps businesses in India to verify availability & rates.",
        "description_for_model": MCP_SERVER_INSTRUCTIONS,
        "auth": {"type": "none"},
        "api": {
            "type": "openapi",
            "url": f"{clean}/openapi-actions.json",
            "is_user_authenticated": False,
        },
        "contact_email": "manavagarwal@users.noreply.github.com",
        "legal_info_url": f"{clean}/privacy",
    }
