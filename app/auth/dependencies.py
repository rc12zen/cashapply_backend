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
from .jit_provision import jit_provision_user
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

    # JIT provisioning (see auth/jit_provision.py): a verified Azure AD
    # identity with no existing row is auto-created here on
    # settings.DEFAULT_NEW_USER_ROLE (Viewer — zero permissions), same as
    # any other brand-new SSO user. An admin can also pre-provision a user
    # with a real role ahead of time via the Users tab ("pending:"
    # placeholder oid), which the block below adopts on first login instead
    # of creating a fresh Viewer row.
    user = db.query(User).filter(User.azure_oid == azure_oid).first()
    if user is None:
        # First login for this identity: match by email against a row that
        # still carries a "pending:" placeholder oid (pre-provisioned by an
        # admin) and adopt the real Entra oid into it, keeping the
        # admin-assigned role(s). Otherwise, auto-provision a brand-new
        # Viewer via jit_provision_user() below.
        email = (claims.get("preferred_username") or claims.get("upn")
                 or claims.get("email") or "").strip().lower()
        pending = (
            db.query(User).filter(User.email == email).first() if email else None
        )
        if pending is not None and is_pending_oid(pending.azure_oid):
            pending.azure_oid = azure_oid
            user = pending
        elif pending is not None:
            # Email already belongs to a different real, already-linked
            # account — never silently take it over.
            raise AppError(ErrorCode.ACCOUNT_NOT_ONBOARDED)
        else:
            if not email:
                raise AppError(ErrorCode.TOKEN_INVALID, detail="token missing email claim")
            user = jit_provision_user(db, azure_oid, email, claims.get("name"))

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