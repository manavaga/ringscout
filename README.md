# RingScout — Parallel AI Voice Scout for Google Maps (`MCP` · `Google A2A` · `OpenAPI` · `x402` · `Stripe MPP`)

[![Official MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.manavaga%2Fringscout-10b981?style=flat-square)](https://registry.modelcontextprotocol.io/v0/servers?search=ringscout)
[![manavaga/ringscout MCP server](https://glama.ai/mcp/servers/manavaga/ringscout/badges/score.svg)](https://glama.ai/mcp/servers/manavaga/ringscout)
[![MCP Protocol](https://img.shields.io/badge/MCP-2025--06--18_Streamable_HTTP_%2B_SSE-6366f1?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/mcp)
[![Google A2A](https://img.shields.io/badge/Google_A2A-v1.0_Agent_Card-38bdf8?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/.well-known/agent-card.json)
[![SEP-2127](https://img.shields.io/badge/SEP--2127-Server_Card_+_AI_Catalog-f59e0b?style=flat-square)](https://ringscout-hom7ntreuq-uc.a.run.app/.well-known/mcp/server-card.json)
[![License: MIT](https://img.shields.io/badge/License:MIT-slate?style=flat-square)](LICENSE)

**RingScout** gives AI agents and human users a real-world voice presence: it finds businesses on Google Maps and can call public business phone numbers in India to ask about live availability, wait times, appointments, services and quoted prices. Supported voice interactions include Indian English and Hindi/Hinglish. Results may include call status, reported answers and playable WAV audio proof where available.

- **Live Consumer Web App**: https://ringscout-hom7ntreuq-uc.a.run.app/
- **Live Agentic & MCP Telemetry**: https://ringscout-hom7ntreuq-uc.a.run.app/dashboard
- **Remote MCP Endpoint (Zero Install)**: `https://ringscout-hom7ntreuq-uc.a.run.app/mcp`
- **Developer tier and usage details**: See the live pricing endpoint and current service configuration.

### 🎬 Watch Real Live +91 Restaurant Calls
[![Watch RingScout Live PSTN Demo Video](https://github.com/manavaga/ringscout/releases/download/v3.0.0/agentic_marketplace_poster.jpg)](https://github.com/manavaga/ringscout/releases/download/v3.0.0/ringscout_live_calls_demo.mp4)

> **[▶ Watch the live PSTN call demo](https://github.com/manavaga/ringscout/releases/download/v3.0.0/ringscout_live_calls_demo.mp4)** · **[🔊 Listen to the sample call audio](https://github.com/manavaga/ringscout/releases/download/v3.0.0/live_pstn_a2a_cincin.wav)**

---

## Connect in 10 Seconds (Zero Local Install Required)

### Claude (Web & Desktop)
*Settings → Connectors → Add Custom Connector* → paste:
```text
https://ringscout-hom7ntreuq-uc.a.run.app/mcp
```

### Cursor / Windsurf / VS Code / Gemini CLI / Claude Desktop
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

---

## Support, Privacy & Legal

- [Support and troubleshooting](SUPPORT.md)
- [Privacy Notice](PRIVACY.md)
- [Terms of Use](TERMS.md)
- [Frequently Asked Questions](FAQ.md)
- [Security vulnerability reporting](SECURITY.md)

**Important:** RingScout reports information communicated during a call. Businesses may not answer, and availability, prices and terms can change. Confirm important details directly with the business. RingScout does not itself complete a booking or purchase unless a distinct workflow expressly states otherwise.

The privacy and terms documents are drafts pending operator verification of contact details, data retention, production processors, pricing and applicable legal terms. They should not be treated as final until those items are confirmed.

---

## Local Development & Verification

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
