"""
scripts/seed_rbac.py
=======================
Seeds the fixed 5-role RBAC model referenced throughout the backend
(auth/jit_provision.py, auth/permissions.py, bff/admin_routes.py's Role
Legend, etc.) Roles are seed-defined, not created through the UI — the
Users page only assigns EXISTING role(s) to a user, it never invents a
new one.

Run once per environment (idempotent — safe to re-run; it upserts):

    python -m scripts.seed_rbac

Optionally also bootstrap a first local/dev user in the same run — useful
right after a fresh DB, since the Users tab itself needs an existing
Administrator to call it (chicken-and-egg on a brand-new database). Accepts
one or more roles (an Administrator can hold multiple roles — see
db/models.py's UserRole join table):

    python -m scripts.seed_rbac --dev-user you@example.com --dev-role Administrator
    python -m scripts.seed_rbac --dev-user you@example.com --dev-role Analyst --dev-role "Oracle Operator"

Or seed 4 standard demo users in one go — Administrator, Viewer, Auditor,
and a multi-role Analyst + Oracle Operator user (see DEMO_USERS below):

    python -m scripts.seed_rbac --demo-users

Run this AFTER the app has started at least once (so init_db()'s
create_all() has already created the tables), or run `python -c
"from app.db.session import init_db; init_db()"` first.

── THE 5 ROLES & WHAT THEY CAN DO ──────────────────────────────────────────
Administrator   — everything. Holds the wildcard "*" permission, which
                  satisfies every require_permission(...) check.
Analyst         — preparer + config author. Uploads statements
                  (statement:upload), starts & monitors runs (run:start,
                  run:monitor), maps invoices (hitl:map), authors config &
                  bank-format recipes (config:view, config:author),
                  downloads files (file:download), and views the activity
                  log (activity_log:view). CANNOT approve/reject/post
                  (no oracle:post / hitl:reject), manage the aging report,
                  delete recipes (both config:manage, Admin-only), or
                  manage users.
Oracle Operator — approver/poster. Views data everywhere (run:view), maps
                  invoices (hitl:map), rejects (hitl:reject), and
                  approves/posts to Oracle (oracle:post); downloads files
                  (file:download); views the activity log. CANNOT run or
                  monitor analysis, nor see/author config (no run:start,
                  run:monitor, config:view, config:author).
Auditor         — read-only. Views data (run:view), config (config:view),
                  and the activity log (activity_log:view). CANNOT run,
                  monitor, map, approve, reject, download files, or manage
                  anything.
Viewer          — the default role a brand-new SSO/JIT user lands on (see
                  db/settings.py's DEFAULT_NEW_USER_ROLE). Holds NO
                  permissions at all — the frontend keeps a Viewer on the
                  single Welcome page until an Administrator assigns them
                  a real role.

── PERMISSION CODES ─────────────────────────────────────────────────────────
"*"                — wildcard, Administrator only.
run:view           — broad read across Home dashboard / Overview / Analysis
                     History / Results / Executive Summary / AI Usage /
                     aging status-history-preview / bank-account list /
                     Filters / HITL read.
run:monitor        — view live run status/progress, file preview, and ingest
                     status (Analyst only).
run:start          — start / reset an analysis run.
statement:upload   — upload / delete / re-ingest a bank statement.
config:view        — view config data (abbreviations, AI status, format
                     detection), the Config tab, AND the saved bank-format
                     recipe list / account detail (read-only). Analyst,
                     Auditor.
config:author      — config WRITES: edit abbreviations, test config, Config
                     Builder upload/raw-preview/locate/test/save, edit
                     account→business-unit mapping; plus the Config Builder
                     OU list (available-ous) used while authoring. Analyst
                     only.
hitl:map           — confirm a manual invoice mapping / recheck remittance
                     (does NOT post to Oracle by itself).
hitl:reject        — reject a HITL row.
oracle:post        — approve a row / post to Oracle Fusion / retry a post.
file:download      — download a stored file (Analyst, Oracle Operator).
activity_log:view  — view the Activity Log (Analyst, Oracle Operator,
                     Auditor, Administrator).
user:manage        — onboard/edit/activate/deactivate users + purge system
                     logs (Users tab). Administrator only.
config:manage      — Admin-only config that stays locked: aging
                     upload/select/refresh/remove and DELETE a bank-format
                     recipe. Administrator only.
"""
from __future__ import annotations

import argparse

from app.db.models import Permission, Role, RolePermission, User, UserRole
from app.db.session import session_scope
from app.auth.onboarding import pending_oid

# role name -> (description, [permission codes])
ROLE_PERMISSIONS: dict[str, tuple[str, list[str]]] = {
    "Administrator": (
        "Full access — every permission, no constraints.",
        ["*"],
    ),
    "Analyst": (
        "Preparer + config author: uploads statements, runs & monitors analysis, "
        "maps invoices, authors config/recipes, downloads files, views the activity "
        "log. Cannot approve/reject/post, manage aging, delete recipes, or manage users.",
        ["run:view", "run:start", "statement:upload", "run:monitor",
         "config:view", "config:author", "hitl:map", "file:download",
         "activity_log:view"],
    ),
    "Oracle Operator": (
        "Approver/poster: reviews, maps, rejects, and approves/posts to Oracle; "
        "downloads files; views the activity log. Cannot run analysis, monitor runs, "
        "or see/author config.",
        ["run:view", "hitl:map", "hitl:reject", "oracle:post",
         "file:download", "activity_log:view"],
    ),
    "Auditor": (
        "Read-only — views data, config, and the activity log everywhere; cannot run, "
        "monitor, map, approve, reject, download files, or manage anything.",
        ["run:view", "config:view", "activity_log:view"],
    ),
    "Viewer": (
        "Default role for a brand-new user. No permissions — restricted to the single Welcome page until an administrator assigns a real role.",
        [],
    ),
}

