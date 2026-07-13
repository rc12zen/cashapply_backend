"""app.bff.filters_routes — /api/filters/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory, User
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
    # param). RowStatusHistory.triggered_by is the acting user's email,
    # recorded on every spoc_approve/spoc_reject transition (hitl/service.py)
    # — but that same table is ALSO written by the rule engine's own
    # automatic categorization (orchestrator.py: apply_transition(...,
    # trigger="rule_engine")), which has no human actor and defaults
    # triggered_by to the literal string "system" (see
    # rule_engine/state_machine.py's apply_transition signature). A plain
    # `SELECT DISTINCT triggered_by` therefore surfaced "system" as if it
    # were a real user in the dropdown. Joining against the actual `users`
    # table by email filters that out (and anything else that isn't a real
    # registered user, e.g. hitl/service.py's hardcoded "spoc_ui" for
    # retry_oracle_post — a separate known gap where the real acting user
    # isn't threaded through to that call at all).
    uq = (
        db.query(RowStatusHistory.triggered_by)
        .join(User, User.email == RowStatusHistory.triggered_by)
    )
    if run_id:
        uq = uq.join(LineItem, LineItem.id == RowStatusHistory.line_item_id).filter(LineItem.run_id == run_id)
    users = sorted({u for (u,) in uq.distinct() if u})

    return {"banks": banks, "business_units": bus, "users": users}