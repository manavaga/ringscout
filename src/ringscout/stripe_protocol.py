"""Stripe Machine Payments Protocol (MPP — `WWW-Authenticate: Payment` / `Authorization: Payment`)
and Stripe `PaymentIntents` / Shared Payment Token (`spt_...` / `pm_...` / `pi_...`) verifier.

Supports:
1. Phase A (Default `PAYMENT_GATE_MODE="off"`): Frictionless out-of-the-box A2A / MCP / ChatGPT Actions
   execution with daily safety caps (zero payment wall so Claude, Gemini, and ChatGPT work immediately).
2. Phase B (`PAYMENT_GATE_MODE="freemium"` or `"required"`):
   - Official Stripe + Tempo Machine Payments Protocol (`MPP`) HTTP 402 challenge (`WWW-Authenticate: Payment`)
     and `Authorization: Payment <base64url_credential>` verification + `Payment-Receipt` response header.
   - Live Stripe `PaymentIntents` API (`https://api.stripe.com/v1/payment_intents`) charging/verifying
     Shared Payment Tokens (`spt_...`), PaymentMethods (`pm_card_visa`), or confirmed `pi_...` IDs when
     `STRIPE_SECRET_KEY` is configured (in env or runtime RAM).
   - 1-click Stripe Checkout URL generation for human chat connectors (`claude.ai`, `chatgpt.com`, `gemini.google.com`).
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

STRIPE_API_BASE = "https://api.stripe.com/v1"
SCOUT_PRICE_CENTS = 75  # $0.75 USD
SCOUT_PRICE_USD = 0.75
SCOUT_PRICE_USD_STR = "0.75"
_STRIPE_TOKEN_RE = re.compile(r"^(?:pi|spt|pm)_[A-Za-z0-9_]{3,96}$")

_PROCESS_MPP_SECRET = secrets.token_hex(32)
SPENT_STRIPE_TOKENS: OrderedDict[str, float] = OrderedDict()


GITHUB_REPO_SLUG = os.environ.get("RINGSCOUT_GITHUB_REPO", "manavaga/ringscout").strip() or "manavaga/ringscout"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO_SLUG}"
_GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_STAR_CACHE: OrderedDict[str, tuple[bool, float]] = OrderedDict()
_DAILY_STARGAZER_USAGE: OrderedDict[str, int] = OrderedDict()


@dataclass
class RuntimePaymentConfig:
    """In-memory operator configuration for Phase A (open) vs Phase B (freemium / paid), Star-Gate & Stripe keys."""

    gate_mode: str = "off"  # "off" (Phase A live open), "freemium" (N free/day then paid), "required"
    star_gate_mode: str = "off"  # "off", "after_first_call" (1 free call then star for 5/day), "strict" (star required from call 1)
    free_daily_calls_per_client: int = 5
    global_daily_free_pstn_legs: int = 25
    stripe_secret_key: str = ""
    stripe_checkout_url: str = ""


_RUNTIME_PAY_CFG = RuntimePaymentConfig(
    gate_mode=os.environ.get("RINGSCOUT_PAYMENT_GATE_MODE", "off").strip().lower() or "off",
    star_gate_mode=os.environ.get("RINGSCOUT_STAR_GATE_MODE", "off").strip().lower() or "off",
    free_daily_calls_per_client=int(os.environ.get("RINGSCOUT_FREE_DAILY_CALLS", "5")),
    global_daily_free_pstn_legs=int(os.environ.get("RINGSCOUT_GLOBAL_DAILY_FREE_PSTN_CAP", "25")),
    stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
    stripe_checkout_url=os.environ.get("STRIPE_CHECKOUT_URL", "").strip(),
)

# Per-(UTC-day, client-fingerprint) usage counter for Phase A safety cap & Phase B freemium tier
_DAILY_CLIENT_USAGE: OrderedDict[str, int] = OrderedDict()
_DAILY_GLOBAL_FREE_PSTN_LEGS: OrderedDict[str, int] = OrderedDict()

_AUTOMATED_SCANNER_UA_MARKERS: tuple[str, ...] = (
    "brickbluebot",
    "brick.blue",
    "agentvalcon",
    "sealkeeper",
    "sentineloracle",
    "alalanet-prober",
    "smithery",
    "glama",
    "pulsemcp",
    "x402scan",
    "headlesschrome",
    "uptimerobot",
    "pingdom",
    "statuscake",
    "spider",
    "crawler",
    "prober",
    "scanner",
)


def is_automated_scanner_user_agent(user_agent: str | None) -> bool:
    """Detect automated MCP/A2A directory scanners so unpaid bot probes receive 402 instead of ringing real +91 phones."""
    ua = (user_agent or "").strip().lower()
    if not ua:
        return False
    return any(tok in ua for tok in _AUTOMATED_SCANNER_UA_MARKERS)


def get_payment_gate_mode() -> str:
    """Resolve active payment gate mode; explicit env override wins in tests, else runtime config."""
    env_mode = os.environ.get("RINGSCOUT_PAYMENT_GATE_MODE", "").strip().lower()
    if env_mode in ("off", "freemium", "required"):
        return env_mode
    return _RUNTIME_PAY_CFG.gate_mode if _RUNTIME_PAY_CFG.gate_mode in ("off", "freemium", "required") else "off"


def get_effective_stripe_secret() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "").strip() or _RUNTIME_PAY_CFG.stripe_secret_key.strip()


def get_effective_checkout_url() -> str:
    return os.environ.get("STRIPE_CHECKOUT_URL", "").strip() or _RUNTIME_PAY_CFG.stripe_checkout_url.strip()


def get_star_gate_mode() -> str:
    """Resolve GitHub Star-Gate mode: 'off', 'after_first_call', or 'strict'."""
    env_mode = os.environ.get("RINGSCOUT_STAR_GATE_MODE", "").strip().lower()
    if env_mode in ("off", "after_first_call", "strict"):
        return env_mode
    return (
        _RUNTIME_PAY_CFG.star_gate_mode
        if _RUNTIME_PAY_CFG.star_gate_mode in ("off", "after_first_call", "strict")
        else "off"
    )


async def verify_github_stargazer(github_username: str | None) -> bool:
    """Check whether `github_username` has starred `manavaga/ringscout` on GitHub (60s TTL cache)."""
    clean = (github_username or "").strip().lstrip("@")
    if not clean or not _GITHUB_USER_RE.match(clean):
        return False
    cache_key = clean.lower()
    now = time.time()
    cached = _GITHUB_STAR_CACHE.get(cache_key)
    if cached is not None and (now - cached[1]) < 60.0:
        return cached[0]

    url = f"https://api.github.com/users/{clean}/starred/{GITHUB_REPO_SLUG}"
    hdrs = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "RingScout-StarGate/3.0",
    }
    gh_tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_tok:
        hdrs["Authorization"] = f"Bearer {gh_tok}"
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as http:
            resp = await http.get(url, headers=hdrs)
            starred = resp.status_code == 204
    except Exception as exc:
        logger.warning("GitHub star check failed for %s: %s", clean, exc)
        starred = False

    _GITHUB_STAR_CACHE[cache_key] = (starred, now)
    while len(_GITHUB_STAR_CACHE) > 2048:
        _GITHUB_STAR_CACHE.popitem(last=False)
    return starred


def get_client_daily_used(client_id: str) -> int:
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return _DAILY_CLIENT_USAGE.get(f"{day}:{client_id or 'anon'}", 0)


def check_and_increment_stargazer_quota(github_username: str) -> tuple[bool, int, int]:
    """Per-GitHub-username daily cap so one stargazer handle cannot be shared unboundedly."""
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    key = f"{day}:{github_username.strip().lstrip('@').lower()}"
    limit = max(1, _RUNTIME_PAY_CFG.free_daily_calls_per_client)
    used = _DAILY_STARGAZER_USAGE.get(key, 0)
    if used >= limit:
        return False, used, limit
    _DAILY_STARGAZER_USAGE[key] = used + 1
    while len(_DAILY_STARGAZER_USAGE) > 4096:
        _DAILY_STARGAZER_USAGE.popitem(last=False)
    return True, used + 1, limit


def update_runtime_payment_config(
    *,
    gate_mode: str | None = None,
    star_gate_mode: str | None = None,
    free_daily_calls_per_client: int | None = None,
    global_daily_free_pstn_legs: int | None = None,
    stripe_secret_key: str | None = None,
    stripe_checkout_url: str | None = None,
) -> dict[str, Any]:
    if gate_mode and gate_mode.lower() in ("off", "freemium", "required"):
        _RUNTIME_PAY_CFG.gate_mode = gate_mode.lower()
    if star_gate_mode and star_gate_mode.lower() in ("off", "after_first_call", "strict"):
        _RUNTIME_PAY_CFG.star_gate_mode = star_gate_mode.lower()
    if free_daily_calls_per_client is not None and 1 <= free_daily_calls_per_client <= 500:
        _RUNTIME_PAY_CFG.free_daily_calls_per_client = free_daily_calls_per_client
    if global_daily_free_pstn_legs is not None and 1 <= global_daily_free_pstn_legs <= 2000:
        _RUNTIME_PAY_CFG.global_daily_free_pstn_legs = global_daily_free_pstn_legs
    if stripe_secret_key is not None:
        _RUNTIME_PAY_CFG.stripe_secret_key = stripe_secret_key.strip()
    if stripe_checkout_url is not None:
        _RUNTIME_PAY_CFG.stripe_checkout_url = stripe_checkout_url.strip()
    return masked_payment_status()


def masked_payment_status() -> dict[str, Any]:
    sk = get_effective_stripe_secret()
    masked_sk = f"{sk[:7]}...{sk[-4:]}" if len(sk) > 12 else ("configured" if sk else None)
    return {
        "gate_mode": get_payment_gate_mode(),
        "star_gate_mode": get_star_gate_mode(),
        "github_repo": GITHUB_REPO_URL,
        "free_daily_calls_per_client": _RUNTIME_PAY_CFG.free_daily_calls_per_client,
        "global_daily_free_pstn_legs": _RUNTIME_PAY_CFG.global_daily_free_pstn_legs,
        "stripe_configured": bool(sk),
        "stripe_key_masked": masked_sk,
        "stripe_mode": "live" if sk.startswith("sk_live_") else ("test" if sk.startswith("sk_test_") else "mpp_hmac_ready"),
        "stripe_checkout_url": get_effective_checkout_url() or None,
    }


def check_and_increment_free_quota(client_id: str) -> tuple[bool, int, int]:
    """Return `(within_quota, used_today, limit)` for `client_id` on the current UTC date."""
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    key = f"{day}:{client_id or 'anon'}"
    limit = max(1, _RUNTIME_PAY_CFG.free_daily_calls_per_client)
    used = _DAILY_CLIENT_USAGE.get(key, 0)
    if used >= limit:
        return False, used, limit
    _DAILY_CLIENT_USAGE[key] = used + 1
    while len(_DAILY_CLIENT_USAGE) > 4096:
        _DAILY_CLIENT_USAGE.popitem(last=False)
    return True, used + 1, limit


def check_and_increment_global_free_pstn_legs(requested_legs: int) -> tuple[bool, int, int]:
    """Global daily circuit breaker on unpaid free-tier real PSTN phone legs."""
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    env_cap = os.environ.get("RINGSCOUT_GLOBAL_DAILY_FREE_PSTN_CAP", "").strip()
    cap = int(env_cap) if env_cap.isdigit() else max(1, _RUNTIME_PAY_CFG.global_daily_free_pstn_legs)
    legs = max(1, requested_legs)
    used = _DAILY_GLOBAL_FREE_PSTN_LEGS.get(day, 0)
    if used + legs > cap:
        return False, used, cap
    _DAILY_GLOBAL_FREE_PSTN_LEGS[day] = used + legs
    while len(_DAILY_GLOBAL_FREE_PSTN_LEGS) > 64:
        _DAILY_GLOBAL_FREE_PSTN_LEGS.popitem(last=False)
    return True, used + legs, cap


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    clean = raw.strip()
    padded = clean + "=" * (-len(clean) % 4)
    return base64.urlsafe_b64decode(padded)


def _mpp_hmac_secret() -> bytes:
    raw = os.environ.get("RINGSCOUT_HMAC_SECRET", "").strip() or _PROCESS_MPP_SECRET
    return raw.encode("utf-8")


def _record_or_reject_stripe_replay(token_id: str) -> bool:
    key = token_id.strip()
    if not key or key in SPENT_STRIPE_TOKENS:
        return False
    SPENT_STRIPE_TOKENS[key] = time.time()
    while len(SPENT_STRIPE_TOKENS) > 2048:
        SPENT_STRIPE_TOKENS.popitem(last=False)
    return True


def build_stripe_mpp_challenge(
    resource_url: str,
    amount_cents: int = SCOUT_PRICE_CENTS,
    currency: str = "usd",
) -> dict[str, str]:
    """Build spec-compliant Stripe + Tempo Machine Payments Protocol (`MPP`) challenge components."""
    exp_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
    expires_iso = exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    jcs_req_obj = {
        "amount": str(amount_cents),
        "currency": currency.lower(),
        "merchant": "ringscout",
        "resource": resource_url,
        "sptSupported": True,
    }
    req_b64 = _b64url_encode(
        json.dumps(jcs_req_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    nonce = secrets.token_hex(9)
    mac_input = f"{nonce}:stripe:charge:{req_b64}:{expires_iso}".encode("utf-8")
    mac_hex = hmac.new(_mpp_hmac_secret(), mac_input, hashlib.sha256).hexdigest()[:20]
    challenge_id = f"mpp_{nonce}_{mac_hex}"

    www_auth = (
        f'Payment realm="ringscout", id="{challenge_id}", '
        f'method="stripe", intent="charge", request="{req_b64}", expires="{expires_iso}"'
    )
    return {
        "id": challenge_id,
        "realm": "ringscout",
        "method": "stripe",
        "intent": "charge",
        "request": req_b64,
        "expires": expires_iso,
        "www_authenticate": www_auth,
    }


def verify_mpp_challenge_binding(
    challenge_obj: dict[str, Any],
    expected_resource_url: str | None = None,
    min_amount_cents: int = SCOUT_PRICE_CENTS,
) -> tuple[bool, str]:
    """Verify HMAC signature, expiration, and resource/currency binding on an echoed MPP `challenge` object."""
    cid = str(challenge_obj.get("id") or "")
    method = str(challenge_obj.get("method") or "")
    intent = str(challenge_obj.get("intent") or "")
    req_b64 = str(challenge_obj.get("request") or "")
    expires_iso = str(challenge_obj.get("expires") or "")
    if not cid.startswith("mpp_") or "_" not in cid[4:]:
        return False, "Invalid MPP challenge id format"
    nonce, given_mac = cid[4:].rsplit("_", 1)
    if not nonce or not given_mac:
        return False, "Invalid MPP challenge id format"
    mac_input = f"{nonce}:{method}:{intent}:{req_b64}:{expires_iso}".encode("utf-8")
    expected_mac = hmac.new(_mpp_hmac_secret(), mac_input, hashlib.sha256).hexdigest()[:20]
    if not hmac.compare_digest(expected_mac, given_mac):
        return False, "MPP challenge HMAC signature mismatch"
    try:
        req_obj = json.loads(_b64url_decode(req_b64).decode("utf-8"))
        if str(req_obj.get("currency") or "").lower() != "usd":
            return False, "MPP challenge request currency must be USD"
        if int(req_obj.get("amount") or 0) < min_amount_cents:
            return False, "MPP challenge request amount below required price"
        if expected_resource_url and req_obj.get("resource"):
            got_host = urlsplit(str(req_obj["resource"])).netloc.lower()
            exp_host = urlsplit(expected_resource_url).netloc.lower()
            if got_host and exp_host and got_host != exp_host:
                return False, "MPP challenge resource origin mismatch"
    except Exception:
        return False, "Malformed MPP challenge request payload"
    try:
        exp_ts = datetime.datetime.strptime(expires_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        ).timestamp()
        if exp_ts < time.time():
            return False, "MPP challenge has expired"
    except ValueError:
        return False, "Malformed MPP challenge expires timestamp"
    return True, "ok"


def create_signed_demo_stripe_mpp_credential(resource_url: str) -> tuple[str, dict[str, Any]]:
    """Create a spec-accurate `Authorization: Payment <b64url>` credential for UI/CLI testing."""
    ch = build_stripe_mpp_challenge(resource_url)
    sk = get_effective_stripe_secret()
    # When a Stripe test key (`sk_test_...`) is configured, use Stripe's official test card PaymentMethod `pm_card_visa`!
    token_val = "pm_card_visa" if sk.startswith("sk_test_") else f"spt_demo_{secrets.token_hex(8)}"
    cred_obj = {
        "challenge": {
            "id": ch["id"],
            "realm": ch["realm"],
            "method": ch["method"],
            "intent": ch["intent"],
            "request": ch["request"],
            "expires": ch["expires"],
        },
        "source": "did:stripe:agent_buyer_demo",
        "payload": {
            "type": "payment_method" if token_val.startswith("pm_") else "spt",
            "token": token_val,
        },
    }
    b64_cred = _b64url_encode(
        json.dumps(cred_obj, separators=(",", ":")).encode("utf-8")
    )
    return b64_cred, cred_obj


def build_stripe_payment_receipt_header(
    rail: str, payer: str, tx_or_pi: str | None, charged_usd: float
) -> str:
    receipt_obj = {
        "protocol": "mpp-1.0",
        "method": "stripe",
        "status": "succeeded",
        "rail": rail,
        "payer": payer,
        "paymentIntent": tx_or_pi or "",
        "chargedUsd": charged_usd,
    }
    return _b64url_encode(json.dumps(receipt_obj, separators=(",", ":")).encode("utf-8"))


async def verify_stripe_agent_payment(
    *,
    auth_header: str | None,
    stripe_token_header: str | None,
    owner_passcode_valid: bool,
    resource_url: str | None = None,
    min_amount_cents: int = SCOUT_PRICE_CENTS,
) -> tuple[tuple[str, str, str | None, bool] | None, str]:
    """Verify Stripe `MPP` (`Authorization: Payment <b64url>`) or `X-Stripe-Payment-Token` (`spt_`/`pm_`/`pi_`).

    Returns `((rail, payer, payment_intent_id, is_sandbox), "ok")` on success, or `(None, error_reason)`.
    """
    raw_mpp = ""
    if auth_header and auth_header.strip().lower().startswith("payment "):
        raw_mpp = auth_header.strip()[8:].strip()

    token = (stripe_token_header or "").strip()
    payer = "stripe_agent"
    challenge_verified = False
    challenge_id = ""

    if raw_mpp:
        # Backwards compatibility with legacy shared-secret or sandbox string tokens
        expected_mpp = os.environ.get("RINGSCOUT_MPP_SHARED_SECRET", "").strip()
        if expected_mpp and hmac.compare_digest(expected_mpp, raw_mpp):
            return ("stripe-mpp-1.0", "mpp_authenticated_agent", None, False), "ok"
        if owner_passcode_valid and raw_mpp == "sandbox_mpp_spt_demo":
            return ("stripe-mpp-sandbox", "mpp_sandbox_agent", None, True), "ok"

        try:
            cred = json.loads(_b64url_decode(raw_mpp).decode("utf-8"))
        except Exception:
            return None, "Invalid MPP Payment credential encoding (expected unpadded base64url JSON)"

        ch_obj = cred.get("challenge") or {}
        ok_ch, ch_err = verify_mpp_challenge_binding(
            ch_obj, expected_resource_url=resource_url, min_amount_cents=min_amount_cents
        )
        if not ok_ch:
            return None, ch_err
        challenge_verified = True
        challenge_id = str(ch_obj.get("id") or "")
        payer = str(cred.get("source") or "did:stripe:agent")
        payload = cred.get("payload") or {}
        token = str(payload.get("token") or payload.get("paymentIntentId") or "").strip()

    if not token:
        return None, "Missing Stripe SPT / PaymentMethod / PaymentIntent token"
    if not _STRIPE_TOKEN_RE.match(token):
        return None, "Malformed Stripe token format (expected spt_..., pm_..., or pi_...)"

    sk = get_effective_stripe_secret()
    if sk:
        headers = {"Authorization": f"Bearer {sk}"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                if token.startswith("pi_"):
                    if not _record_or_reject_stripe_replay(token):
                        return None, "Stripe PaymentIntent has already been spent (replay blocked)"
                    resp = await http.get(f"{STRIPE_API_BASE}/payment_intents/{token}", headers=headers)
                    resp.raise_for_status()
                    pi = resp.json()
                    if pi.get("status") != "succeeded":
                        return None, f"Stripe PaymentIntent status is '{pi.get('status')}' (expected 'succeeded')"
                    if str(pi.get("currency") or "").lower() != "usd":
                        return None, f"Stripe PaymentIntent currency '{pi.get('currency')}' must be 'usd'"
                    amt = int(pi.get("amount_received") or pi.get("amount") or 0)
                    if amt < min_amount_cents:
                        return None, f"Stripe PaymentIntent amount ({amt}c) below required {min_amount_cents}c"
                    return ("stripe-payment-intent-live", payer, str(pi["id"]), sk.startswith("sk_test_")), "ok"

                # Charge Shared Payment Token (`spt_...`) or PaymentMethod (`pm_...`) via Stripe PaymentIntents API
                replay_key = challenge_id or token
                if not _record_or_reject_stripe_replay(replay_key):
                    return None, "Stripe MPP payment credential has already been spent (replay blocked)"
                form_data = {
                    "amount": str(min_amount_cents),
                    "currency": "usd",
                    "payment_method": token,
                    "confirm": "true",
                    "automatic_payment_methods[enabled]": "true",
                    "automatic_payment_methods[allow_redirects]": "never",
                    "description": "RingScout Google Maps Voice Insights (A2A / MPP)",
                }
                resp = await http.post(
                    f"{STRIPE_API_BASE}/payment_intents",
                    headers=headers,
                    data=form_data,
                )
                if resp.status_code >= 400:
                    err_msg = ((resp.json() or {}).get("error") or {}).get("message") or f"HTTP {resp.status_code}"
                    return None, f"Stripe API rejected payment token: {err_msg}"
                pi = resp.json()
                if pi.get("status") != "succeeded":
                    return None, f"Stripe charge status '{pi.get('status')}' (requires non-redirect card/SPT)"
                return ("stripe-mpp-spt-live", payer, str(pi["id"]), sk.startswith("sk_test_")), "ok"
        except Exception as exc:
            logger.warning("Stripe API verification error: %s", exc)
            return None, "Stripe API verification failed"

    # When STRIPE_SECRET_KEY is not set yet: allow cryptographically verified MPP challenge + SPT demo for owner
    if challenge_verified and owner_passcode_valid and token.startswith(("spt_", "pm_")):
        replay_key = f"{challenge_id}:{token}"
        if not _record_or_reject_stripe_replay(replay_key):
            return None, "Stripe MPP payment credential has already been spent (replay blocked)"
        sim_pi = f"pi_mpp_verified_{hashlib.sha256(replay_key.encode('utf-8')).hexdigest()[:14]}"
        return ("stripe-mpp-spt-verified", payer, sim_pi, True), "ok"

    return (
        None,
        "Live Stripe settlement requires STRIPE_SECRET_KEY configured on server (or owner passcode for MPP SPT handshake demo).",
    )
