"""
app.auth.bypass
=================
Dev/test SSO bypass. See design doc §1.3.

SAFETY CONTRACT — read before touching this file:
  1. This module must ONLY ever be imported from inside a branch that has
     already checked `settings.ENVIRONMENT == "local"`. It is never imported
     at module load time by app.main or app.auth.dependencies.
  2. get_bypass_user() ALSO independently re-checks the environment and
     returns None unconditionally when it is not "local". Two independent
     guards, not one — see the design doc for why.
  3. Do not add a "bypass in prod if a magic header is present" fallback of
     any kind, no matter how tempting during an incident. Use a real Azure
     test account for that instead.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import User
from ..db.settings import get_settings


def get_bypass_user(x_dev_user: str | None, db: Session) -> User | None:
    settings = get_settings()

    # Guard #2 (independent of the caller's guard #1).
    if settings.ENVIRONMENT != "local":
        return None

    if not x_dev_user:
        return None

    allowed = {
        e.strip().lower()
        for e in (settings.DEV_SSO_BYPASS_EMAILS or "").split(",")
        if e.strip()
    }
    if x_dev_user.strip().lower() not in allowed:
        return None

    return db.query(User).filter(User.email == x_dev_user.strip().lower()).first()
