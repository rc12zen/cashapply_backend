"""
app.bff.auth_routes
=====================
/api/auth/* — session-adjacent endpoints. There is no server-side session to
manage (Azure Entra ID is the sole token issuer), so this is deliberately
small: just "who am I" for the frontend to bootstrap its UI (display name,
roles, permission list for conditional rendering).

A user can hold MULTIPLE roles at once (see db/models.py's UserRole join
table) — `roles` is the full list; `role` (singular) is kept for backward
compatibility with any older frontend code and is just the highest-
priority one for display (see auth/role_priority.py) — permission checks
never use it, they always use the full `permissions` union below.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth.permissions import get_user_permission_codes
from ..auth.role_priority import primary_role_name, sort_role_names
from ..db.models import User
from ..deps import get_db
from ..auth import get_current_user

router = APIRouter()


@router.get("/me")
def get_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    role_names = sort_role_names(user.role_names)
    permissions = sorted(get_user_permission_codes(db, user))
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "roles": role_names,
        "role": primary_role_name(role_names),  # back-compat singular field
        "permissions": permissions,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
