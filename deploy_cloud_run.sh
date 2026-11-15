#!/usr/bin/env bash
# Deploy RingScout v3.0 (Public Consumer URL + Live Dashboard + MCP/A2A + Secret Manager) to Cloud Run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROJECT_ID="${GCP_PROJECT_ID:-manav-chub}"
REGION="${GCP_LOCATION:-us-central1}"
SERVICE_NAME="${RINGSCOUT_SERVICE_NAME:-ringscout}"
export CLOUDSDK_CORE_ACCOUNT="${GCLOUD_ACCOUNT:-admin@manavagarwal.altostrat.com}"

echo "==> Checking gcloud access to project '${PROJECT_ID}' (${REGION}) as '${CLOUDSDK_CORE_ACCOUNT}'..."
if ! gcloud run services list --project="${PROJECT_ID}" --region="${REGION}" --quiet >/dev/null 2>&1; then
  echo ""
  echo "❌ Active gcloud token for '${CLOUDSDK_CORE_ACCOUNT}' expired."
  echo "Run:"
  echo "  gcloud auth login ${CLOUDSDK_CORE_ACCOUNT} --no-launch-browser && ./deploy_cloud_run.sh"
  echo ""
  exit 1
fi

echo "==> Syncing telephony & admin secrets into Google Cloud Secret Manager..."
GCP_PROJECT_ID="${PROJECT_ID}" ./scripts/setup_gcp_secrets.sh

SECRETS_MOUNT="VOBIZ_AUTH_ID=ringscout-vobiz-auth-id:latest,VOBIZ_AUTH_TOKEN=ringscout-vobiz-auth-token:latest,VOBIZ_FROM_NUMBER=ringscout-vobiz-from-number:latest,RINGSCOUT_ADMIN_PASSCODE=ringscout-admin-passcode:latest,RINGSCOUT_HMAC_SECRET=ringscout-hmac-secret:latest"

ENV_VARS="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${REGION},RINGSCOUT_USE_SECRET_MANAGER=1,RINGSCOUT_PUBLIC_CONSUMER_ACCESS=1"
if [[ -n "${X402_PAY_TO_ADDRESS:-}" ]]; then
  ENV_VARS="${ENV_VARS},X402_PAY_TO_ADDRESS=${X402_PAY_TO_ADDRESS}"
fi

echo "==> Deploying '${SERVICE_NAME}' to Cloud Run (${PROJECT_ID} / ${REGION})..."
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --no-invoker-iam-check \
  --max-instances 1 \
  --concurrency 80 \
  --timeout 300 \
  --cpu 2 \
  --memory 1Gi \
  --session-affinity \
  --quiet \
  --set-env-vars "${ENV_VARS}" \
  --set-secrets "${SECRETS_MOUNT}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project "${PROJECT_ID}" --region "${REGION}" --format='value(status.url)')"

echo ""
echo "✅ RingScout v3.0 is live with GCP Secret Manager!"
echo "🌐 Public Consumer URL (anyone can use immediately, zero passcode):"
echo "   ${SERVICE_URL}/"
echo "📊 Live Calls & Agentic MCP Dashboard:"
echo "   ${SERVICE_URL}/dashboard"
echo "🤖 Remote Streamable HTTP MCP Endpoint:"
echo "   ${SERVICE_URL}/mcp"
