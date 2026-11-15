# `RingScout` — India Parallel Voice Scout (`H2A` + `A2A` over `MCP`, `x402`, `AP2`, `MPP`)

## 1. Repository Scope & Architecture
- **Market Scope:** India hyperlocal offline discovery (`+91` / STD shop listings, `₹` INR quotes, natural Hinglish & Indian English conversations). Default discovery batch is `5` open medical stores / diagnostic centers / local shops.
- **Dual-Door Architecture (One Core Engine, Two Front Doors):**
  1. **Door 1 — Human-to-Agent (`H2A` Mission Control & Self-Test):**
     - **Live India `+91` PSTN (`LiveIndiaPstnAdapter`):** Real outbound calls via `Vobiz.ai` (`₹0.45/min`, `24kHz/16kHz L16` HD audio), `Plivo`, or `Twilio`, plus a 1-click `'Test Call My Own +91 Number First'` safety gate.
     - **Browser Mic Duplex (`BrowserMicAdapter`)** & **Parallel Hinglish Swarm (`HinglishSwarmAdapter`)**.
  2. **Door 2 — Agent-to-Agent (`A2A` Gateway — `a2a_gateway.py`):**
     - **Discovery Card:** `GET /.well-known/agent.json`
     - **Streamable HTTP MCP Server:** `POST /mcp` (`initialize`, `tools/list`, `tools/call`)
     - **HTTP `402` Paid Voice Oracle:** `POST /api/v1/scout` issuing dual `x402` v2 (`USDC` on Base `eip155:8453`) + `MPP` (`WWW-Authenticate: Payment`) challenges with optional `AP2` spend-mandate verification. Zero Vertex/PSTN calls execute without either valid `RINGSCOUT_PASSCODE` or facilitator-verified `x402` payment.

## 2. GCP Project & Auth Isolation
- **Always target GCP Project:** `manav-chub` (`1004579867040`), region `us-central1` (with regional Live failover across `us-east1` and `europe-west4`).
- **Never** fall back to ambient `gcloud` default project (`manavagarwal-sobi-demo`) and never hardcode API keys or access tokens in source files.

## 3. Voice Realism & AI Architecture Invariants
1. **Thin Live Socket + Out-of-Band Side-Brain:**
   - **Zero tools** (`tools=None`) may ever be attached directly to the `BidiGenerateContent` Gemini Live WebSocket session (`voice_engine.py`).
   - Primary Live voice model is `gemini-3.8-live` (`us-central1`) with automatic `<200ms` failover across `LIVE_FALLBACK_TARGETS`.
   - All structured extraction (`price_inr`, `in_stock`, `eta_or_slot`, `dtmf_digit`, `is_voicemail`, `should_hangup`) runs out-of-band via `gemini-2.5-flash` at `temperature=0.0`.
2. **Humanized 2-Beat Call Flow (`compile_conversational_call_plan`):**
   - Never read raw UI search prompts verbatim to shopkeepers. Compile raw goals into casual spoken Hinglish / Indian English with **2-beat micro-turns** (Turn 1: check availability only; Turn 2: ask `₹` price after confirmation; Turn 3: warm thank-you), `1.3s` human `"Hello?"` pickup wait on live PSTN, and subtle `-42 dBFS` telephone comfort noise.
3. **Accent Drift Guard (`vocal_tone`):**
   - Every Side-Brain response schema and prompt must explicitly carry `vocal_tone` (`polite_indian_english` | `conversational_hinglish`) to prevent mid-call US accent drift.
4. **Deterministic Phone Hygiene & Safety Guardrails:**
   - Normalize Indian `+91` numbers deterministically in Python (`google_lookup.py`), strip `1800` / `1860` toll-free call centers, enforce `<5s` voicemail bailout, `90s` hard call cap, **Stop All** panic endpoint, `RINGSCOUT_PASSCODE` user guard, and `RINGSCOUT_HMAC_SECRET` carrier webhook signatures.

## 4. Commands
- **Install:** `.venv/bin/pip install -e .`
- **Test:** `.venv/bin/pytest -v`
- **Run Local Server:** `.venv/bin/uvicorn ringscout.server:app --host 0.0.0.0 --port 8080`
- **Deploy Public `wss://` to Cloud Run (`manav-chub`):** `./deploy_cloud_run.sh`
