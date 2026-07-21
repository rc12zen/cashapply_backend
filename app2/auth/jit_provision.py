"""
app.auth.jit_provision
========================
Just-in-time user provisioning on first successful SSO login. See design
doc §1.2. New users always land on the lowest-privilege role — an
Administrator must explicitly promote them via the User Management screen.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import Role, User
from ..db.settings import get_settings


def get_or_create_default_role(db: Session) -> Role:
    settings = get_settings()
    role = db.query(Role).filter(Role.name == settings.DEFAULT_NEW_USER_ROLE).first()
    if role is None:
        # Should already exist from scripts/seed_rbac.py — this is a defensive
        # fallback so JIT provisioning never hard-fails because seeding wasn't run.
        role = Role(name=settings.DEFAULT_NEW_USER_ROLE, description="Auto-created fallback role")
        db.add(role)
        db.flush()
    return role


def jit_provision_user(db: Session, azure_oid: str, email: str, display_name: str | None) -> User:
    role = get_or_create_default_role(db)
    user = User(
        azure_oid=azure_oid,
        email=email.strip().lower(),
        display_name=display_name,
        role_id=role.id,
        is_active=True,
        provisioned_at=dt.datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
