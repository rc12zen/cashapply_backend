"""app.bff.filters_routes — /api/filters/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory
from ..deps import get_db

router = APIRouter()


@router.get("/options")
def get_filter_options(run_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(LineItem)
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    banks = sorted({r.bank_name for r in q.with_entities(LineItem.bank_name).distinct() if r.bank_name})
    bus = sorted({r.business_unit for r in q.with_entities(LineItem.business_unit).distinct() if r.business_unit})

    # `users` — the dashboard's "User" filter (feeds getMetrics' approved_by
    # param). Previously hardcoded to [] regardless of run_id, so this
    # dropdown always had only "All Users" and the filter could never
    # actually be exercised. RowStatusHistory.triggered_by is the acting
    # user's email, recorded on every spoc_approve/spoc_reject transition
    # (see hitl/service.py) — that's the only place "who acted on this row"
    # is persisted, since LineItem itself has no approved_by column.
    uq = db.query(RowStatusHistory.triggered_by).filter(RowStatusHistory.triggered_by.isnot(None))
    if run_id:
        uq = uq.join(LineItem, LineItem.id == RowStatusHistory.line_item_id).filter(LineItem.run_id == run_id)
    users = sorted({u for (u,) in uq.distinct() if u})

    return {"banks": banks, "business_units": bus, "users": users}
