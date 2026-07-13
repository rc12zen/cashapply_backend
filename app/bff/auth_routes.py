"""
app.bff.auth_routes
=====================
/api/auth/* — session-adjacent endpoints. There is no server-side session to
manage (see design doc §1.4 — Azure Entra ID is the sole token issuer), so
this is deliberately small: just "who am I" for the frontend to bootstrap
its UI (display name, role, permission list for conditional rendering).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import Permission, RolePermission, User
from ..deps import get_db
from ..auth import get_current_user

router = APIRouter()


@router.get("/me")
def get_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    permissions = (
        db.query(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .filter(RolePermission.role_id == user.role_id)
        .all()
    )
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.name if user.role else None,
        "permissions": [p[0] for p in permissions],
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
