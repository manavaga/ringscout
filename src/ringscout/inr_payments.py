"""India INR / UPI Agentic Payments (Razorpay Payment Links + Atomic Credit Ledger).

Enables end-to-end fiat INR payments via UPI (GPay, PhonePe, Paytm, BHIM) and Indian cards
without requiring a Stripe account or US entity:
  1. `POST /api/v1/inr/topup` & MCP tool `buy_ringscout_inr_credits`:
     Creates a Razorpay dynamic UPI Payment Link (`POST /v1/payment_links`, `upi_link=true`, `currency="INR"`)
     for prepaid call packs (₹19 / ₹49 / ₹99 / ₹499) and issues a caller API key (`rk_live_...` / `rk_test_...`).
  2. `POST /api/payments/razorpay/webhook`:
     Verifies `X-Razorpay-Signature` HMAC-SHA256 over the exact raw request body, enforces INR currency and
     minimum paise amount against server-side pack tables, deduplicates on both `payment_id` and `order_id`,
     and atomically credits the caller's balance.
  3. `GET /payments/inr/success`:
     Verifies Razorpay callback HMAC signature + order/plink binding as an instant activation fast-path
     (idempotent with webhook) and renders an escaped receipt page.
  4. Persistent atomic storage:
     Uses Google Cloud Firestore REST `:commit` transactions & `FieldTransform.increment` via ADC on Cloud Run
     (zero extra pip dependencies) so paid INR balances survive restarts and never race across rolling deploys,
     with seamless in-memory storage for local development and pytest.
"""

from __future__ import annotations

import asyncio
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

import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
import httpx

from ringscout.secrets_manager import get_secret

logger = logging.getLogger(__name__)

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
INR_PER_USD_ESTIMATE = 85.0

INR_CREDIT_PACKS: dict[str, dict[str, Any]] = {
    "starter_19": {
        "pack_id": "starter_19",
        "label": "Starter Pack — 10 Live +91 Calls",
        "amount_inr": 19,
        "amount_paise": 1900,
        "credits": 10,
        "per_call_inr": 1.90,
        "recommended": False,
    },
    "pro_49": {
        "pack_id": "pro_49",
        "label": "Pro Pack — 30 Live +91 Calls",
        "amount_inr": 49,
        "amount_paise": 4900,
        "credits": 30,
        "per_call_inr": 1.63,
        "recommended": True,
    },
    "scale_99": {
        "pack_id": "scale_99",
        "label": "Power Pack — 75 Live +91 Calls",
        "amount_inr": 99,
        "amount_paise": 9900,
        "credits": 75,
        "per_call_inr": 1.32,
        "recommended": False,
    },
    "agent_499": {
        "pack_id": "agent_499",
        "label": "Agent Builder Pack — 400 Live +91 Calls",
        "amount_inr": 499,
        "amount_paise": 49900,
        "credits": 400,
        "per_call_inr": 1.25,
        "recommended": False,
    },
}

_API_KEY_RE = re.compile(r"^rk_(?:live|test)_[A-Za-z0-9_-]{16,96}$")
_GH_HANDLE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


@dataclass
class RuntimeRazorpayConfig:
    key_id: str = ""
    key_secret: str = ""
    webhook_secret: str = ""


_RUNTIME_RZP_CFG = RuntimeRazorpayConfig()


def get_effective_razorpay_credentials() -> tuple[str, str, str]:
    """Resolve Razorpay `(key_id, key_secret, webhook_secret)` from runtime, env, or GCP Secret Manager."""
    key_id = (
        _RUNTIME_RZP_CFG.key_id.strip()
        or os.environ.get("RAZORPAY_KEY_ID", "").strip()
        or get_secret("RAZORPAY_KEY_ID")
    )
    key_secret = (
        _RUNTIME_RZP_CFG.key_secret.strip()
        or os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
        or get_secret("RAZORPAY_KEY_SECRET")
    )
    webhook_secret = (
        _RUNTIME_RZP_CFG.webhook_secret.strip()
        or os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
        or get_secret("RAZORPAY_WEBHOOK_SECRET")
    )
    return key_id, key_secret, webhook_secret


