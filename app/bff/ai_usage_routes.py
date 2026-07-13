"""app.bff.ai_usage_routes — /api/ai-usage/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..ai_usage.service import get_usage_summary
from ..deps import get_db

router = APIRouter()


@router.get("/summary")
def get_ai_usage_summary(run_id: int | None = None, db: Session = Depends(get_db)):
    """
    AI token consumption + cost, optionally scoped to one run. Backs the
    dashboard's "AI Run Details" panel (see app/dashboard/page.tsx) — no
    permission gate here since it's read-only aggregate cost data, same
    tier as /api/results/metrics.
    """
    return get_usage_summary(db, run_id=run_id)