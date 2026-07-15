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
from .date_range import parse_date_from, parse_date_to

router = APIRouter()

# Category → action-prefix patterns (SQL LIKE) + optional actor constraint.
# Kept in one place so the frontend pill labels and the backend filter never
# drift apart. actor="user" → only real users (user_id NOT NULL);
# actor="system" → only background/pipeline rows (user_id NULL).
_CATEGORIES: dict[str, dict] = {
    "analysis_run":      {"like": ["run.start%", "run.reset%"], "actor": "user"},
    "config_creation":   {"like": ["config.create%", "config.version_added%"]},
    "approved":          {"like": ["hitl.approve%", "oracle.retry%"]},
    "rejected":          {"like": ["hitl.reject%"]},
}


def _summarize(r: ActivityLog, email: str | None) -> str:
    """Plain-English, one-line description of a log row for non-engineers."""
    who = email or "System"
    m = r.log_metadata or {}
    a = r.action or ""
    id_ = r.entity_id
    if a.startswith("run.start"):
        files = m.get("selected_files") or []
        return f"{who} started an Analysis Run" + (f" on {', '.join(files)}" if files else "")
    if a.startswith("run.reset"):
        return f"{who} reset Analysis Run #{id_}"
    if a.startswith("statement.ingest_complete"):
        return f"Statement #{id_} processed — {m.get('new_rows', 0)} new rows, {m.get('duplicate_rows', 0)} duplicates"
    if a.startswith("statement.upload_rejected_duplicate"):
        return f"{who} uploaded a duplicate statement (rejected)"
    if a.startswith("statement.upload"):
        return f"{who} uploaded a statement file"
    if a.startswith("statement.restore"):
        return f"{who} restored a statement"
    if a.startswith("statement.delete"):
        return f"{who} deleted statement {id_}"
    if a.startswith("hitl.manual_mapping"):
        return f"{who} mapped line item #{id_} to invoice(s) {', '.join(m.get('invoice_numbers') or [])}"
    if a.startswith("config.create"):
        name = m.get("display_name")
        src = m.get("source_filename")
        return f"{who} added {name}" + (f" for {src}" if src else "")
    if a.startswith("config.version_added"):
        name = m.get("display_name")
        src = m.get("source_filename")
        return f"{who} added a new version of {name}" + (f" for {src}" if src else "")
    if a.startswith("hitl.approve_bulk"):
        return f"{who} approved multiple line items"
    if a.startswith("hitl.approve"):
        return f"{who} approved line item #{id_}"
    if a.startswith("hitl.reject"):
        return f"{who} rejected line item #{id_}"
    if a.startswith("oracle.retry"):
        return f"{who} retried Oracle posting for line item #{id_}"
    if a.startswith("user.role_changed"):
        return f"{who} changed {m.get('target_email')}'s role from {m.get('from_role')} to {m.get('to_role')}"
    if a.startswith("user.active_changed"):
        return f"{who} {'activated' if m.get('to_active') else 'deactivated'} {m.get('target_email')}"
    for verb in ("GET ", "POST ", "PUT ", "DELETE ", "PATCH "):
        if a.startswith(verb):
            base = f"{who} viewed {a[len(verb):]}" if verb == "GET " else f"{who} performed {a}"
            return base + (" (failed)" if r.status == "failure" else "")
    return f"{who} — {a}"


@router.get("")
def get_activity_log(
    page: int = 1, page_size: int = 50,
    user_id: int | None = None, user_email: str | None = None,
    action: str | None = None,
    entity_type: str | None = None, entity_id: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("activity_log:view")),
):
    q = db.query(ActivityLog)
    if user_id:
        q = q.filter(ActivityLog.user_id == user_id)
    if user_email:
        q = q.join(User).filter(User.email == user_email)
    if action:
        q = q.filter(ActivityLog.action == action)
    if entity_type:
        q = q.filter(ActivityLog.entity_type == entity_type)
    if entity_id:
        q = q.filter(ActivityLog.entity_id == entity_id)
    if date_from:
        q = q.filter(ActivityLog.created_at >= parse_date_from(date_from))
    if date_to:
        q = q.filter(ActivityLog.created_at <= parse_date_to(date_to))
    cat = _CATEGORIES.get(category or "")
    if cat:
        if cat.get("like"):
            q = q.filter(or_(*(ActivityLog.action.like(p) for p in cat["like"])))
        if cat.get("actor") == "user":
            q = q.filter(ActivityLog.user_id.isnot(None))
        elif cat.get("actor") == "system":
            q = q.filter(ActivityLog.user_id.is_(None))

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
        "summary": _summarize(r, r.user.email if r.user else None),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
    return {"data": data, "total": total, "page": page, "page_size": page_size}


@router.get("/users")
def activity_users(
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("activity_log:view")),
):
    """Distinct user emails that appear in the audit trail — for the UI filter."""
    rows = (
        db.query(User.email)
        .join(ActivityLog, ActivityLog.user_id == User.id)
        .distinct().order_by(User.email).all()
    )
    return {"users": [r[0] for r in rows]}
