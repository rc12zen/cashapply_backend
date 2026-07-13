"""
app.bff.activity_log_routes
=============================
/api/activity-log/* — read-only audit trail view (design doc §6). Not yet
wired to the frontend's app/activity-log/page.tsx (that page currently has
no API calls at all — it's on mock data); this gives it a real contract to
point at whenever that's picked up.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..db.models import ActivityLog, User
from ..deps import get_db
from ..auth import require_permission

router = APIRouter()


@router.get("")
def get_activity_log(
    page: int = 1, page_size: int = 50,
    user_id: int | None = None, action: str | None = None,
    entity_type: str | None = None, entity_id: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("activity_log:view")),
):
    q = db.query(ActivityLog)
    if user_id:
        q = q.filter(ActivityLog.user_id == user_id)
    if action:
        q = q.filter(ActivityLog.action == action)
    if entity_type:
        q = q.filter(ActivityLog.entity_type == entity_type)
    if entity_id:
        q = q.filter(ActivityLog.entity_id == entity_id)

    total = q.count()
    rows = (
        q.order_by(desc(ActivityLog.created_at))
        .offset((page - 1) * page_size).limit(page_size).all()
    )
    data = [{
        "id": r.id, "user_id": r.user_id,
        "user_email": r.user.email if r.user else None,
        "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
        "status": r.status, "ip_address": r.ip_address, "metadata": r.log_metadata,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
    return {"data": data, "total": total, "page": page, "page_size": page_size}
