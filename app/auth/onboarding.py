"""
app.auth.onboarding
=====================
Helpers for the invite-only onboarding scheme.

A user onboarded by an admin exists as a User row BEFORE they have ever logged
in, so they have no real Azure `oid` yet. `azure_oid` is NOT NULL + unique, so
we seed a deterministic PLACEHOLDER — "pending:<email>" — until first login.

On first SSO login (see app.auth.dependencies) the placeholder row is matched
by email and its `azure_oid` is replaced with the real Entra `oid` ("adopted"),
keeping the admin-assigned role. Local dev bypass identifies users by email, so
it works against a placeholder row directly with no adoption needed.
"""
from __future__ import annotations

PENDING_OID_PREFIX = "pending:"


def pending_oid(email: str) -> str:
    """Deterministic placeholder azure_oid for a not-yet-logged-in onboarded user."""
    return f"{PENDING_OID_PREFIX}{email.strip().lower()}"


def is_pending_oid(oid: str | None) -> bool:
    """True if this azure_oid is a placeholder (user has never completed SSO login)."""
    return bool(oid) and oid.startswith(PENDING_OID_PREFIX)
