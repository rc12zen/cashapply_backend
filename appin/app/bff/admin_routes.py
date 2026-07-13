"""
app.bff.admin_routes
======================
/api/admin/* — User Management (design doc §7/§8). Administrator-only.
Role/permission changes are logged with BEFORE and AFTER values in the
audit metadata — "role changed" without "from what to what" is close to
useless for an audit, per the design doc's note on this.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db.models import Role, User
from ..deps import get_db
from ..auth import require_permission
from ..audit.service import log_activity

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/users")
def list_users(db: Session = Depends(get_db), user: User = Depends(require_permission("*"))):
    rows = db.query(User).order_by(User.email).all()
    return {"users": [{
        "id": u.id, "email": u.email, "display_name": u.display_name,
        "role": u.role.name if u.role else None, "is_active": u.is_active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    } for u in rows]}


@router.get("/roles")
def list_roles(db: Session = Depends(get_db), user: User = Depends(require_permission("*"))):
    rows = db.query(Role).order_by(Role.name).all()
    return {"roles": [{"id": r.id, "name": r.name, "description": r.description} for r in rows]}


@router.put("/users/{user_id}/role")
def change_user_role(user_id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                      admin: User = Depends(require_permission("*"))):
    target = db.query(User).get(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    new_role = db.query(Role).filter(Role.name == payload.get("role_name")).first()
    if not new_role:
        raise HTTPException(400, f"Unknown role: {payload.get('role_name')}")

    old_role_name = target.role.name if target.role else None
    target.role_id = new_role.id

    log_activity(db, admin, action="user.role_changed", entity_type="User", entity_id=target.id,
                 ip_address=_client_ip(request),
                 metadata={"from_role": old_role_name, "to_role": new_role.name, "target_email": target.email})
    db.commit()
    return {"id": target.id, "email": target.email, "role": new_role.name}


@router.put("/users/{user_id}/active")
def set_user_active(user_id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                     admin: User = Depends(require_permission("*"))):
    target = db.query(User).get(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    new_active = bool(payload.get("is_active", True))
    old_active = target.is_active
    target.is_active = new_active

    log_activity(db, admin, action="user.active_changed", entity_type="User", entity_id=target.id,
                 ip_address=_client_ip(request),
                 metadata={"from_active": old_active, "to_active": new_active, "target_email": target.email})
    db.commit()
    return {"id": target.id, "email": target.email, "is_active": target.is_active}
