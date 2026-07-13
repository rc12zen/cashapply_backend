"""
scripts/seed_rbac.py
======================
One-time (idempotent) seed for roles/permissions and, optionally, a first
local dev user so you can log in via the X-Dev-User bypass without waiting
on real Azure SSO. Safe to re-run — every insert is get-or-create.

Usage:
    python -m scripts.seed_rbac
    python -m scripts.seed_rbac --dev-user you@example.com --dev-role Administrator

Run this AFTER the app has started at least once (so init_db()'s
create_all() has already created the tables), or run `python -c
"from app.db.session import init_db; init_db()"` first.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import session_scope  # noqa: E402
from app.db.models import Permission, Role, RolePermission, User  # noqa: E402

# role_name -> [permission codes]. "*" = every permission (Administrator).
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "Administrator": ["*"],
    "Analyst": ["statement:upload", "run:start", "run:view", "hitl:reject"],
    "Oracle Operator": ["oracle:post", "oracle:retry", "run:view", "hitl:reject"],
    "Auditor": ["run:view", "report:download", "activity_log:view"],
    "Viewer": ["dashboard:view", "run:view"],
}

ALL_PERMISSION_CODES = sorted({
    code
    for codes in ROLE_PERMISSIONS.values()
    for code in codes
    if code != "*"
} | {
    "statement:upload", "run:start", "run:view", "oracle:post", "oracle:retry",
    "hitl:reject", "report:download", "activity_log:view", "dashboard:view", "*",
})


def get_or_create_permission(db, code: str) -> Permission:
    p = db.query(Permission).filter(Permission.code == code).first()
    if p is None:
        p = Permission(code=code)
        db.add(p)
        db.flush()
    return p


def get_or_create_role(db, name: str, description: str = "") -> Role:
    r = db.query(Role).filter(Role.name == name).first()
    if r is None:
        r = Role(name=name, description=description)
        db.add(r)
        db.flush()
    return r


def seed_rbac() -> None:
    with session_scope() as db:
        permissions_by_code = {code: get_or_create_permission(db, code) for code in ALL_PERMISSION_CODES}

        for role_name, codes in ROLE_PERMISSIONS.items():
            role = get_or_create_role(db, role_name)
            for code in codes:
                perm = permissions_by_code[code]
                exists = (
                    db.query(RolePermission)
                    .filter(RolePermission.role_id == role.id, RolePermission.permission_id == perm.id)
                    .first()
                )
                if not exists:
                    db.add(RolePermission(role_id=role.id, permission_id=perm.id))

        print(f"Seeded {len(ROLE_PERMISSIONS)} roles and {len(ALL_PERMISSION_CODES)} permissions.")


def seed_dev_user(email: str, role_name: str, azure_oid: str | None = None) -> None:
    with session_scope() as db:
        role = db.query(Role).filter(Role.name == role_name).first()
        if role is None:
            raise SystemExit(f"Role '{role_name}' not found — run seed_rbac() first.")

        existing = db.query(User).filter(User.email == email.lower()).first()
        if existing:
            existing.role_id = role.id
            print(f"Updated existing user {email} -> role {role_name}")
            return

        db.add(User(
            azure_oid=azure_oid or f"local-dev-{email}",
            email=email.lower(),
            display_name=email.split("@")[0].title(),
            role_id=role.id,
            is_active=True,
        ))
        print(f"Created dev user {email} with role {role_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-user", help="Email to create/update as a local dev user (bypass login)")
    parser.add_argument("--dev-role", default="Administrator", help="Role to assign the dev user")
    args = parser.parse_args()

    seed_rbac()
    if args.dev_user:
        seed_dev_user(args.dev_user, args.dev_role)
