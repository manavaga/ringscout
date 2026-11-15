#!/usr/bin/env python3
"""Standalone Agent-to-Agent (A2A v1.0) Buyer Client supporting:
- `--rail open`: Phase A frictionless out-of-the-box A2A (`message/send`)
- `--rail stripe`: Phase B Stripe Machine Payments Protocol (`WWW-Authenticate: Payment` -> `Authorization: Payment`)
- `--rail x402`: Phase B Cryptographic EIP-3009 `TransferWithAuthorization` Base USDC
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

import httpx

from ringscout.x402_crypto import (
    generate_evm_keypair,
    private_key_hex_to_address,
    sign_x402_exact_evm_payload,
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def main() -> int:
    parser = argparse.ArgumentParser(description="RingScout A2A v1.0 Buyer Agent (Open / Stripe MPP / x402)")
    parser.add_argument(
        "--url",
        default="https://ringscout-hom7ntreuq-uc.a.run.app",
        help="Base URL of deployed RingScout service",
    )
    parser.add_argument(
        "--rail",
        choices=["open", "stripe", "x402"],
        default="open",
        help="Settlement mode: 'open' (Phase A free), 'stripe' (Stripe MPP / SPT), or 'x402' (Base USDC)",
    )
    parser.add_argument(
        "--category",
        default="hotel",
        help="Google Maps location category (hotel, restaurant, clinic, diagnostics, petrol, auto, salon, pharmacy, custom)",
    )
    parser.add_argument(
        "--message",
        default="Deluxe double room in Bandra West, Mumbai tonight — confirm 10 AM early check-in and direct phone tariff in ₹",
        help="Natural-language A2A task prompt",
    )
    parser.add_argument(
        "--mode",
        choices=["swarm", "live_pstn", "auto"],
        default="swarm",
        help="Execution mode ('live_pstn' dials real +91 PSTN numbers when carrier is configured)",
    )
    parser.add_argument(
        "--stripe-token",
        default=os.environ.get("STRIPE_SPT_TOKEN", "pm_card_visa"),
        help="Stripe Shared Payment Token (spt_...) or PaymentMethod (pm_card_visa) for --rail stripe",
    )
    parser.add_argument(
        "--network",
        default="eip155:84532",
        choices=["eip155:84532", "eip155:8453"],
        help="CAIP-2 network for EIP-3009 signature when --rail x402",
    )
    parser.add_argument(
        "--passcode",
        default=os.environ.get("RINGSCOUT_PASSCODE", ""),
        help="Optional owner passcode when testing sandbox/testnet handshakes",
    )
    args = parser.parse_args()

    base = args.url.rstrip("/")
    with httpx.Client(timeout=110.0, follow_redirects=True) as client:
        card = client.get(f"{base}/.well-known/agent-card.json").json()
        a2a_url = card["url"]
        print(
            f"[1] Discovered A2A Card: {card['name']} -> {a2a_url} "
            f"(gate_mode={card['payment'].get('gate_mode')})"
        )

        rpc_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": f"msg_{int(time.time())}",
                    "role": "user",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "place_category": args.category,
                                "item_question": args.message,
                                "city": "Mumbai" if "mumbai" in args.message.lower() else "Bengaluru",
                                "locality": "Bandra West" if "bandra" in args.message.lower() else "Indiranagar",
                                "execution_mode": args.mode,
                                "max_shops": 2 if args.mode == "live_pstn" else 3,
                            },
                        }
                    ],
                },
                "metadata": {
                    "execution_mode": args.mode,
                    "ap2Mandate": {
                        "mandate_id": f"mnd_{int(time.time())}",
                        "principal_id": "concierge_user",
                        "agent_id": "cli_a2a_buyer_agent",
                        "max_amount_usd": 2.00,
                        "currency": "USD",
                        "purpose": "google_maps_voice_scout",
                    },
                },
            },
        }

        headers: dict[str, str] = {}
        if args.passcode:
            headers["X-RingScout-Passcode"] = args.passcode

        if args.rail == "open":
            r = client.post(a2a_url, json=rpc_req, headers=headers)
            if r.status_code == 200:
                body = r.json()
                task = body["result"]["task"]
                print(f"[2] Phase A Open A2A Completed: {task['taskId']} -> {task['status']['state']}")
                for part in task["history"][-1]["parts"]:
                    if part.get("kind") == "text":
                        print("\n" + part["text"])
                return 0
            print(f"[!] Server returned HTTP {r.status_code} (Phase B gate active); switch to --rail stripe or --rail x402.")
            return 1

        # Probe 402 challenge from GET /api/v1/scout (works regardless of Phase A/B gate)
        probe = client.get(f"{base}/api/v1/scout")
        challenge = json.loads(base64.b64decode(probe.headers["PAYMENT-REQUIRED"]).decode("utf-8"))

        if args.rail == "stripe":
            mpp = challenge["mpp"]
            print(f"[2] Received Stripe MPP Challenge id={mpp['id']} ({mpp['amount']} {mpp['currency']})")
            cred = {
                "challenge": {
                    "id": mpp["id"],
                    "realm": mpp["realm"],
                    "method": mpp["method"],
                    "intent": mpp["intent"],
                    "request": mpp["request"],
                    "expires": mpp["expires"],
                },
                "source": "did:stripe:cli_buyer_agent",
                "payload": {
                    "type": "payment_method" if args.stripe_token.startswith("pm_") else "spt",
                    "token": args.stripe_token,
                },
            }
            headers["Authorization"] = "Payment " + _b64url_encode(
                json.dumps(cred, separators=(",", ":")).encode("utf-8")
            )
            r200 = client.post(a2a_url, json=rpc_req, headers=headers)
            print(
                f"[3] Stripe MPP Response: HTTP {r200.status_code} · "
                f"Payment-Receipt={r200.headers.get('Payment-Receipt', '')[:44]}..."
            )
        else:
            buyer_priv = os.environ.get("BUYER_EVM_PRIVATE_KEY", "").strip()
            if not buyer_priv:
                buyer_priv, buyer_addr = generate_evm_keypair()
                print(f"[Buyer Agent] Generated ephemeral EVM wallet: {buyer_addr}")
            else:
                buyer_addr = private_key_hex_to_address(buyer_priv)
                print(f"[Buyer Agent] Loaded EVM wallet: {buyer_addr}")

            accepted = next(
                (opt for opt in challenge["accepts"] if opt["network"] == args.network),
                challenge["accepts"][0],
            )
            signed_payload = sign_x402_exact_evm_payload(
                buyer_private_key_hex=buyer_priv,
                accepted_requirement=accepted,
            )
            headers["PAYMENT-SIGNATURE"] = base64.b64encode(
                json.dumps(signed_payload, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            r200 = client.post(a2a_url, json=rpc_req, headers=headers)
            print(f"[3] Paid x402 A2A Response: HTTP {r200.status_code}")

        body = r200.json()
        if r200.status_code != 200 or "error" in body:
            print(json.dumps(body, indent=2))
            return 1

        task = body["result"]["task"]
        print(f"\n✅ A2A Task {task['taskId']} -> {task['status']['state']}")
        for part in task["history"][-1]["parts"]:
            if part.get("kind") == "text":
                print("\n" + part["text"])
        return 0


if __name__ == "__main__":
    sys.exit(main())
