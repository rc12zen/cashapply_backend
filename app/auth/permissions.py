"""
app.auth.permissions
======================
RBAC permission check. A user can hold MULTIPLE roles at once (see
db/models.py's UserRole join table, and bff/admin_routes.py where an
Administrator assigns them) — their effective permission set is the UNION
of every assigned role's permissions.

Usage on a route:
    @router.post("/approve/{id}")
    def approve(id: int, user: User = Depends(require_permission("oracle:post")), ...):
        ...

A user holding a role with the wildcard permission "*" (Administrator)
passes every check, no matter what their other roles are.
"""
from __future__ import annotations

from fastapi import Depends

from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import Permission, RolePermission, User, UserRole
from ..deps import get_db
from .dependencies import get_current_user


def get_user_permission_codes(db: Session, user: User) -> set[str]:
    """The full, de-duplicated set of permission codes this user holds
    across ALL of their assigned roles."""
    role_ids = [ur.role_id for ur in db.query(UserRole).filter(UserRole.user_id == user.id).all()]
    if not role_ids:
        return set()
    rows = (
        db.query(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .filter(RolePermission.role_id.in_(role_ids))
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def user_has_permission(db: Session, user: User, permission_code: str) -> bool:
    codes = get_user_permission_codes(db, user)
    return "*" in codes or permission_code in codes


def permission_set_has(permission_codes: set[str], permission_code: str) -> bool:
    """Same wildcard rule as user_has_permission(), but against an
    already-resolved permission set (see hitl/actions_registry.py, which
    resolves the set once per request rather than re-querying per action)."""
    return "*" in permission_codes or permission_code in permission_codes


def require_permission(permission_code: str):
    def dependency(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not user_has_permission(db, user, permission_code):
            raise AppError(ErrorCode.PERMISSION_DENIED, detail=f"missing permission: {permission_code}")
        return user

    return dependency
