#!/usr/bin/env python3
"""Runnable Buyer Agent Demo for RingScout (A2A v1.0 + India INR UPI + Always-402 $0.05 x402).

Demonstrates an autonomous external buyer agent discovering RingScout's capabilities,
inspecting the HTTP 402 payment challenge ($0.05 Base USDC + Razorpay INR UPI packs),
and executing a real-time Google Maps voice scout using either:
  - Prepaid India INR UPI key (`RINGSCOUT_API_KEY=rk_...`), OR
  - GitHub stargazer free tier (`RINGSCOUT_GITHUB_USER=<handle>`), OR
  - Sandbox swarm dry-run (`--dry-run`) to inspect the full A2A/x402 handshake.

Usage:
  python3 examples/buyer_agent_demo.py
  RINGSCOUT_API_KEY="rk_live_..." python3 examples/buyer_agent_demo.py --live
  python3 examples/buyer_agent_demo.py --buy-inr starter_19
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://ringscout-1004579867040.us-central1.run.app"


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json", "User-Agent": "RingScout-BuyerAgent-Demo/1.0"}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return err.code, dict(err.headers), body


def main() -> int:
    parser = argparse.ArgumentParser(description="RingScout Autonomous Buyer Agent Demo")
    parser.add_argument("--base-url", default=os.environ.get("RINGSCOUT_URL", DEFAULT_BASE_URL))
    parser.add_argument("--city", default="Mumbai")
    parser.add_argument("--locality", default="Bandra West")
    parser.add_argument("--category", default="restaurant")
    parser.add_argument(
        "--question",
        default="Table for 4 tonight at 8:30 PM and current wait time",
    )
    parser.add_argument("--live", action="store_true", help="Place real +91 PSTN calls (default is simulated swarm dry-run)")
    parser.add_argument("--buy-inr", metavar="PACK_ID", help="Generate a Razorpay INR UPI checkout link (starter_19, pro_49, scale_99, agent_499)")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    api_key = os.environ.get("RINGSCOUT_API_KEY", "").strip()
    gh_user = os.environ.get("RINGSCOUT_GITHUB_USER", "").strip()

    print(f"1) Discovering A2A Agent Card at {base}/.well-known/agent-card.json ...")
    st, _, card = http_json(f"{base}/.well-known/agent-card.json")
    print(f"   HTTP {st} | Service: {card.get('name')} | Rails: {card.get('payment', {}).get('supported_rails')}")

    print(f"\n2) Probing Always-402 x402 Micro-Rail at {base}/api/v1/x402/scout ...")
    st402, _, ch402 = http_json(f"{base}/api/v1/x402/scout")
    accepts = (ch402.get("accepts") or [{}])[0]
    inr_info = ch402.get("inr_upi") or {}
    print(
        f"   HTTP {st402} | Base USDC atomic={accepts.get('amount')} ($0.05) -> {accepts.get('payTo')}\n"
        f"   India UPI topup: {inr_info.get('topup_endpoint')} (starts at ₹{inr_info.get('starting_pack_inr')})"
    )

    if args.buy_inr:
        print(f"\n3) Generating Razorpay INR UPI Payment Link for pack '{args.buy_inr}' ...")
        st_top, _, top = http_json(
            f"{base}/api/v1/inr/topup",
            method="POST",
            payload={"pack_id": args.buy_inr, "github_username": gh_user},
        )
        print(f"   HTTP {st_top}: {json.dumps(top, indent=2)}")
        return 0 if st_top == 200 else 1

    endpoint = f"{base}/api/v1/x402/scout" if api_key else f"{base}/api/v1/scout"
    exec_mode = "auto" if args.live else "swarm"
    req_headers: dict[str, str] = {}
    if api_key:
        req_headers["X-RingScout-Key"] = api_key
    if gh_user:
        req_headers["X-GitHub-Username"] = gh_user

    print(f"\n3) Calling {endpoint} (execution_mode={exec_mode}) ...")
    st_call, resp_hdrs, result = http_json(
        endpoint,
        method="POST",
        headers=req_headers,
        payload={
            "place_category": args.category,
            "city": args.city,
            "locality": args.locality,
            "item_question": args.question,
            "max_shops": 2,
            "execution_mode": exec_mode,
        },
    )
    print(f"   HTTP {st_call}")
    if st_call == 402:
        print("   Payment Required! Pass RINGSCOUT_API_KEY=rk_... (from --buy-inr starter_19) or x402 PAYMENT-SIGNATURE.")
        print(f"   Detail: {result.get('error')}")
        return 0

    settlement = result.get("settlement") or {}
    print(f"   Settled via: {settlement.get('rail')} ({settlement.get('network')}) | Best match:")
    print(json.dumps(result.get("best_match") or result.get("ranked_quotes", [])[:1], indent=2, ensure_ascii=False))
    if resp_hdrs.get("PAYMENT-RESPONSE"):
        print("   PAYMENT-RESPONSE receipt header received ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
