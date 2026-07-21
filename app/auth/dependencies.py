"""
app.auth.dependencies
=======================
FastAPI dependencies for authentication. See design doc §1.

get_current_user() is the single entry point every protected route depends
on (directly or via require_permission()). It tries, in order:
  1. Local dev bypass (X-Dev-User header) — only reachable when
     ENVIRONMENT == "local"; see auth/bypass.py for the safety contract.
  2. Azure Entra ID bearer token validation.
"""
from __future__ import annotations

import datetime as dt

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import OAuth2AuthorizationCodeBearer
from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import User
from ..db.settings import get_settings
from ..deps import get_db
from .azure_validator import TokenValidationError, validate_azure_token
from .onboarding import is_pending_oid

# authorizationUrl/tokenUrl here are informational only (used for OpenAPI docs /
# the Swagger "Authorize" button) — this backend never issues or exchanges
# tokens itself, it only validates ones Azure already issued to the frontend.
_settings_for_urls = get_settings()
_tenant = _settings_for_urls.AZURE_TENANT_ID or "common"
oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=f"https://login.microsoftonline.com/{_tenant}/oauth2/v2.0/authorize",
    tokenUrl=f"https://login.microsoftonline.com/{_tenant}/oauth2/v2.0/token",
    auto_error=False,
)


def _extract_bearer_token(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip()


async def _validate_azure_and_load_user(token: str, db: Session) -> User:
    try:
        claims = validate_azure_token(token)
    except TokenValidationError as e:
        raise AppError(ErrorCode.TOKEN_INVALID, detail=str(e))

    azure_oid = claims.get("oid")
    if not azure_oid:
        raise AppError(ErrorCode.TOKEN_INVALID, detail="token missing 'oid' claim")

    # Invite-only (closed) onboarding — see PLAN-users-tab.md. There is NO
    # just-in-time auto-provisioning: an unknown SSO user is rejected. A user
    # must have been onboarded by an admin first.
    user = db.query(User).filter(User.azure_oid == azure_oid).first()
    if user is None:
        # First login for a pre-onboarded user: match by email against a row
        # that still carries a "pending:" placeholder oid, and adopt the real
        # Entra oid into it (keeping the admin-assigned role). Any other case
        # (no such email, or an email already bound to a different real oid) is
        # treated as not-onboarded.
        email = (claims.get("preferred_username") or claims.get("upn")
                 or claims.get("email") or "").strip().lower()
        pending = (
            db.query(User).filter(User.email == email).first() if email else None
        )
        if pending is not None and is_pending_oid(pending.azure_oid):
            pending.azure_oid = azure_oid
            user = pending
        else:
            raise AppError(ErrorCode.ACCOUNT_NOT_ONBOARDED)

    if not user.is_active:
        raise AppError(ErrorCode.ACCOUNT_DISABLED)

    user.last_login_at = dt.datetime.utcnow()
    db.commit()
    return user


async def get_current_user(
    request: Request,
    x_dev_user: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Primary auth dependency. Raises 401/403 on failure."""
    settings = get_settings()

    # Guard #1 — bypass module is only ever imported inside this branch.
    # Gated on APP_ENV (deployment tier), NOT STORAGE_BACKEND — a UAT/PROD
    # deployment can use local disk storage while still requiring real
    # Azure AD tokens here (see db/settings.py's docstring for why these
    # were split).
    if settings.APP_ENV == "local" and x_dev_user:
        from .bypass import get_bypass_user
        user = get_bypass_user(x_dev_user, db)
        if user is not None:
            # Deactivated users can't sign in, even via the dev bypass — mirror
            # the SSO path's is_active enforcement.
            if not user.is_active:
                raise AppError(ErrorCode.ACCOUNT_DISABLED)
            user.last_login_at = dt.datetime.utcnow()
            db.commit()
            request.state.current_user = user  # picked up by audit/middleware.py
            return user
        raise AppError(ErrorCode.ACCOUNT_NOT_ONBOARDED, detail=f"'{x_dev_user}' -- ask an administrator to add you in the Users tab")

    token = _extract_bearer_token(request)
    if not token:
        raise AppError(ErrorCode.NOT_SIGNED_IN)
    user = await _validate_azure_and_load_user(token, db)
    request.state.current_user = user  # picked up by audit/middleware.py
    return user


async def get_optional_current_user(
    request: Request,
    x_dev_user: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """Same as get_current_user but returns None instead of raising — for
    endpoints that behave differently for anonymous vs. authenticated callers
    (none currently, but kept available)."""
    try:
        return await get_current_user(request, x_dev_user, db)
    except (HTTPException, AppError):
        return None