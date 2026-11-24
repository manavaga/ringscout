# Publishing & Discovering RingScout Across All MCP Directories & AI Platforms

RingScout exposes a multi-layered remote MCP + Agentic Discovery stack on Cloud Run (`https://ringscout-hom7ntreuq-uc.a.run.app`) so external AI agents, IDEs, consumer chat connectors, and automated directory crawlers can discover, inspect, ping, and invoke it without manual setup.

---

## 1. The 4 Discovery Layers (How Agents & Platforms Find Your MCP Endpoint)

| Layer | Mechanism | RingScout Live Endpoints | Who Uses It |
| :--- | :--- | :--- | :--- |
| **Layer 1: Upstream & Community MCP Registries** | Canonical `server.json` (`2025-12-11` schema) + repo manifests (`smithery.yaml`, `glama.json`) + `/ping` health probe | `/server.json`, `/ping`, `/mcp/health` | **Official MCP Registry** (`registry.modelcontextprotocol.io`), **Smithery**, **Glama**, **PulseMCP**, **MCP.so**, VS Code / Cursor / GitHub Copilot MCP extensions |
| **Layer 2: RFC 8615 `.well-known` Domain Auto-Discovery** | `SEP-2127` MCP Server Cards (`application/mcp-server-card+json`), Domain AI Catalog, Google A2A v1.0 Agent Card, OpenAPI 3.1, `x402` | `/.well-known/ai-catalog.json`<br>`/.well-known/mcp/server-card.json`<br>`/.well-known/mcp.json`<br>`/.well-known/agent-card.json`<br>`/openapi-actions.json` | Autonomous agent routers, Vertex AI ADK / A2A clients, browser MCP inspectors, ChatGPT Actions importer, x402 Bazaar scanners |
| **Layer 3: AI Search / GEO Grounding** | Crawler allowlist + structured Markdown tool guide + XML sitemap | `/llms.txt`, `/llms-full.txt`, `/robots.txt`, `/sitemap.xml` | `OAI-SearchBot`, `ChatGPT-User`, `Claude-SearchBot`, `PerplexityBot`, `Google-Extended` when users ask *"Which MCP tool can call restaurants/hotels in India?"* |
| **Layer 4: Runtime MCP Handshake & "Agentic SEO"** | `initialize` `instructions` + `tools/list` `annotations` (`readOnlyHint`, `openWorldHint`) + `prompts/list` + `resources/list` + Legacy `/sse` | `POST /mcp` (Streamable HTTP `2025-06-18`)<br>`GET /sse` + `POST /messages` (Legacy `2024-11-05`) | Host LLM tool-routers inside **Claude**, **ChatGPT Developer Mode**, **Gemini CLI / Jetski**, **Cursor**, **Windsurf**, and **LangChain / LlamaIndex** |

---

## 2. Pre-Publish Verification Command (Run Anytime)

```bash
# 1. Validate server.json against the live Official MCP Registry API
~/.local/bin/mcp-publisher validate server.json

# 2. Run the 18-check External Agent & Directory Scanner Simulator
python3 scripts/verify_mcp_discovery.py https://ringscout-hom7ntreuq-uc.a.run.app
```

---

## 3. Step-by-Step Multi-Directory Submission Commands

### A. Push Public Repository to GitHub (Prerequisite for GitHub-Verified Directories)
Smithery, Glama, Awesome-MCP-Servers, and `io.github.<username>/*` namespaces verify against a public GitHub repo (zero secrets are stored in git — verified by `test_spa_vs_salon_specific_place_dashboard_and_zero_repo_secrets`):
```bash
git remote add origin https://github.com/manavaga/ringscout.git
git push -u origin main
```

### B. Official MCP Registry (`registry.modelcontextprotocol.io`)
PulseMCP, Glama, VS Code, and Cursor sync directly from the Official MCP Registry upstream:
```bash
# Option 1: GitHub Namespace (io.github.manavaga/ringscout)
~/.local/bin/mcp-publisher login github
~/.local/bin/mcp-publisher publish

# Option 2: Direct Cloud Run / Custom Domain HTTP Proof (/.well-known/mcp-registry-auth)
# Generate Ed25519 keypair, set MCP_REGISTRY_PUBLIC_KEY on Cloud Run, then:
~/.local/bin/mcp-publisher login http --domain ringscout-hom7ntreuq-uc.a.run.app --private-key "$PRIVATE_KEY_HEX"
```

### C. Major MCP Marketplaces & Directories
1. **Smithery.ai (`smithery.ai/new`)**: Connect `github.com/manavaga/ringscout` or submit remote Streamable HTTP URL `https://ringscout-hom7ntreuq-uc.a.run.app/mcp` (`smithery.yaml` is pre-configured in root).
2. **Glama.ai (`glama.ai/mcp/servers`)**: Auto-syncs from Official MCP Registry, or submit repo URL directly (`glama.json` claims maintainer ownership; `/ping` + tool `annotations` + `resources` + `prompts` earn top quality score).
3. **PulseMCP (`pulsemcp.com/submit`) & MCP.so (`mcp.so/submit`)**: Submit `https://ringscout-hom7ntreuq-uc.a.run.app/mcp` and GitHub repo URL.
4. **Awesome-MCP-Servers (`github.com/punkpeye/awesome-mcp-servers`)**: Open a 1-line PR adding `RingScout` under *Location & Travel / Voice & Telephony*.

### D. Direct 1-Click Connection in Major AI Assistants
- **Claude (`claude.ai` / Claude Desktop)**: *Settings → Connectors → Add Custom Connector* → `https://ringscout-hom7ntreuq-uc.a.run.app/mcp`
- **ChatGPT (Developer Mode MCP)**: *Settings → Connectors → Create* → `https://ringscout-hom7ntreuq-uc.a.run.app/mcp` (or *Custom GPT → Actions → Import from URL* → `https://ringscout-hom7ntreuq-uc.a.run.app/openapi-actions.json`)
- **Gemini CLI / Jetski / Cursor**: Add `"ringscout": {"url": "https://ringscout-hom7ntreuq-uc.a.run.app/mcp"}` to `mcpServers` (`protocols/mcp_config.json`).
