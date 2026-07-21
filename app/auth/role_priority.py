"""
app.auth.role_priority
=========================
A user can now hold MULTIPLE roles at once (see db/models.py's UserRole
join table) — their effective permissions are the UNION of every assigned
role (see permissions.py::user_has_permission). This module is ONLY about
display ordering/labeling (e.g. "which role badge shows first", "is this
user administrator-equivalent"), not about permission resolution itself.
"""
from __future__ import annotations

# Higher = shown first / treated as "most privileged" for display purposes
# only. Doesn't affect what a user can actually do — that's strictly the
# union of permissions across ALL of their assigned roles.
_DISPLAY_PRIORITY = {
    "Administrator": 100,
    "Oracle Operator": 80,
    "Analyst": 70,
    "Auditor": 50,
    "Viewer": 0,
}


def sort_role_names(role_names: list[str]) -> list[str]:
    """Sorts role names highest-priority first, for consistent badge/label
    ordering in the UI (e.g. "Administrator, Analyst" not "Analyst,
    Administrator")."""
    return sorted(role_names, key=lambda n: -_DISPLAY_PRIORITY.get(n, 0))


def primary_role_name(role_names: list[str]) -> str | None:
    """The single "headline" role for places that only have room for one
    label (e.g. a compact list row). Prefer sort_role_names(...) directly
    wherever multiple badges can actually be shown."""
    if not role_names:
        return None
    return sort_role_names(role_names)[0]
