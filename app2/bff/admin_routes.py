"""
app.bff.admin_routes
======================
/api/admin/* — User Management (Users tab). Gated on the "user:manage"
permission (Administrator holds "*", which satisfies it). Invite-only: admins
onboard users by email; the user first signs in later (SSO adopts their real
Entra oid, or local bypass identifies them by email). See PLAN-users-tab.md.

Role/permission changes are logged with BEFORE and AFTER values in the audit
metadata — "role changed" without "from what to what" is close to useless for
an audit.
"""
from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db.models import Permission, Role, RolePermission, User
from ..deps import get_db
from ..auth import require_permission
from ..auth.onboarding import pending_oid
from ..audit.service import log_activity

router = APIRouter()

_MANAGE = "user:manage"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _status(u: User) -> str:
    """UI status: disabled (deactivated) | pending (onboarded, never logged in) | active."""
    if not u.is_active:
        return "disabled"
    if u.last_login_at is None:
        return "pending"
    return "active"


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role.name if u.role else None,
        "is_active": u.is_active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "status": _status(u),
    }


def _admin_role_ids(db: Session) -> list[int]:
    """Role ids that grant the wildcard '*' (i.e. Administrator-equivalent)."""
    rows = (
        db.query(RolePermission.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .filter(Permission.code == "*")
        .all()
    )
    return [r[0] for r in rows]


def _is_last_active_admin(db: Session, target: User) -> bool:
    """True if `target` is the only remaining active user on an admin role.
    Used to prevent locking everyone out of user management."""
    admin_ids = _admin_role_ids(db)
    if target.role_id not in admin_ids:
        return False
    active_admins = (
        db.query(User)
        .filter(User.role_id.in_(admin_ids), User.is_active.is_(True))
        .count()
    )
    return active_admins <= 1


# ── list ─────────────────────────────────────────────────────────────────────

@router.get("/users")
def list_users(db: Session = Depends(get_db), user: User = Depends(require_permission(_MANAGE))):
    rows = db.query(User).order_by(User.email).all()
    return {"users": [_user_dict(u) for u in rows]}


@router.get("/roles")
def list_roles(db: Session = Depends(get_db), user: User = Depends(require_permission(_MANAGE))):
    """Roles + the permission codes each grants (read-only — roles are seed-defined)."""
    rows = db.query(Role).order_by(Role.name).all()
    # role_id -> [codes]
    perms: dict[int, list[str]] = {}
    for role_id, code in (
        db.query(RolePermission.role_id, Permission.code)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .all()
    ):
        perms.setdefault(role_id, []).append(code)
    return {"roles": [{
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "permissions": sorted(perms.get(r.id, [])),
    } for r in rows]}


# ── onboard (create) ───────────────────────────────────────────────────────────

class OnboardUserRequest(BaseModel):
    email: str
    display_name: str | None = None
    role_name: str


@router.post("/users")
def onboard_user(body: OnboardUserRequest, request: Request, db: Session = Depends(get_db),
                 admin: User = Depends(require_permission(_MANAGE))):
    """Onboard a user by email. Creates a row with a placeholder azure_oid; the
    user first signs in later (SSO adopts the real oid, local bypass matches by
    email). No email is sent — sign-in is via SSO / the login screen."""
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "A valid email address is required")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, f"A user with email '{email}' already exists")

    role = db.query(Role).filter(Role.name == body.role_name).first()
    if not role:
        raise HTTPException(400, f"Unknown role: {body.role_name}")

    display = (body.display_name or "").strip() or email.split("@")[0].title()
    new_user = User(
        azure_oid=pending_oid(email),
        email=email,
        display_name=display,
        role_id=role.id,
        is_active=True,
        provisioned_at=dt.datetime.utcnow(),
        last_login_at=None,
    )
    db.add(new_user)
    db.flush()

    log_activity(db, admin, action="user.onboarded", entity_type="User", entity_id=new_user.id,
                 ip_address=_client_ip(request),
                 metadata={"target_email": email, "role": role.name})
    db.commit()
    db.refresh(new_user)
    return _user_dict(new_user)


# ── edit (display name and/or role) ─────────────────────────────────────────────

class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    role_name: str | None = None


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UpdateUserRequest, request: Request,
                db: Session = Depends(get_db), admin: User = Depends(require_permission(_MANAGE))):
    target = db.query(User).get(user_id)
    if not target:
        raise HTTPException(404, "User not found")

    changes: dict = {}

    if body.display_name is not None:
        new_name = body.display_name.strip()
        if new_name and new_name != target.display_name:
            changes["display_name"] = {"from": target.display_name, "to": new_name}
            target.display_name = new_name

    if body.role_name is not None and (target.role is None or body.role_name != target.role.name):
        new_role = db.query(Role).filter(Role.name == body.role_name).first()
        if not new_role:
            raise HTTPException(400, f"Unknown role: {body.role_name}")
        # Guard: don't demote the last active administrator (lockout prevention).
        if new_role.id not in _admin_role_ids(db) and _is_last_active_admin(db, target):
            raise HTTPException(400, "Cannot change the role of the last active administrator")
        old_role_name = target.role.name if target.role else None
        changes["role"] = {"from": old_role_name, "to": new_role.name}
        target.role_id = new_role.id

    if changes:
        log_activity(db, admin, action="user.updated", entity_type="User", entity_id=target.id,
                     ip_address=_client_ip(request),
                     metadata={"target_email": target.email, "changes": changes})
    db.commit()
    db.refresh(target)
    return _user_dict(target)


# Back-compat: role-only change (kept; the UI uses PUT /users/{id}).
@router.put("/users/{user_id}/role")
def change_user_role(user_id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                     admin: User = Depends(require_permission(_MANAGE))):
    return update_user(user_id, UpdateUserRequest(role_name=payload.get("role_name")),
                       request, db, admin)


# ── activate / deactivate (soft delete) ─────────────────────────────────────────

@router.put("/users/{user_id}/active")
def set_user_active(user_id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                    admin: User = Depends(require_permission(_MANAGE))):
    target = db.query(User).get(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    new_active = bool(payload.get("is_active", True))

    if not new_active:
        # Lockout guards: can't deactivate yourself, or the last active admin.
        if target.id == admin.id:
            raise HTTPException(400, "You cannot deactivate your own account")
        if _is_last_active_admin(db, target):
            raise HTTPException(400, "Cannot deactivate the last active administrator")

    old_active = target.is_active
    target.is_active = new_active

    log_activity(db, admin, action="user.active_changed", entity_type="User", entity_id=target.id,
                 ip_address=_client_ip(request),
                 metadata={"from_active": old_active, "to_active": new_active, "target_email": target.email})
    db.commit()
    db.refresh(target)
    return _user_dict(target)
