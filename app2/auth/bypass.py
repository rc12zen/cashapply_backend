"""
app.auth.bypass
=================
Dev/test SSO bypass. See design doc §1.3.

SAFETY CONTRACT — read before touching this file:
  1. This module must ONLY ever be imported from inside a branch that has
     already checked `settings.APP_ENV == "local"`. It is never imported
     at module load time by app.main or app.auth.dependencies. NOTE:
     gated on APP_ENV (deployment tier), NOT STORAGE_BACKEND — a UAT/PROD
     deployment can use local disk storage while this bypass stays off.
  2. get_bypass_user() ALSO independently re-checks APP_ENV and
     returns None unconditionally when it is not "local". Two independent
     guards, not one — see the design doc for why.
  3. Do not add a "bypass in prod if a magic header is present" fallback of
     any kind, no matter how tempting during an incident. Use a real Azure
     test account for that instead.

ALLOWLIST NOTE:
  This used to ALSO require the email to be in DEV_SSO_BYPASS_EMAILS. With the
  invite-only Users tab, the Users table IS the allowlist — only emails an admin
  has onboarded exist as rows, so an unknown email resolves to None here anyway.
  The env allowlist was therefore redundant friction (every onboarded user had
  to be hand-added to .env before they could log in locally), so it was dropped.
  DEV_SSO_BYPASS_EMAILS is now unused/legacy. The caller (dependencies.py) still
  enforces `is_active`, so deactivated users cannot bypass-login. This only
  affects local dev — the prod SSO path is unchanged.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import User
from ..db.settings import get_settings


def get_bypass_user(x_dev_user: str | None, db: Session) -> User | None:
    settings = get_settings()

    # Guard #2 (independent of the caller's guard #1).
    if settings.APP_ENV != "local":
        return None

    if not x_dev_user:
        return None

    # The Users table is the allowlist: an email that hasn't been onboarded has
    # no row and returns None. is_active is enforced by the caller.
    return db.query(User).filter(User.email == x_dev_user.strip().lower()).first()