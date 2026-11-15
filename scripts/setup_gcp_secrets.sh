#!/usr/bin/env bash
# Provision & update RingScout telephony and admin secrets inside Google Cloud Secret Manager.
# Reads credentials strictly via stdin from ~/.config/ringscout/pstn_carrier.json (0600) —
# NEVER passes secret values on command-line arguments or writes them into the repository.
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-manav-chub}"
CARRIER_JSON="${HOME}/.config/ringscout/pstn_carrier.json"

echo "==> Verifying Secret Manager API in project '${PROJECT_ID}'..."
gcloud services enable secretmanager.googleapis.com --project="${PROJECT_ID}" --quiet

upsert_secret() {
  local secret_name="$1"
  local secret_val="$2"
  if [[ -z "${secret_val}" ]]; then
    return 0
  fi
  if ! gcloud secrets describe "${secret_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${secret_name}" \
      --replication-policy="automatic" \
      --project="${PROJECT_ID}" \
      --quiet
  fi
  printf '%s' "${secret_val}" | gcloud secrets versions add "${secret_name}" \
    --data-file=- \
    --project="${PROJECT_ID}" \
    --quiet >/dev/null
  echo "  ✓ Synced Secret Manager secret: ${secret_name}"
}

if [[ -f "${CARRIER_JSON}" ]]; then
  V_ID="$(python3 -c 'import json, pathlib; print(json.loads(pathlib.Path.home().joinpath(".config/ringscout/pstn_carrier.json").read_text()).get("auth_id",""))')"
  V_TOK="$(python3 -c 'import json, pathlib; print(json.loads(pathlib.Path.home().joinpath(".config/ringscout/pstn_carrier.json").read_text()).get("auth_token",""))')"
  V_FROM="$(python3 -c 'import json, pathlib; print(json.loads(pathlib.Path.home().joinpath(".config/ringscout/pstn_carrier.json").read_text()).get("from_number",""))')"
  upsert_secret "ringscout-vobiz-auth-id" "${V_ID}"
  upsert_secret "ringscout-vobiz-auth-token" "${V_TOK}"
  upsert_secret "ringscout-vobiz-from-number" "${V_FROM}"
else
  echo "⚠️  ${CARRIER_JSON} not found; using existing Secret Manager values if already created."
fi

ADMIN_PASS="${RINGSCOUT_ADMIN_PASSCODE:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(14))')}"
HMAC_KEY="${RINGSCOUT_HMAC_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"

upsert_secret "ringscout-admin-passcode" "${ADMIN_PASS}"
upsert_secret "ringscout-hmac-secret" "${HMAC_KEY}"

if [[ -n "${STRIPE_SECRET_KEY:-}" ]]; then
  upsert_secret "ringscout-stripe-secret-key" "${STRIPE_SECRET_KEY}"
fi

# Grant Cloud Run default compute service account read access to these secrets
PROJECT_NUM="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
SA_EMAIL="${PROJECT_NUM}-compute@developer.gserviceaccount.com"

for s in ringscout-vobiz-auth-id ringscout-vobiz-auth-token ringscout-vobiz-from-number ringscout-admin-passcode ringscout-hmac-secret; do
  if gcloud secrets describe "${s}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "${s}" \
      --project="${PROJECT_ID}" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="roles/secretmanager.secretAccessor" \
      --quiet >/dev/null
  fi
done

echo "✅ All secrets stored safely in Google Cloud Secret Manager (${PROJECT_ID})."
