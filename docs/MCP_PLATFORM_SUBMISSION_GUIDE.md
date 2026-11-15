# Listing RingScout Across All MCP & AI Agent Platforms

RingScout ships with every official machine-readable discovery manifest in the repository root and live HTTP endpoints so it can be listed across all major MCP directories and AI assistants.

| Ecosystem / Directory | Checked-in Manifest | Live HTTP URL | How to Publish / Claim |
| :--- | :--- | :--- | :--- |
| **1. Official MCP Registry** (`registry.modelcontextprotocol.io`) | `server.json` | `/server.json` | Install `mcp-publisher`, run `mcp-publisher login github`, then `mcp-publisher publish` from repo root. |
| **2. Smithery.ai** (`smithery.ai`) | `smithery.yaml` | `/mcp` | Open `https://smithery.ai/new`, connect your GitHub repo `ringscout` — Smithery auto-detects `smithery.yaml`. |
| **3. Glama.ai MCP Directory** (`glama.ai/mcp/servers`) | `glama.json` | `/.well-known/mcp.json` | Submit your GitHub repo URL on `https://glama.ai/mcp/servers` — `glama.json` verifies maintainer ownership automatically. |
| **4. PulseMCP & MCP.so** (`pulsemcp.com` / `mcp.so`) | `protocols/mcp.json` | `/.well-known/mcp.json` | Submit the GitHub repo URL at `https://www.pulsemcp.com/submit` and `https://mcp.so/submit`. |
| **5. Awesome-MCP-Servers** (`punkpeye/awesome-mcp-servers`) | `README.md` | `/mcp` | Open a 1-line PR under *Location & Travel / Telephony* linking your GitHub repo. |
| **6. Anthropic Claude** (`claude.ai` & Claude Desktop) | `protocols/mcp_config.json` | `/mcp` | In `claude.ai` → *Settings → Integrations → Add Custom Connector* → paste `https://<your-cloud-run-url>/mcp`. |
| **7. Google Gemini & Vertex A2A** | `protocols/agent-card.json` | `/.well-known/agent-card.json` | Point Gemini CLI (`mcpServers`) to `/mcp` or Vertex ADK A2A client to `/.well-known/agent-card.json`. |
| **8. OpenAI ChatGPT (Custom GPTs)** | `protocols/openapi-actions.json` | `/openapi-actions.json` | In GPT Editor → *Create new action → Import from URL* → paste `https://<your-cloud-run-url>/openapi-actions.json`. |

## Tracking Live Agentic Traffic
Open **`/dashboard`** (`GET /api/dashboard/summary`) to monitor in real time:
- Calls triggered via **MCP Connectors (`POST /mcp`)**, **Google A2A (`POST /a2a`)**, and **Agent REST / `x402` (`POST /api/v1/scout`)** vs. **Consumer Web (`/`)**.
- Headless crawls from `OAI-SearchBot`, `ChatGPT-User`, `Claude-SearchBot`, `Google-Extended`, and `PerplexityBot` hitting `/llms.txt` and `/.well-known/*`.
