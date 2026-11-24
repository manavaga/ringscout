#!/usr/bin/env bash
# 1-Command Guided Publisher: GitHub (manavaga/ringscout) + Official MCP Registry (registry.modelcontextprotocol.io)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

GH_BIN="${HOME}/.local/bin/gh"
MCP_PUB="${HOME}/.local/bin/mcp-publisher"
GH_USER="manavaga"
REPO_SLUG="${GH_USER}/ringscout"

echo "=================================================================="
echo "  RingScout — GitHub & Official MCP Registry Publisher"
echo "  Target Namespace : io.github.${GH_USER}/ringscout"
echo "  Live MCP URL     : https://ringscout-hom7ntreuq-uc.a.run.app/mcp"
echo "=================================================================="
echo ""

# 1. Authenticate GitHub CLI (device code flow safe over SSH)
if ! "${GH_BIN}" auth status --hostname github.com >/dev/null 2>&1; then
  echo "==> Step 1/4: Sign in to GitHub as '${GH_USER}'"
  echo "    (Copy the 8-character one-time code shown below and open https://github.com/login/device on your Mac browser)"
  echo ""
  BROWSER=echo "${GH_BIN}" auth login --hostname github.com --git-protocol https --web
fi

ACTIVE_GH_USER="$("${GH_BIN}" api user --jq '.login')"
echo "  ✓ Authenticated to GitHub as: ${ACTIVE_GH_USER}"
if [[ "${ACTIVE_GH_USER,,}" != "${GH_USER,,}" ]]; then
  echo "  ⚠️  Note: Logged in as '${ACTIVE_GH_USER}' (expected '${GH_USER}')."
  echo "     Updating server.json & glama.json namespace to '${ACTIVE_GH_USER}'..."
  GH_USER="${ACTIVE_GH_USER}"
  REPO_SLUG="${GH_USER}/ringscout"
  sed -i "s|manavaga|${GH_USER}|g" server.json glama.json protocols/mcp.json
  git add server.json glama.json protocols/mcp.json
  git commit -m "chore(mcp): align GitHub namespace to ${GH_USER}" || true
fi

"${GH_BIN}" auth setup-git --hostname github.com

# 2. Create public GitHub repository (if needed) & push main
echo ""
echo "==> Step 2/4: Pushing public repository https://github.com/${REPO_SLUG} ..."
git remote set-url origin "https://github.com/${REPO_SLUG}.git" 2>/dev/null || git remote add origin "https://github.com/${REPO_SLUG}.git"

if ! "${GH_BIN}" repo view "${REPO_SLUG}" >/dev/null 2>&1; then
  "${GH_BIN}" repo create "${REPO_SLUG}" \
    --public \
    --source=. \
    --remote=origin \
    --homepage="https://ringscout-hom7ntreuq-uc.a.run.app" \
    --description="Parallel AI voice scout that calls real open Google Maps businesses (+91) to verify live availability, wait times, and INR rates with 24kHz WAV proof." \
    --push
else
  git push -u origin main
fi
echo "  ✓ Code pushed to https://github.com/${REPO_SLUG}"

# 3. Validate & Publish to Official MCP Registry
echo ""
echo "==> Step 3/4: Validating server.json against Official MCP Registry..."
"${MCP_PUB}" validate server.json

echo ""
echo "==> Step 4/4: Authenticating & Publishing to registry.modelcontextprotocol.io..."
echo "    (If prompted with a GitHub device code, open https://github.com/login/device on your Mac browser)"
echo ""
BROWSER=echo "${MCP_PUB}" login github
"${MCP_PUB}" publish

echo ""
echo "=================================================================="
echo "  🎉 PUBLISHED! RingScout is now live on the Official MCP Registry!"
echo "=================================================================="
echo "  • Official Registry API : https://registry.modelcontextprotocol.io/v0/servers?search=ringscout"
echo "  • GitHub Repository     : https://github.com/${REPO_SLUG}"
echo "  • Remote MCP Endpoint   : https://ringscout-hom7ntreuq-uc.a.run.app/mcp"
echo ""
echo "  Next 2-Minute Directory Submissions (Glama & PulseMCP auto-sync from above):"
echo "  1. Smithery.ai : https://smithery.ai/new  -> select ${REPO_SLUG}"
echo "  2. Glama.ai    : https://glama.ai/mcp/servers -> claim https://github.com/${REPO_SLUG}"
echo "  3. PulseMCP    : https://www.pulsemcp.com/submit -> paste https://github.com/${REPO_SLUG}"
echo "  4. MCP.so      : https://mcp.so/submit -> paste https://ringscout-hom7ntreuq-uc.a.run.app/mcp"
echo "=================================================================="
