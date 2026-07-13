"""
app.auth.azure_validator
=========================
Validates Azure Entra ID (Azure AD) access tokens against the tenant's
published JWKS. No token is ever issued by this backend — it is purely a
resource server. See design doc §1.1.

JWKS is fetched once and cached in-process for AZURE_JWKS_CACHE_SECONDS;
re-fetched on cache expiry or on a signing-key-not-found (kid rotation).
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JOSEError

from ..db.settings import get_settings

_jwks_cache: dict[str, Any] = {"keys": None, "fetched_at": 0.0}


class TokenValidationError(Exception):
    pass


def _jwks_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


def _get_jwks(tenant_id: str) -> dict:
    settings = get_settings()
    now = time.time()
    if _jwks_cache["keys"] is not None and (now - _jwks_cache["fetched_at"]) < settings.AZURE_JWKS_CACHE_SECONDS:
        return _jwks_cache["keys"]

    resp = httpx.get(_jwks_url(tenant_id), timeout=10)
    resp.raise_for_status()
    keys = resp.json()
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


def _signing_key_for(token: str, tenant_id: str) -> dict:
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")
    jwks = _get_jwks(tenant_id)
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    # kid not found — could be a key rotation; force one refetch before giving up.
    _jwks_cache["keys"] = None
    jwks = _get_jwks(tenant_id)
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    raise TokenValidationError(f"Signing key '{kid}' not found in tenant JWKS")


def validate_azure_token(token: str) -> dict:
    """
    Validates signature, issuer, audience, expiry. Returns the decoded claims
    dict on success. Raises TokenValidationError on any failure — callers
    should turn that into HTTP 401, never fall back to trusting the token.
    """
    settings = get_settings()
    if not settings.AZURE_TENANT_ID or not settings.AZURE_CLIENT_ID:
        raise TokenValidationError(
            "AZURE_TENANT_ID / AZURE_CLIENT_ID not configured — cannot validate SSO tokens. "
            "Set these in .env, or use the local dev bypass (ENVIRONMENT=local)."
        )
    try:
        signing_key = _signing_key_for(token, settings.AZURE_TENANT_ID)
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=settings.AZURE_CLIENT_ID,
            issuer=f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}/v2.0",
            options={"verify_at_hash": False},
        )
        return claims
    except JOSEError as e:
        raise TokenValidationError(f"Token validation failed: {e}") from e
