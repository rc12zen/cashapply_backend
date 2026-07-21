"""app.bff.filters_routes — /api/filters/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import AnalysisRun, LineItem
from ..deps import get_db

router = APIRouter()


@router.get("/options")
def get_filter_options(run_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(LineItem)
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    banks = sorted({r.bank_name for r in q.with_entities(LineItem.bank_name).distinct() if r.bank_name})
    bus = sorted({r.business_unit for r in q.with_entities(LineItem.business_unit).distinct() if r.business_unit})

    # `users` — the dashboard's "User" filter (feeds getMetrics' run_by
    # param). PATCH: previously sourced from RowStatusHistory.triggered_by
    # (who approved/rejected a row), which only exists once a human has
    # actually done HITL work on a run — so the dropdown stayed empty for
    # every run until someone approved/rejected something in it. Simplified
    # to AnalysisRun.triggered_by (who STARTED the run, set at
    # /api/run/start — see bff/run_routes.py) since that's known the
    # instant a run exists, no waiting required. Matches the same field
    # Analysis History's own "Started By" filter uses.
    uq = db.query(AnalysisRun.triggered_by).filter(AnalysisRun.triggered_by.isnot(None))
    if run_id:
        uq = uq.filter(AnalysisRun.run_id == run_id)
    users = sorted({u for (u,) in uq.distinct() if u})

    return {"banks": banks, "business_units": bus, "users": users}