def update_runtime_razorpay_config(
    *,
    key_id: str | None = None,
    key_secret: str | None = None,
    webhook_secret: str | None = None,
) -> dict[str, Any]:
    if key_id is not None:
        _RUNTIME_RZP_CFG.key_id = key_id.strip()
    if key_secret is not None:
        _RUNTIME_RZP_CFG.key_secret = key_secret.strip()
    if webhook_secret is not None:
        _RUNTIME_RZP_CFG.webhook_secret = webhook_secret.strip()
    return masked_razorpay_status()


def masked_razorpay_status() -> dict[str, Any]:
    key_id, key_secret, webhook_secret = get_effective_razorpay_credentials()
    mode = (
        "live"
        if key_id.startswith("rzp_live_")
        else ("test" if key_id.startswith("rzp_test_") else "unconfigured")
    )
    masked_id = f"{key_id[:10]}...{key_id[-4:]}" if len(key_id) > 14 else (key_id or None)
    return {
        "configured": bool(key_id and key_secret),
        "webhook_configured": bool(webhook_secret),
        "mode": mode,
        "key_id_masked": masked_id,
        "currency": "INR",
        "upi_enabled": True,
        "packs": list(INR_CREDIT_PACKS.values()),
        "ledger_stats": INR_CREDIT_LEDGER.summary_stats(),
    }


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def mask_api_key(raw_key: str) -> str:
    clean = (raw_key or "").strip()
    if len(clean) < 12:
        return "rk_••••"
    return f"{clean[:8]}••••{clean[-4:]}"


def is_valid_ringscout_api_key(raw_key: str | None) -> bool:
    return bool(raw_key and _API_KEY_RE.match(raw_key.strip()))


def generate_ringscout_api_key(is_live: bool = True) -> str:
    prefix = "rk_live_" if is_live else "rk_test_"
    return prefix + secrets.token_urlsafe(24)


