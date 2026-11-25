# RingScout — Parallel AI Voice Scout for Google Maps (`MCP` · `Google A2A` · `OpenAPI` · `x402` · `Stripe MPP`)

[![Official MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.manavaga%2Fringscout-10b981?style=flat-square)](https://registry.modelcontextprotocol.io/v0/servers?search=ringscout)
[![manavaga/ringscout MCP server](https://glama.ai/mcp/servers/manavaga/ringscout/badges/score.svg)](https://glama.ai/mcp/servers/manavaga/ringscout)
[![MCP Protocol](https://img.shields.io/badge/MCP-2025--06--18_Streamable_HTTP_%2B_SSE-6366f1?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/mcp)
[![Google A2A](https://img.shields.io/badge/Google_A2A-v1.0_Agent_Card-38bdf8?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/.well-known/agent-card.json)
[![SEP-2127](https://img.shields.io/badge/SEP--2127-Server_Card_%2B_AI_Catalog-f59e0b?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/.well-known/mcp/server-card.json)
[![License: MIT](https://img.shields.io/badge/License-MIT-slate?style=flat-square)](LICENSE)

**RingScout** gives any AI agent, IDE, or human user a real-world voice presence: it finds open businesses on Google Maps (restaurants, hotels, spas, salons, MRI & diagnostic labs, clinics, petrol/EV stations, auto shops, pharmacies) and calls them **in parallel** over real `+91` phone lines in natural Indian English (`en-IN`) and Hindi/Hinglish (`hi-IN`), returning verified live availability, wait times, `₹` rates, and playable `24kHz` `.wav` audio proof.

- **Live Consumer Web App**: https://ringscout-hom7ntreuq-uc.a.run.app/
- **Live Agentic & MCP Telemetry**: https://ringscout-hom7ntreuq-uc.a.run.app/dashboard
- **Remote MCP Endpoint (Zero Install)**: `https://ringscout-hom7ntreuq-uc.a.run.app/mcp`

---

## Connect in 10 Seconds (Zero Local Install Required)

### Claude (`claude.ai` Web & Desktop)
*Settings → Connectors → Add Custom Connector* → paste:
```text
https://ringscout-hom7ntreuq-uc.a.run.app/mcp
```

### Cursor / Windsurf / VS Code / Gemini CLI / Claude Desktop (`mcpServers`)
```json
{
  "mcpServers": {
    "ringscout": {
      "url": "https://ringscout-hom7ntreuq-uc.a.run.app/mcp"
    }
  }
}
```

### ChatGPT (Developer Mode MCP or Custom GPT Actions)
- **Developer Mode MCP**: *Settings → Connectors → Create* → `https://ringscout-hom7ntreuq-uc.a.run.app/mcp`
- **Custom GPT Actions**: *Configure → Actions → Import from URL* → `https://ringscout-hom7ntreuq-uc.a.run.app/openapi-actions.json`

### Live `curl` Handshake Test
```bash
curl -s https://ringscout-hom7ntreuq-uc.a.run.app/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq .
```

---

## Complete Machine-Readable Agent Discovery Matrix

RingScout implements every 2026 agent discovery, protocol, and web-linking standard (including RFC 8288 `Link` HTTP headers and Schema.org `WebAPI` JSON-LD on `/`):

| Layer | Standard | Live Machine-Readable Endpoint |
| :--- | :--- | :--- |
| **Vertical Tooling (MCP)** | Streamable HTTP (`2025-11-25` / `2025-06-18`) | `POST https://ringscout-hom7ntreuq-uc.a.run.app/mcp` |
| **Legacy & Dual MCP** | HTTP+SSE (`2024-11-05`) + Dual POST | `GET / POST https://ringscout-hom7ntreuq-uc.a.run.app/sse` |
| **MCP Auto-Discovery** | `SEP-2127` Server Card + Official Registry `2025-12-11` | `/.well-known/mcp/server-card.json` · `/.well-known/mcp.json` · `/server.json` |
| **Horizontal Peer Agents** | Google / Linux Foundation **A2A v1.0** | `/.well-known/agent-card.json` · `POST /a2a` |
| **Domain AI Catalog** | `SEP-2127` / `ai-catalog` (`specVersion: 1.0`) | `/.well-known/ai-catalog.json` · `/.well-known/ai-catalog` |
| **Multi-Agent Workflows** | `agents.json` + OpenAPI 3.1 Actions | `/.well-known/agents.json` · `/openapi-actions.json` · `/.well-known/ai-plugin.json` |
| **AI Search / GEO** | LLM Markdown Index + Bot Allowlist | `/llms.txt` · `/llms-full.txt` · `/robots.txt` · `/sitemap.xml` |
| **Agentic Payments** | `x402` v2 Base USDC + Stripe `MPP` + Google `AP2` | `/.well-known/x402` · `/api/v1/pricing` · `POST /api/v1/scout` |
| **Health & Trust** | Directory Uptime Probe + RFC 9116 Security | `/ping` · `/mcp/health` · `/.well-known/security.txt` |

---

## Architecture Highlights

- **Thin Live Socket + Out-of-Band Side-Brain**: Zero tools are attached to the `BidiGenerateContent` Gemini Live voice socket (eliminating mid-call tool-turn stalls). A separate `gemini-2.5-flash` Side-Brain (`temperature=0.0`) extracts `₹` quotes, slot times, and IVR DTMF digits asynchronously after each turn.
- **Zero-Leak Security via Google Cloud Secret Manager**: All telephony (`Vobiz` / `Plivo` / `Twilio`), HMAC, and admin credentials live exclusively in Google Cloud Secret Manager (`scripts/setup_gcp_secrets.sh`).

---

## Local Development & Verification

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

# Run the External Agent & Directory Scanner Audit against any host
python3 scripts/verify_mcp_discovery.py https://ringscout-hom7ntreuq-uc.a.run.app
```

## License

MIT — see [LICENSE](LICENSE).
