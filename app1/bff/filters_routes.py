"""app.bff.filters_routes — /api/filters/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import LineItem
from ..deps import get_db

router = APIRouter()


@router.get("/options")
def get_filter_options(run_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(LineItem)
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    banks = sorted({r.bank_name for r in q.with_entities(LineItem.bank_name).distinct() if r.bank_name})
    bus = sorted({r.business_unit for r in q.with_entities(LineItem.business_unit).distinct() if r.business_unit})
    return {"banks": banks, "business_units": bus, "users": []}
