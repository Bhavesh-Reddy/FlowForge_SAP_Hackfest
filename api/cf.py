"""Cloud Foundry: map a user-provided service (VCAP_SERVICES) onto the plan §7 environment variables.

The service is created by a human (`cf create-user-provided-service ff-hana -p '{...}'`, see docs/deploy.md).
Values already present in the environment win, so `cf set-env` can override a single key.
Nothing here logs or returns secret values.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, MutableMapping

log = logging.getLogger(__name__)

SERVICE_NAME = "ff-hana"
# credential key in the service (several spellings accepted) -> env var from plan §7
KEYS: dict[str, tuple[str, ...]] = {
    "HANA_HOST": ("host", "hana_host", "HANA_HOST"),
    "HANA_PORT": ("port", "hana_port", "HANA_PORT"),
    "HANA_USER": ("user", "hana_user", "HANA_USER"),
    "HANA_PASSWORD": ("password", "hana_password", "HANA_PASSWORD"),
    "HANA_SCHEMA": ("schema", "hana_schema", "HANA_SCHEMA"),
    "APP_API_KEY": ("app_api_key", "APP_API_KEY"),
    "SAP_API_HUB_KEY": ("sap_api_hub_key", "SAP_API_HUB_KEY"),
}


def find_credentials(vcap: dict[str, Any], name: str = SERVICE_NAME) -> dict[str, Any] | None:
    """Credentials of the named service (any service type), else the first one that has a HANA host."""
    services = [s for group in vcap.values() if isinstance(group, list) for s in group if isinstance(s, dict)]
    for s in services:
        if s.get("name") == name or s.get("instance_name") == name:
            return s.get("credentials") or {}
    for s in services:
        creds = s.get("credentials") or {}
        if any(k in creds for k in KEYS["HANA_HOST"]):
            return creds
    return None


def apply_vcap(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Fill unset env vars from VCAP_SERVICES. Returns the variable names that were set (never values)."""
    env = os.environ if env is None else env
    raw = env.get("VCAP_SERVICES")
    if not raw:
        return []
    try:
        creds = find_credentials(json.loads(raw))
    except ValueError:
        log.warning("VCAP_SERVICES is not valid JSON; ignoring it")
        return []
    if not creds:
        return []
    applied = []
    for var, keys in KEYS.items():
        value = next((creds[k] for k in keys if creds.get(k) not in (None, "")), None)
        if value is not None and not env.get(var):
            env[var] = str(value)
            applied.append(var)
    if "HANA_HOST" in applied and not env.get("DB_BACKEND"):
        env["DB_BACKEND"] = "hana"
        applied.append("DB_BACKEND")
    return applied
