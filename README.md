# RingScout — Parallel AI Voice Scout for Google Maps (`Web` · `MCP` · `Google A2A` · `x402` · `Stripe MPP`)

**RingScout** finds real open businesses on Google Maps (spas, salons, hotels, restaurants, MRI & diagnostic centers, clinics, petrol/EV stations, auto shops, pharmacies — or any specific business you add by name) and calls them **in parallel** over real `+91` phone lines in natural Indian English (`en-IN`) and Hindi/Hinglish (`hi-IN`).

Every call navigates IVR menus (`Press 1/2/0`) silently, cites street-level landmarks for natural local trust, extracts structured availability + `₹` rates via an out-of-band `temperature=0.0` Flash Side-Brain, and returns a playable `24kHz` `.wav` recording.

---

## Architecture Highlights

- **Thin Live Socket + Out-of-Band Side-Brain**: Zero tools are attached to the `BidiGenerateContent` Gemini Live voice socket (eliminating mid-call tool-turn stalls). A separate `gemini-2.5-flash` Side-Brain (`temperature=0.0`) extracts `₹` quotes, slot times, and IVR DTMF digits asynchronously after each turn.
- **Consumer App (`/`)**: Minimal dark-tech interface with separate **Spa** vs. **Salon** intent accuracy, 10-city / 85-neighbourhood PIN-code grounding, and inline **`+ Add Specific Place`** lookup.
- **Live Calls & Agentic Dashboard (`/dashboard`)**: Real-time observability across Consumer Web calls, **MCP (`POST /mcp`)**, **Google A2A v1.0 (`POST /a2a`)**, **ChatGPT Actions (`/openapi-actions.json`)**, and AI search crawlers (`/llms.txt`).
- **Multi-Platform MCP & Agent Manifests**: Includes `server.json` (Official MCP Registry), `smithery.yaml` (Smithery), `glama.json` (Glama), `/.well-known/mcp.json`, `/.well-known/agent-card.json` (Google A2A v1.0), and optional Phase B `x402` USDC + Stripe Machine Payments Protocol (`MPP`).
- **Zero-Leak Security via Google Cloud Secret Manager**: All telephony (`Vobiz` / `Plivo` / `Twilio`), HMAC, and admin credentials are stored exclusively in **Google Cloud Secret Manager** (`scripts/setup_gcp_secrets.sh`) and mounted at runtime via Cloud Run `--set-secrets`.

---

## Quick Start (Local Development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run unit & security test suite
pytest -q

# Start server on port 8088
uvicorn ringscout.server:app --app-dir src --host 0.0.0.0 --port 8088
```

---

## Deploy to Google Cloud Run (with Secret Manager)

```bash
chmod +x scripts/setup_gcp_secrets.sh deploy_cloud_run.sh
GCP_PROJECT_ID=your-gcp-project ./deploy_cloud_run.sh
```

See [`docs/MCP_PLATFORM_SUBMISSION_GUIDE.md`](docs/MCP_PLATFORM_SUBMISSION_GUIDE.md) for 1-click instructions to list RingScout on the Official MCP Registry, Smithery, Glama, PulseMCP, Claude, ChatGPT, and Gemini.
