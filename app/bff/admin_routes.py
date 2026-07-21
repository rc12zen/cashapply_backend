"""
app.bff.admin_routes
======================
/api/admin/* — User Management (Users tab). Gated on the "user:manage"
permission (Administrator holds "*", which satisfies it). Invite-only: admins
onboard users by email; the user first signs in later (SSO adopts their real
Entra oid, or local bypass identifies them by email).

MULTI-ROLE: an Administrator can assign a user any number of roles at once
(e.g. both Analyst and Oracle Operator) — see db/models.py's UserRole join
table. Every endpoint here that used to take a single `role_name` now takes
`role_names: list[str]`, which REPLACES the user's full set of assigned
roles (not merges with it) — the frontend always sends the complete
intended set, same as a multi-select checkbox list would naturally produce.

Role/permission changes are logged with BEFORE and AFTER values in the audit
metadata — "roles changed" without "from what to what" is close to useless
for an audit.
"""
from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import Permission, Role, RolePermission, User, UserRole
from ..deps import get_db
from ..auth import require_permission
from ..auth.onboarding import pending_oid
from ..auth.role_priority import primary_role_name, sort_role_names
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
    role_names = sort_role_names(u.role_names)
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "roles": role_names,
        "role": primary_role_name(role_names),  # back-compat singular field
        "is_active": u.is_active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "status": _status(u),
    }


