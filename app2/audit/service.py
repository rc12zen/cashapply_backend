"""
app.audit.service
===================
Append-only activity log writer. See design doc §6.

log_activity() deliberately does NOT commit — it rides on the caller's
existing transaction so a log entry never exists for an action that then
rolled back, and vice versa. The one exception is the generic request
middleware (audit/middleware.py), which owns its own commit since it runs
after the route's own transaction has already completed.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import ActivityLog, User


def log_activity(
    db: Session,
    user: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: int | str | None = None,
    status: str = "success",
    ip_address: str | None = None,
    metadata: dict | None = None,
) -> None:
    db.add(ActivityLog(
        user_id=user.id if user else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        status=status,
        ip_address=ip_address,
        log_metadata=metadata or {},
    ))
