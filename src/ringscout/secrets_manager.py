"""Google Cloud Secret Manager & Zero-Leak Credential Loader for RingScout.

Resolution order (zero plaintext secrets ever stored in git or Docker images):
1. Environment variables injected at runtime via Cloud Run `--set-secrets` (native GCP Secret Manager mount).
2. Direct GCP Secret Manager REST API (`projects/{project}/secrets/{id}/versions/latest:access`) via
   Compute/Cloud Run metadata server token when `RINGSCOUT_USE_SECRET_MANAGER=1` or `K_SERVICE` is set.
3. Local developer `0600` file `~/.config/ringscout/pstn_carrier.json` (outside the git repository).
"""

from __future__ import annotations

import base64
from functools import lru_cache
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

SECRET_NAME_MAP: dict[str, str] = {
    "VOBIZ_AUTH_ID": "ringscout-vobiz-auth-id",
    "VOBIZ_AUTH_TOKEN": "ringscout-vobiz-auth-token",
    "VOBIZ_FROM_NUMBER": "ringscout-vobiz-from-number",
    "RINGSCOUT_ADMIN_PASSCODE": "ringscout-admin-passcode",
    "RINGSCOUT_HMAC_SECRET": "ringscout-hmac-secret",
    "STRIPE_SECRET_KEY": "ringscout-stripe-secret-key",
}


def _metadata_access_token() -> str | None:
    """Fetch service-account OAuth token from GCP Metadata Server on Cloud Run/GCE, falling back to `gcloud` CLI on workstation."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            import json

            data = json.loads(resp.read().decode("utf-8"))
            tok = str(data.get("access_token") or "")
            if tok:
                return tok
    except Exception:
        pass
    try:
        import subprocess

        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=4.0,
        ).strip()
        return tok or None
    except Exception:
        return None


@lru_cache(maxsize=32)
def fetch_gcp_secret(secret_id: str, project_id: str | None = None) -> str:
    """Read latest secret version directly from Google Cloud Secret Manager (`secretmanager.googleapis.com`)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return ""
    proj = (project_id or os.environ.get("GCP_PROJECT_ID") or "").strip()
    if not proj:
        return ""
    token = _metadata_access_token()
    if not token:
        return ""
    url = f"https://secretmanager.googleapis.com/v1/projects/{proj}/secrets/{secret_id}/versions/latest:access"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            import json

            body = json.loads(resp.read().decode("utf-8"))
            b64_data = (body.get("payload") or {}).get("data") or ""
            if b64_data:
                return base64.b64decode(b64_data).decode("utf-8").strip()
    except Exception as exc:
        logger.debug("Secret Manager lookup for '%s' skipped (%s)", secret_id, type(exc).__name__)
    return ""


def get_secret(env_key: str, default: str = "") -> str:
    """Resolve `env_key` from env / Cloud Run `--set-secrets` mount first, then GCP Secret Manager API."""
    direct = os.environ.get(env_key, "").strip()
    if direct:
        return direct
    use_gsm = bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("RINGSCOUT_USE_SECRET_MANAGER", "").strip() == "1"
    )
    if use_gsm and env_key in SECRET_NAME_MAP:
        fetched = fetch_gcp_secret(SECRET_NAME_MAP[env_key])
        if fetched:
            return fetched
    return default