def _admin_role_ids(db: Session) -> set[int]:
    """Role ids that grant the wildcard '*' (i.e. Administrator-equivalent)."""
    rows = (
        db.query(RolePermission.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .filter(Permission.code == "*")
        .all()
    )
    return {r[0] for r in rows}


def _is_last_active_admin(db: Session, target: User, admin_ids: set[int] | None = None) -> bool:
    """True if `target` currently holds an admin-equivalent role AND is the
    only remaining active user who does. Used to prevent locking everyone
    out of user management. Checks ALL of target's roles, not just one."""
    admin_ids = admin_ids if admin_ids is not None else _admin_role_ids(db)
    target_role_ids = {ur.role_id for ur in target.user_roles}
    if not (target_role_ids & admin_ids):
        return False
    active_admin_user_ids = {
        ur.user_id
        for ur in db.query(UserRole).filter(UserRole.role_id.in_(admin_ids)).all()
    }
    if not active_admin_user_ids:
        return False
    active_count = (
        db.query(User)
        .filter(User.id.in_(active_admin_user_ids), User.is_active.is_(True))
        .count()
    )
    return active_count <= 1


def _resolve_roles(db: Session, role_names: list[str]) -> list[Role]:
    if not role_names:
        raise AppError(ErrorCode.USER_NO_ROLES_ASSIGNED)
    roles = db.query(Role).filter(Role.name.in_(role_names)).all()
    found_names = {r.name for r in roles}
    missing = [n for n in role_names if n not in found_names]
    if missing:
        raise AppError(ErrorCode.ROLE_UNKNOWN, detail=", ".join(missing))
    return roles


def _set_user_roles(db: Session, user: User, roles: list[Role]) -> None:
    """Replaces the user's full set of assigned roles with exactly `roles`.

    Goes through the ORM-tracked `user.user_roles` collection (cascade=
    "all, delete-orphan" — see db/models.py) rather than raw UserRole
    queries, so SQLAlchemy's identity map stays consistent. `user.roles`
    is a separate `viewonly=True` secondary relationship for convenient
    reads (see User.role_names) — it's loaded via its own query and does
    NOT auto-refresh just because `user_roles` changed in the same
    session, so it's explicitly expired here. Without this, code later in
    the same request (e.g. building the audit log entry or the response
    body) would see the OLD role list even though the new rows are
    already flushed.
    """
    user.user_roles = [UserRole(role_id=role.id) for role in roles]
    db.flush()
    db.expire(user, ["roles"])


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
    # Accepts one or more role names. `role_name` (singular) is still
    # accepted for a transition period -- both are folded into role_names.
    role_names: list[str] = Field(default_factory=list)
    role_name: str | None = None

    def resolved_role_names(self) -> list[str]:
        names = list(self.role_names)
        if self.role_name and self.role_name not in names:
            names.append(self.role_name)
        return names


@router.post("/users")
def onboard_user(body: OnboardUserRequest, request: Request, db: Session = Depends(get_db),
                 admin: User = Depends(require_permission(_MANAGE))):
    """Onboard a user by email. Creates a row with a placeholder azure_oid; the
    user first signs in later (SSO adopts the real oid, local bypass matches by
    email). No email is sent — sign-in is via SSO / the login screen."""
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise AppError(ErrorCode.USER_EMAIL_INVALID)
    if db.query(User).filter(User.email == email).first():
        raise AppError(ErrorCode.USER_ALREADY_EXISTS, detail=email)

    roles = _resolve_roles(db, body.resolved_role_names())

    display = (body.display_name or "").strip() or email.split("@")[0].title()
    new_user = User(
        azure_oid=pending_oid(email),
        email=email,
        display_name=display,
        is_active=True,
        provisioned_at=dt.datetime.utcnow(),
        last_login_at=None,
    )
    db.add(new_user)
    db.flush()
    _set_user_roles(db, new_user, roles)

    log_activity(db, admin, action="user.onboarded", entity_type="User", entity_id=new_user.id,
                 ip_address=_client_ip(request),
                 metadata={"target_email": email, "roles": [r.name for r in roles]})
    db.commit()
    db.refresh(new_user)
    return _user_dict(new_user)


# ── edit (display name and/or roles) ─────────────────────────────────────────────

class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    # The COMPLETE new set of roles this user should have (not a merge).
    # `role_name` (singular) still accepted for a transition period, treated
    # as a one-item list if `role_names` isn't also given.
    role_names: list[str] | None = None
    role_name: str | None = None

    def resolved_role_names(self) -> list[str] | None:
        if self.role_names is not None:
            return self.role_names
        if self.role_name is not None:
            return [self.role_name]
        return None


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UpdateUserRequest, request: Request,
                db: Session = Depends(get_db), admin: User = Depends(require_permission(_MANAGE))):
    target = db.query(User).get(user_id)
    if not target:
        raise AppError(ErrorCode.USER_NOT_FOUND)

    changes: dict = {}

    if body.display_name is not None:
        new_name = body.display_name.strip()
        if new_name and new_name != target.display_name:
            changes["display_name"] = {"from": target.display_name, "to": new_name}
            target.display_name = new_name

    new_role_names = body.resolved_role_names()
    if new_role_names is not None:
        old_role_names = sort_role_names(target.role_names)
        new_role_names_sorted = sorted(new_role_names)
        if sorted(old_role_names) != new_role_names_sorted:
            new_roles = _resolve_roles(db, new_role_names)
            admin_ids = _admin_role_ids(db)
            new_role_ids = {r.id for r in new_roles}
            # Guard: don't demote the last active administrator (lockout
            # prevention) -- i.e. if target currently props up the ONLY
            # active admin-equivalent account, the new role set must still
            # include an admin-equivalent role.
            if not (new_role_ids & admin_ids) and _is_last_active_admin(db, target, admin_ids):
                raise AppError(ErrorCode.LAST_ADMIN_LOCKOUT_ROLE)
            changes["roles"] = {"from": old_role_names, "to": sort_role_names(new_role_names)}
            _set_user_roles(db, target, new_roles)

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
    role_names = payload.get("role_names")
    if role_names is None and payload.get("role_name"):
        role_names = [payload["role_name"]]
    return update_user(user_id, UpdateUserRequest(role_names=role_names),
                       request, db, admin)


# ── activate / deactivate (soft delete) ─────────────────────────────────────────

@router.put("/users/{user_id}/active")
def set_user_active(user_id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                    admin: User = Depends(require_permission(_MANAGE))):
    target = db.query(User).get(user_id)
    if not target:
        raise AppError(ErrorCode.USER_NOT_FOUND)
    new_active = bool(payload.get("is_active", True))

    if not new_active:
        # Lockout guards: can't deactivate yourself, or the last active admin.
        if target.id == admin.id:
            raise AppError(ErrorCode.CANNOT_DEACTIVATE_SELF)
        if _is_last_active_admin(db, target):
            raise AppError(ErrorCode.LAST_ADMIN_LOCKOUT_DEACTIVATE)

    old_active = target.is_active
    target.is_active = new_active

    log_activity(db, admin, action="user.active_changed", entity_type="User", entity_id=target.id,
                 ip_address=_client_ip(request),
                 metadata={"from_active": old_active, "to_active": new_active, "target_email": target.email})
    db.commit()
    db.refresh(target)
    return _user_dict(target)
