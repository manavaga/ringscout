# RingScout

**Real-time local voice checks**

[![Official MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.manavaga%2Fringscout-10b981?style=flat-square)](https://registry.modelcontextprotocol.io/v0/servers?search=ringscout)
[![manavaga/ringscout MCP server](https://glama.ai/mcp/servers/manavaga/ringscout/badges/score.svg)](https://glama.ai/mcp/servers/manavaga/ringscout)
[![Remote MCP](https://img.shields.io/badge/Remote_MCP-Streamable_HTTP-6366f1?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/mcp)
[![Google A2A](https://img.shields.io/badge/Google_A2A-v1.0-38bdf8?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/.well-known/agent-card.json)
[![License: MIT](https://img.shields.io/badge/License-MIT-slate?style=flat-square)](LICENSE)

RingScout finds local businesses on Google Maps and calls them directly to verify what's happening right now.

Instead of relying only on potentially outdated listings, RingScout uses AI-powered voice calls to contact real business phone numbers in India, ask your questions in Indian English, Hindi, or Hinglish, and return the answers.

### Find out:
- Whether a **restaurant** has a table available and how long the wait is
- Whether a **hotel** has rooms, early check-in, or a better direct tariff
- Whether a **salon or spa** has a same-day appointment
- Whether a **clinic or diagnostic centre** has an available slot and what it charges
- Whether a **petrol pump** has a particular fuel or service available
- Whether a **pharmacy or auto shop** has the item or service you need

RingScout can contact up to **five businesses in parallel** and return verified responses, prices in **₹**, key details, and recorded call audio when available.

- **Coverage:** India, with supported local business categories and locations.
- **Important limitations:** Results depend on businesses being reachable and answering. Availability and prices can change after a call. RingScout provides information; it does not itself make reservations, purchases, or appointments.

---

### Watch Real Live `+91` Calls (`57s` Demo — Audio ON)
[![Watch RingScout Live PSTN Demo Video](https://github.com/manavaga/ringscout/releases/download/v3.0.0/agentic_marketplace_poster.jpg)](https://github.com/manavaga/ringscout/releases/download/v3.0.0/ringscout_live_calls_demo.mp4)
> **[▶ Play 1080p MP4 demo (real Hakkasan & CinCin calls)](https://github.com/manavaga/ringscout/releases/download/v3.0.0/ringscout_live_calls_demo.mp4)** · **[🔊 Listen to uncut 57s live A2A call + IVR keypad navigation (`.wav`)](https://github.com/manavaga/ringscout/releases/download/v3.0.0/live_pstn_a2a_cincin.wav)**

---

## Try It or Connect Your AI Assistant

- **Web App (No install):** https://ringscout-hom7ntreuq-uc.a.run.app/
- **Live Call & Agent Dashboard:** https://ringscout-hom7ntreuq-uc.a.run.app/dashboard
- **Remote MCP Endpoint:** `https://ringscout-hom7ntreuq-uc.a.run.app/mcp`
- **Free Developer Tier:** Your first live call works out of the box; ⭐ **star this repository** and pass your `github_username` to unlock **5 free live `+91` calls/day**.

### ChatGPT (`Plugins → + → Add custom MCP server`)
- **Name:** `RingScout`
- **Server URL:** `https://ringscout-hom7ntreuq-uc.a.run.app/mcp`
- **Authentication:** `No authentication`

### Claude (`Settings → Connectors → Add custom connector`)
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

### Google A2A v1.0 (`POST /a2a`)
```bash
curl -X POST https://ringscout-hom7ntreuq-uc.a.run.app/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "m1",
        "role": "user",
        "parts": [{"kind": "text", "text": "Call CinCin in BKC Mumbai and ask if a table for 4 is open tonight at 8:30 PM"}]
      }
    }
  }'
```

---

## Machine-Readable Discovery Endpoints

| Interface | Standard | Endpoint |
| :--- | :--- | :--- |
| **Remote MCP** | Streamable HTTP (`2025-11-25` / `2025-06-18`) + SSE (`2024-11-05`) | `POST /mcp` · `GET /sse` |
| **MCP Discovery** | `SEP-2127` Server Card + Official Registry `2025-12-11` | `/.well-known/mcp/server-card.json` · `/server.json` |
| **Agent-to-Agent** | Google / Linux Foundation **A2A v1.0** | `/.well-known/agent-card.json` · `POST /a2a` |
| **Domain AI Catalog** | `SEP-2127` AI Catalog + `agents.json` + OpenAPI `3.1` | `/.well-known/ai-catalog.json` · `/openapi-actions.json` |
| **AI Search & Trust** | `llms.txt` + Privacy & Call Disclosure Policy | `/llms.txt` · `/privacy` · `/ping` |

---

## Architecture & Local Development

- **Thin Live Socket + Out-of-Band Side-Brain:** Duplex voice runs over Gemini Live (`en-IN` / `hi-IN`) with zero inline tools attached to the audio stream; an out-of-band `gemini-2.5-flash` Side-Brain extracts `₹` prices, slot details, and IVR keypad digits asynchronously.
- **Public Business Provenance:** Only verified public business listings discovered on Google Maps may be dialed (`1800` toll-free lines and unlisted personal numbers are blocked).
- **Secret Hygiene:** Carrier (`Vobiz` / `Plivo` / `Twilio`) and admin credentials are stored exclusively in Google Cloud Secret Manager (`scripts/setup_gcp_secrets.sh`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python3 scripts/verify_mcp_discovery.py https://ringscout-hom7ntreuq-uc.a.run.app
```

## License

MIT — see [LICENSE](LICENSE).

---

## Support, Privacy & Legal

- [Support and troubleshooting](SUPPORT.md)
- [Privacy Notice](PRIVACY.md)
- [Terms of Use](TERMS.md)
- [Frequently Asked Questions](FAQ.md)
- [Security vulnerability reporting](SECURITY.md)

**Important:** RingScout reports information communicated during a call. Businesses may not answer, and availability, prices and terms can change. Confirm important details directly with the business. RingScout does not itself complete a booking or purchase unless a distinct workflow expressly states otherwise.

The Privacy Notice and Terms identify Manav Agarwal and agarwalmanav91@gmail.com, and specify Indian law. They remain drafts: the source audit found that AI/recording disclosures are not proactive, public dashboard responses expose transcripts and signed audio links, and actual Cloud Logging retention has not been verified. These issues must be resolved before public launch.