# Admin-only codes that appear in no non-admin role list above, but must still
# exist as Permission rows: config:manage (aging writes + delete recipe) and
# user:manage (user administration + purge system logs).
ALL_PERMISSION_CODES = sorted({
    code
    for _desc, codes in ROLE_PERMISSIONS.values()
    for code in codes
} | {"user:manage", "config:manage"})


def seed_rbac() -> None:
    with session_scope() as db:
        # 1. Ensure every permission code exists as a row.
        existing_perms = {p.code: p for p in db.query(Permission).all()}
        for code in ALL_PERMISSION_CODES:
            if code not in existing_perms:
                perm = Permission(code=code)
                db.add(perm)
                db.flush()
                existing_perms[code] = perm

        # 2. Ensure every role exists (create if missing, update description
        #    if it changed — never delete a role or touch its users).
        existing_roles = {r.name: r for r in db.query(Role).all()}
        for name, (description, codes) in ROLE_PERMISSIONS.items():
            role = existing_roles.get(name)
            if role is None:
                role = Role(name=name, description=description)
                db.add(role)
                db.flush()
                existing_roles[name] = role
            elif role.description != description:
                role.description = description

            # Administrator gets ONLY "*" (wildcard already satisfies every
            # check — see auth/permissions.py) so it isn't cluttered with
            # every other code too; every other role gets exactly its list.
            wanted_codes = set(codes)
            current = {
                rp.permission.code
                for rp in db.query(RolePermission).filter(RolePermission.role_id == role.id).all()
            }
            for code in wanted_codes - current:
                db.add(RolePermission(role_id=role.id, permission_id=existing_perms[code].id))
            for rp in db.query(RolePermission).filter(RolePermission.role_id == role.id).all():
                if rp.permission.code not in wanted_codes:
                    db.delete(rp)

        print("RBAC seed complete:")
        for name, (_desc, codes) in ROLE_PERMISSIONS.items():
            print(f"  {name:16s} -> {codes}")


def seed_dev_user(email: str, role_names: list[str], azure_oid: str | None = None) -> None:
    """Creates (or updates the role assignment of) a local/dev user with
    one or more roles — an Administrator can hold multiple roles at once
    (see db/models.py's UserRole join table), so this takes a LIST, not a
    single role name.

    Placeholder azure_oid defaults to "pending:<email>" (same scheme the
    Users tab uses for onboarding) — this matters for the FIRST-admin
    bootstrap on a production/SSO deployment: on that admin's first real
    SSO login, the reconciliation path (auth/dependencies.py) matches by
    email and adopts their real Entra oid, but ONLY for "pending:"
    placeholders. Local dev-bypass identifies users by email regardless,
    so this placeholder works there too.
    """
    email = email.strip().lower()
    with session_scope() as db:
        roles = db.query(Role).filter(Role.name.in_(role_names)).all()
        found_names = {r.name for r in roles}
        missing = [n for n in role_names if n not in found_names]
        if missing:
            raise SystemExit(f"Role(s) not found: {', '.join(missing)} — run seed_rbac() first.")

        existing = db.query(User).filter(User.email == email).first()
        if existing:
            existing.user_roles = [UserRole(role_id=r.id) for r in roles]
            db.flush()
            db.expire(existing, ["roles"])
            print(f"Updated existing user {email} -> role(s) {', '.join(role_names)}")
            return

        user = User(
            azure_oid=azure_oid or pending_oid(email),
            email=email,
            display_name=email.split("@")[0].title(),
            is_active=True,
        )
        db.add(user)
        db.flush()
        user.user_roles = [UserRole(role_id=r.id) for r in roles]
        print(f"Created dev user {email} with role(s) {', '.join(role_names)}")


DEMO_USERS: list[tuple[str, list[str]]] = [
    ("muni@zensar.com", ["Administrator"]),
    ("viewer@example.com", ["Viewer"]),
    ("auditor@example.com", ["Auditor"]),
    ("multi@example.com", ["Analyst", "Oracle Operator"]),
]


def seed_demo_users() -> None:
    """Seeds the 4 standard demo users covering every role combination:
    Administrator, Viewer, Auditor, and a multi-role Analyst + Oracle
    Operator user. Handy for exercising RBAC locally without onboarding
    each one by hand via the Users tab."""
    for email, role_names in DEMO_USERS:
        seed_dev_user(email, role_names)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-user", help="Email to create/update as a local dev user (bypass login)")
    parser.add_argument(
        "--dev-role", action="append", default=[],
        help="Role to assign the dev user. Repeat for multiple roles, e.g. "
             "--dev-role Analyst --dev-role \"Oracle Operator\". Defaults to Administrator.",
    )
    parser.add_argument(
        "--demo-users", action="store_true",
        help="Also seed the 4 standard demo users: Administrator, Viewer, "
             "Auditor, and a multi-role Analyst + Oracle Operator user.",
    )
    args = parser.parse_args()

    seed_rbac()
    if args.dev_user:
        seed_dev_user(args.dev_user, args.dev_role or ["Administrator"])
    if args.demo_users:
        seed_demo_users()