def verify_razorpay_webhook_signature(
    raw_body: bytes, signature_header: str | None, webhook_secret: str
) -> bool:
    """Verify Razorpay `X-Razorpay-Signature` HMAC-SHA256 over the exact raw request body bytes."""
    if not raw_body or not signature_header or not webhook_secret:
        return False
    expected = hmac.new(
        webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def verify_razorpay_callback_signature(
    *,
    payment_link_id: str,
    payment_link_reference_id: str,
    payment_link_status: str,
    payment_id: str,
    signature: str,
    key_secret: str,
) -> bool:
    """Verify Razorpay Payment Link redirect callback HMAC-SHA256 signature."""
    if not all([payment_link_id, payment_link_reference_id, payment_link_status, payment_id, signature, key_secret]):
        return False
    payload = f"{payment_link_id}|{payment_link_reference_id}|{payment_link_status}|{payment_id}"
    expected = hmac.new(
        key_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class _FirestoreRestAdapter:
    """Atomic Firestore REST client using ADC + httpx (zero extra pip deps).

    Design invariants:
    - `get_doc_with_meta` returns `(None, None)` ONLY on HTTP 404 (never on 403/5xx/timeouts).
    - `commit_credit_payment` uses Firestore `:commit` with `currentDocument.exists=false` on the
      event & order markers plus `FieldTransform.increment` on balances, guaranteeing idempotency
      and zero lost updates across rolling Cloud Run instances.
    - `commit_consume_credit` uses optimistic locking (`currentDocument.updateTime`) with retry.
    """

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.doc_root = f"projects/{project_id}/databases/(default)/documents"
        self.base_url = f"https://firestore.googleapis.com/v1/{self.doc_root}"

    def is_enabled(self) -> bool:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        return bool(os.environ.get("K_SERVICE") or os.environ.get("RINGSCOUT_FIRESTORE_ENABLED") == "1")

    def _get_token(self) -> str:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/datastore"])
        creds.refresh(GoogleAuthRequest())
        if not creds.token:
            raise RuntimeError("Firestore ADC token unavailable")
        return str(creds.token)

    async def get_doc_with_meta(
        self, collection: str, doc_id: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not self.is_enabled():
            return None, None
        token = await asyncio.to_thread(self._get_token)
        url = f"{self.base_url}/{collection}/{doc_id}"
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 404:
                return None, None
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Firestore get_doc failed ({collection}/{doc_id}): HTTP {resp.status_code}"
                )
            body = resp.json()
            return self._decode_fields(body.get("fields") or {}), body.get("updateTime")

    async def put_doc(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        if not self.is_enabled():
            return
        token = await asyncio.to_thread(self._get_token)
        url = f"{self.base_url}/{collection}/{doc_id}"
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.patch(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"fields": self._encode_fields(data)},
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Firestore put_doc failed ({collection}/{doc_id}): HTTP {resp.status_code}"
                )

    async def commit_credit_payment(
        self,
        *,
        payment_id: str,
        order_id: str,
        key_hash: str,
        credits: int,
        amount_inr: int,
        key_masked: str,
        github_username: str,
        pack_id: str,
        now_iso: str,
    ) -> bool:
        """Atomically create replay marker(s) (`exists=false`) and increment key balance.

        Returns True if newly committed, False if `payment_id` or `order_id` already existed (409/400).
        Raises `RuntimeError` on any transient/permission/5xx failure so webhooks retry.
        """
        token = await asyncio.to_thread(self._get_token)
        event_doc = f"{self.doc_root}/ringscout_razorpay_events/{payment_id}"
        acct_doc = f"{self.doc_root}/ringscout_credit_keys/{key_hash}"

        meta_fields: dict[str, Any] = {
            "key_hash": key_hash,
            "key_masked": key_masked,
            "updated_at": now_iso,
        }
        if github_username:
            meta_fields["github_username"] = github_username

        writes: list[dict[str, Any]] = [
            {
                "update": {
                    "name": event_doc,
                    "fields": self._encode_fields(
                        {
                            "payment_id": payment_id,
                            "order_id": order_id,
                            "key_hash": key_hash,
                            "pack_id": pack_id,
                            "amount_inr": int(amount_inr),
                            "credits": int(credits),
                            "credited_at": now_iso,
                        }
                    ),
                },
                "currentDocument": {"exists": False},
            },
        ]
        if order_id:
            order_marker_doc = f"{self.doc_root}/ringscout_razorpay_orders/{order_id}"
            writes.append(
                {
                    "update": {
                        "name": order_marker_doc,
                        "fields": self._encode_fields(
                            {
                                "order_id": order_id,
                                "payment_id": payment_id,
                                "key_hash": key_hash,
                                "credited_at": now_iso,
                            }
                        ),
                    },
                    "currentDocument": {"exists": False},
                }
            )
        writes.append(
            {
                "update": {
                    "name": acct_doc,
                    "fields": self._encode_fields(meta_fields),
                },
                "updateMask": {"fieldPaths": list(meta_fields.keys())},
                "updateTransforms": [
                    {"fieldPath": "credits_remaining", "increment": {"integerValue": str(int(credits))}},
                    {"fieldPath": "credits_purchased", "increment": {"integerValue": str(int(credits))}},
                    {"fieldPath": "credits_used", "increment": {"integerValue": "0"}},
                    {"fieldPath": "total_paid_inr", "increment": {"integerValue": str(int(amount_inr))}},
                ],
            }
        )

        async with httpx.AsyncClient(timeout=6.0) as http:
            resp = await http.post(
                f"{self.base_url}:commit",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"writes": writes},
            )
            if resp.status_code in (409, 412):
                return False
            if resp.status_code == 400 and "ALREADY_EXISTS" in resp.text:
                return False
            if resp.status_code >= 400:
                raise RuntimeError(f"Firestore commit_credit_payment failed: HTTP {resp.status_code}")
            return True

    async def commit_consume_credit(
        self, *, key_hash: str, cost_credits: int
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """Optimistically lock `ringscout_credit_keys/{key_hash}` on `updateTime` and deduct `cost_credits`."""
        token = await asyncio.to_thread(self._get_token)
        acct_doc = f"{self.doc_root}/ringscout_credit_keys/{key_hash}"

        for _attempt in range(3):
            acc, update_time = await self.get_doc_with_meta("ringscout_credit_keys", key_hash)
            if acc is None or not update_time:
                return False, None, "Prepaid INR key has no active credits yet (complete UPI payment first)"
            rem = int(acc.get("credits_remaining", 0))
            if rem < cost_credits:
                return (
                    False,
                    dict(acc),
                    f"Prepaid INR balance exhausted ({rem} credits remaining). Top up via UPI at POST /api/v1/inr/topup.",
                )
            now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            updated = dict(acc)
            updated["credits_remaining"] = rem - cost_credits
            updated["credits_used"] = int(acc.get("credits_used", 0)) + cost_credits
            updated["updated_at"] = now_iso

            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(
                    f"{self.base_url}:commit",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={
                        "writes": [
                            {
                                "update": {
                                    "name": acct_doc,
                                    "fields": self._encode_fields(updated),
                                },
                                "currentDocument": {"updateTime": update_time},
                            }
                        ]
                    },
                )
                if resp.status_code in (409, 412) or (
                    resp.status_code == 400 and "FAILED_PRECONDITION" in resp.text
                ):
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"Firestore commit_consume_credit failed: HTTP {resp.status_code}")
                return True, updated, "ok"

        raise RuntimeError("Firestore credit deduction contention (please retry)")

    @staticmethod
    def _encode_fields(data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, bool):
                out[k] = {"booleanValue": v}
            elif isinstance(v, int):
                out[k] = {"integerValue": str(v)}
            elif isinstance(v, float):
                out[k] = {"doubleValue": v}
            elif v is None:
                out[k] = {"nullValue": None}
            else:
                out[k] = {"stringValue": str(v)}
        return out

    @staticmethod
    def _decode_fields(fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, val in fields.items():
            if not isinstance(val, dict):
                continue
            if "integerValue" in val:
                out[k] = int(val["integerValue"])
            elif "doubleValue" in val:
                out[k] = float(val["doubleValue"])
            elif "booleanValue" in val:
                out[k] = bool(val["booleanValue"])
            elif "stringValue" in val:
                out[k] = str(val["stringValue"])
            elif "nullValue" in val:
                out[k] = None
        return out


class InrCreditLedger:
    """Async-safe prepaid INR call-credit ledger with dual payment/order idempotency & atomic Firestore persistence."""

    def __init__(self, project_id: str | None = None) -> None:
        self._lock = asyncio.Lock()
        # key_hash -> {credits_remaining, credits_purchased, credits_used, total_paid_inr, key_masked, github_username, updated_at}
        self._accounts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # order_id -> pending order metadata (no plaintext api_key stored)
        self._pending_orders: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # payment_id -> credited_at_iso
        self._processed_payments: OrderedDict[str, str] = OrderedDict()
        # order_id -> payment_id (prevents double-credit across payment_link.paid + payment.captured)
        self._processed_orders: OrderedDict[str, str] = OrderedDict()
        self._firestore = _FirestoreRestAdapter(
            project_id or os.environ.get("GCP_PROJECT_ID", "manav-chub").strip() or "manav-chub"
        )

    def reset_for_tests(self) -> None:
        self._accounts.clear()
        self._pending_orders.clear()
        self._processed_payments.clear()
        self._processed_orders.clear()

    def register_pending_order(
        self,
        *,
        order_id: str,
        api_key: str,
        pack_id: str,
        amount_inr: int,
        credits: int,
        github_username: str = "",
        plink_id: str = "",
        short_url: str = "",
    ) -> dict[str, Any]:
        khash = hash_api_key(api_key)
        clean_gh = github_username.strip().lstrip("@").lower()
        if clean_gh and not _GH_HANDLE_RE.match(clean_gh):
            clean_gh = ""
        rec = {
            "order_id": order_id,
            "key_hash": khash,
            "key_masked": mask_api_key(api_key),
            "pack_id": pack_id,
            "amount_inr": int(amount_inr),
            "credits": int(credits),
            "github_username": clean_gh,
            "plink_id": plink_id,
            "short_url": short_url,
            "status": "created",
            "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self._pending_orders[order_id] = rec
        while len(self._pending_orders) > 2048:
            self._pending_orders.popitem(last=False)
        return rec

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self._pending_orders.get(order_id)

    async def save_pending_order_remote(self, rec: dict[str, Any]) -> None:
        order_id = str(rec.get("order_id") or "")
        if order_id and self._firestore.is_enabled():
            try:
                await self._firestore.put_doc("ringscout_pending_orders", order_id, rec)
            except Exception as exc:
                logger.warning("Pending order Firestore write failed (%s): %s", order_id, exc)

    async def get_order_async(self, order_id: str) -> dict[str, Any] | None:
        if not order_id:
            return None
        local = self._pending_orders.get(order_id)
        if local is not None:
            return local
        if self._firestore.is_enabled():
            remote, _ = await self._firestore.get_doc_with_meta("ringscout_pending_orders", order_id)
            if remote is not None:
                self._pending_orders[order_id] = remote
                return remote
        return None

    async def credit_payment(
        self,
        *,
        payment_id: str,
        key_hash: str,
        credits: int,
        amount_inr: int,
        key_masked: str = "rk_••••",
        github_username: str = "",
        order_id: str = "",
        pack_id: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically and idempotently credit a verified Razorpay INR payment.

        Deduplicates on BOTH `payment_id` and `order_id` to prevent double-crediting when
        Razorpay delivers both `payment_link.paid` and `payment.captured` or callback + webhook.
        Raises `RuntimeError` if Firestore persistence fails so Razorpay retries the webhook.
        """
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        clean_gh = github_username.strip().lstrip("@").lower()
        if clean_gh and not _GH_HANDLE_RE.match(clean_gh):
            clean_gh = ""

        async with self._lock:
            if payment_id in self._processed_payments or (
                order_id and order_id in self._processed_orders
            ):
                acc = await self._read_account_authoritative(key_hash)
                return False, acc or {"credits_remaining": 0, "total_paid_inr": 0}

            if self._firestore.is_enabled():
                committed = await self._firestore.commit_credit_payment(
                    payment_id=payment_id,
                    order_id=order_id,
                    key_hash=key_hash,
                    credits=credits,
                    amount_inr=amount_inr,
                    key_masked=key_masked,
                    github_username=clean_gh,
                    pack_id=pack_id,
                    now_iso=now_iso,
                )
                self._processed_payments[payment_id] = now_iso
                while len(self._processed_payments) > 4096:
                    self._processed_payments.popitem(last=False)
                if order_id:
                    self._processed_orders[order_id] = payment_id
                    while len(self._processed_orders) > 4096:
                        self._processed_orders.popitem(last=False)
                acc = await self._read_account_authoritative(key_hash)
                if acc is not None:
                    self._accounts[key_hash] = acc
                if order_id and order_id in self._pending_orders:
                    self._pending_orders[order_id]["status"] = "paid"
                    self._pending_orders[order_id]["payment_id"] = payment_id
                return committed, dict(acc or {"credits_remaining": 0, "total_paid_inr": 0})

            # In-memory atomic path (local dev & pytest)
            acc = self._accounts.get(key_hash)
            if acc is None:
                acc = {
                    "key_hash": key_hash,
                    "key_masked": key_masked,
                    "credits_remaining": 0,
                    "credits_purchased": 0,
                    "credits_used": 0,
                    "total_paid_inr": 0,
                    "github_username": "",
                    "updated_at": now_iso,
                }
            acc["credits_remaining"] = int(acc.get("credits_remaining", 0)) + int(credits)
            acc["credits_purchased"] = int(acc.get("credits_purchased", 0)) + int(credits)
            acc["total_paid_inr"] = int(acc.get("total_paid_inr", 0)) + int(amount_inr)
            if key_masked and key_masked != "rk_••••":
                acc["key_masked"] = key_masked
            if clean_gh:
                acc["github_username"] = clean_gh
            acc["updated_at"] = now_iso

            self._accounts[key_hash] = acc
            self._processed_payments[payment_id] = now_iso
            while len(self._processed_payments) > 4096:
                self._processed_payments.popitem(last=False)
            if order_id:
                self._processed_orders[order_id] = payment_id
                while len(self._processed_orders) > 4096:
                    self._processed_orders.popitem(last=False)
                if order_id in self._pending_orders:
                    self._pending_orders[order_id]["status"] = "paid"
                    self._pending_orders[order_id]["payment_id"] = payment_id
            return True, dict(acc)

    async def _read_account_authoritative(self, key_hash: str) -> dict[str, Any] | None:
        if self._firestore.is_enabled():
            remote, _ = await self._firestore.get_doc_with_meta("ringscout_credit_keys", key_hash)
            if remote is not None:
                self._accounts[key_hash] = remote
            return remote
        return self._accounts.get(key_hash)

    async def get_balance(self, *, api_key: str | None = None) -> dict[str, Any] | None:
        """Return credit balance strictly for a valid authenticated `rk_...` API key."""
        if not api_key or not is_valid_ringscout_api_key(api_key):
            return None
        khash = hash_api_key(api_key)
        acc = await self._read_account_authoritative(khash)
        if not acc:
            return None
        return {
            "key_masked": acc.get("key_masked", "rk_••••"),
            "credits_remaining": int(acc.get("credits_remaining", 0)),
            "credits_purchased": int(acc.get("credits_purchased", 0)),
            "credits_used": int(acc.get("credits_used", 0)),
            "total_paid_inr": int(acc.get("total_paid_inr", 0)),
            "github_username": acc.get("github_username") or None,
            "updated_at": acc.get("updated_at"),
        }

    async def consume_credit(
        self,
        *,
        api_key: str | None = None,
        cost_credits: int = 1,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """Atomically deduct `cost_credits` strictly for a valid authenticated `rk_...` API key."""
        if not api_key or not is_valid_ringscout_api_key(api_key):
            return False, None, "Invalid or unknown RingScout prepaid INR API key"
        khash = hash_api_key(api_key)

        if self._firestore.is_enabled():
            ok, updated, msg = await self._firestore.commit_consume_credit(
                key_hash=khash, cost_credits=cost_credits
            )
            if updated is not None:
                self._accounts[khash] = updated
            return ok, updated, msg

        async with self._lock:
            acc = self._accounts.get(khash)
            if not acc:
                return False, None, "Prepaid INR key has no active credits yet (complete UPI payment first)"
            rem = int(acc.get("credits_remaining", 0))
            if rem < cost_credits:
                return (
                    False,
                    dict(acc),
                    f"Prepaid INR balance exhausted ({rem} credits remaining). Top up via UPI at POST /api/v1/inr/topup.",
                )
            now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            acc["credits_remaining"] = rem - cost_credits
            acc["credits_used"] = int(acc.get("credits_used", 0)) + cost_credits
            acc["updated_at"] = now_iso
            self._accounts[khash] = acc
            return True, dict(acc), "ok"

    def summary_stats(self) -> dict[str, Any]:
        total_inr = sum(int(a.get("total_paid_inr", 0)) for a in self._accounts.values())
        total_credits_sold = sum(int(a.get("credits_purchased", 0)) for a in self._accounts.values())
        total_credits_used = sum(int(a.get("credits_used", 0)) for a in self._accounts.values())
        return {
            "active_paid_keys": len(self._accounts),
            "paid_orders_processed": len(self._processed_payments),
            "total_inr_collected": total_inr,
            "total_credits_sold": total_credits_sold,
            "total_credits_used": total_credits_used,
        }


INR_CREDIT_LEDGER = InrCreditLedger()


async def create_razorpay_upi_topup(
    *,
    pack_id: str,
    base_url: str,
    existing_api_key: str | None = None,
    github_username: str = "",
    customer_name: str = "",
    customer_email: str = "",
    customer_phone: str = "",
) -> dict[str, Any]:
    """Create a dynamic Razorpay UPI Payment Link in INR and return the checkout URL + caller API key."""
    clean_pack = (pack_id or "pro_49").strip().lower()
    pack = INR_CREDIT_PACKS.get(clean_pack)
    if not pack:
        raise ValueError(
            f"Unknown INR pack '{pack_id}'. Valid packs: {', '.join(INR_CREDIT_PACKS.keys())}"
        )

    key_id, key_secret, _webhook_secret = get_effective_razorpay_credentials()
    if not key_id or not key_secret:
        raise RuntimeError(
            "RAZORPAY_NOT_CONFIGURED: Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in /admin or GCP Secret Manager "
            "(create a free Individual account at https://dashboard.razorpay.com/signup — Test Mode keys work in 2 minutes)."
        )

    is_live = key_id.startswith("rzp_live_")
    expected_prefix = "rk_live_" if is_live else "rk_test_"
    clean_existing = (existing_api_key or "").strip()
    if clean_existing:
        if not is_valid_ringscout_api_key(clean_existing):
            raise ValueError("Invalid existing_api_key format (expected rk_live_... or rk_test_...)")
        if not clean_existing.startswith(expected_prefix):
            raise ValueError(
                f"API key mode mismatch: server is in {'live' if is_live else 'test'} Razorpay mode "
                f"(expected '{expected_prefix}...' key)."
            )
        api_key = clean_existing
        is_new_key = False
    else:
        api_key = generate_ringscout_api_key(is_live=is_live)
        is_new_key = True

    order_id = f"rs_inr_{secrets.token_hex(8)}"
    key_hash = hash_api_key(api_key)
    clean_gh = github_username.strip().lstrip("@")
    clean_base = base_url.rstrip("/")
    expire_by = int(time.time()) + 3600  # 1 hour UPI checkout expiry

    payload: dict[str, Any] = {
        "upi_link": True,
        "amount": int(pack["amount_paise"]),
        "currency": "INR",
        "accept_partial": False,
        "reference_id": order_id,
        "description": f"RingScout {pack['label']} (₹{pack['amount_inr']})",
        "expire_by": expire_by,
        "reminder_enable": False,
        "notify": {"sms": False, "email": False},
        "notes": {
            "order_id": order_id,
            "pack_id": pack["pack_id"],
            "credits": str(pack["credits"]),
            "amount_inr": str(pack["amount_inr"]),
            "key_hash": key_hash,
            "key_masked": mask_api_key(api_key),
            "github_username": clean_gh[:40],
        },
        "callback_url": f"{clean_base}/payments/inr/success?order_id={order_id}",
        "callback_method": "get",
    }
    customer_obj: dict[str, str] = {}
    if customer_name.strip():
        customer_obj["name"] = customer_name.strip()[:60]
    if customer_email.strip() and "@" in customer_email:
        customer_obj["email"] = customer_email.strip()[:80]
    digits_phone = re.sub(r"\D", "", customer_phone or "")
    if len(digits_phone) >= 10:
        customer_obj["contact"] = f"+91{digits_phone[-10:]}"
    if customer_obj:
        payload["customer"] = customer_obj

    async with httpx.AsyncClient(timeout=12.0) as http:
        resp = await http.post(
            f"{RAZORPAY_API_BASE}/payment_links",
            auth=(key_id, key_secret),
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise RuntimeError(f"Razorpay Payment Link creation failed (HTTP {resp.status_code}): {detail}")
        rzp_data = resp.json()

    short_url = str(rzp_data.get("short_url") or "")
    plink_id = str(rzp_data.get("id") or "")
    pending_rec = INR_CREDIT_LEDGER.register_pending_order(
        order_id=order_id,
        api_key=api_key,
        pack_id=pack["pack_id"],
        amount_inr=int(pack["amount_inr"]),
        credits=int(pack["credits"]),
        github_username=clean_gh,
        plink_id=plink_id,
        short_url=short_url,
    )
    await INR_CREDIT_LEDGER.save_pending_order_remote(pending_rec)

    return {
        "order_id": order_id,
        "razorpay_payment_link_id": plink_id,
        "mode": "live" if is_live else "test",
        "pack_id": pack["pack_id"],
        "label": pack["label"],
        "amount_inr": pack["amount_inr"],
        "currency": "INR",
        "credits": pack["credits"],
        "upi_payment_url": short_url,
        "api_key": api_key,
        "is_new_key": is_new_key,
        "github_username": clean_gh or None,
        "expires_at": datetime.datetime.fromtimestamp(expire_by, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "instructions": (
            f"1. Open {short_url} and pay ₹{pack['amount_inr']} via GPay, PhonePe, Paytm, or any UPI app. "
            f"2. Save your API key ({mask_api_key(api_key)}) — it activates automatically upon UPI confirmation "
            "and works across ChatGPT, Claude, Gemini, Cursor, MCP (`api_key`), and REST (`X-RingScout-Key`)."
        ),
    }


async def process_razorpay_webhook_event(
    raw_body: bytes, signature_header: str | None
) -> dict[str, Any]:
    """Verify Razorpay webhook signature and credit the buyer's INR balance atomically and idempotently."""
    _kid, _ksec, webhook_secret = get_effective_razorpay_credentials()
    if not webhook_secret:
        raise RuntimeError("RAZORPAY_WEBHOOK_SECRET not configured on server")
    if not verify_razorpay_webhook_signature(raw_body, signature_header, webhook_secret):
        raise PermissionError("Invalid X-Razorpay-Signature HMAC-SHA256 digest")

    evt = json.loads(raw_body.decode("utf-8"))
    event_name = str(evt.get("event") or "")
    if event_name not in ("payment_link.paid", "payment.captured"):
        return {"ok": True, "ignored_event": event_name}

    payload = evt.get("payload") or {}
    plink_entity = (payload.get("payment_link") or {}).get("entity") or {}
    pay_entity = (payload.get("payment") or {}).get("entity") or {}

    notes = plink_entity.get("notes") or pay_entity.get("notes") or {}
    payment_id = str(pay_entity.get("id") or plink_entity.get("id") or "").strip()
    order_id = str(notes.get("order_id") or plink_entity.get("reference_id") or "").strip()
    key_hash = str(notes.get("key_hash") or "").strip()
    key_masked = str(notes.get("key_masked") or "rk_••••").strip()
    pack_id = str(notes.get("pack_id") or "").strip()
    github_username = str(notes.get("github_username") or "").strip()

    pending = await INR_CREDIT_LEDGER.get_order_async(order_id) if order_id else None
    if pending:
        key_hash = key_hash or str(pending.get("key_hash") or "")
        key_masked = key_masked if key_masked != "rk_••••" else str(pending.get("key_masked") or "rk_••••")
        pack_id = pack_id or str(pending.get("pack_id") or "")
        github_username = github_username or str(pending.get("github_username") or "")

    pack = INR_CREDIT_PACKS.get(pack_id)
    if not pack:
        return {"ok": False, "reason": f"Unknown or missing pack_id '{pack_id}' in Razorpay event"}

    currency = str(pay_entity.get("currency") or plink_entity.get("currency") or "").upper()
    if currency != "INR":
        return {"ok": False, "reason": f"Unsupported currency '{currency}' (expected INR)"}

    raw_amount = pay_entity.get("amount") if pay_entity.get("amount") is not None else plink_entity.get("amount")
    try:
        amount_paise = int(raw_amount) if raw_amount is not None else 0
    except (TypeError, ValueError):
        amount_paise = 0

    if amount_paise < int(pack["amount_paise"]):
        return {
            "ok": False,
            "reason": f"Underpaid or missing amount ({amount_paise} paise < expected {pack['amount_paise']} paise)",
        }

    amount_inr = amount_paise // 100
    credits = int(pack["credits"])

    if not payment_id or not key_hash:
        return {"ok": False, "reason": "Missing payment_id or key_hash in Razorpay event"}

    newly_credited, acc = await INR_CREDIT_LEDGER.credit_payment(
        payment_id=payment_id,
        key_hash=key_hash,
        credits=credits,
        amount_inr=amount_inr,
        key_masked=key_masked,
        github_username=github_username,
        order_id=order_id,
        pack_id=pack_id,
    )
    return {
        "ok": True,
        "event": event_name,
        "newly_credited": newly_credited,
        "payment_id": payment_id,
        "order_id": order_id,
        "amount_inr": amount_inr,
        "credits_added": credits if newly_credited else 0,
        "credits_remaining": int(acc.get("credits_remaining", 0)),
    }
