"""
app.bff.activity_log_routes
=============================
/api/activity-log/* — read-only audit trail view (design doc §6). Now
wired to the frontend's app/activity-log/page.tsx (previously that page had
no API calls at all — it was on mock data).

PATCH NOTES:
  - Added date_from / date_to (the page's timeline filter had nothing to
    call before this).
  - Added `category`, a convenience grouping on top of the raw `action`
    column, since one UI pill ("File Uploads", "Approvals", ...) maps to
    several distinct action strings (e.g. "File Uploads" covers both
    statement.upload and statement.upload_rejected_duplicate) — the
    frontend would otherwise need to fire one request per action value.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from ..db.models import ActivityLog, User
from ..deps import get_db
from ..auth import require_permission

router = APIRouter()

# Category → action-prefix patterns (SQL LIKE). Kept in one place so the
# frontend pill labels and the backend filter never drift apart.
_CATEGORY_ACTION_LIKE: dict[str, list[str]] = {
    "file_upload":  ["statement.upload%"],
    "analysis_run": ["run.start%", "run.reset%", "statement.ingest_complete%"],
    "approved":     ["hitl.approve%"],
    "rejected":     ["hitl.reject%"],
}


@router.get("")
def get_activity_log(
    page: int = 1, page_size: int = 50,
    user_id: int | None = None, action: str | None = None,
    entity_type: str | None = None, entity_id: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    category: str | None = None,
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
    if date_from:
        q = q.filter(ActivityLog.created_at >= date_from)
    if date_to:
        q = q.filter(ActivityLog.created_at <= date_to)
    if category and category in _CATEGORY_ACTION_LIKE:
        patterns = _CATEGORY_ACTION_LIKE[category]
        q = q.filter(or_(*(ActivityLog.action.like(p) for p in patterns)))

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
