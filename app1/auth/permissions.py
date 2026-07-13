"""
app.auth.permissions
======================
RBAC permission check. See design doc §7.

Usage on a route:
    @router.post("/approve/{id}")
    def approve(id: int, user: User = Depends(require_permission("oracle:post")), ...):
        ...

A role holding the wildcard permission "*" (Administrator) passes every check.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.models import Permission, RolePermission, User
from ..deps import get_db
from .dependencies import get_current_user


def user_has_permission(db: Session, user: User, permission_code: str) -> bool:
    return (
        db.query(RolePermission)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .filter(
            RolePermission.role_id == user.role_id,
            Permission.code.in_([permission_code, "*"]),
        )
        .first()
        is not None
    )


def require_permission(permission_code: str):
    def dependency(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not user_has_permission(db, user, permission_code):
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {permission_code}",
            )
        return user

    return dependency
