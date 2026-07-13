"""app.bff.results_routes — /api/results/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..deps import get_db
from .metrics import compute_metrics, compute_run_summary
from .row_detail import build_row_detail
from .shortage import compute_shortage_summary

router = APIRouter()


@router.get("/metrics")
def get_metrics(run_id: int | None = None, date_from: str | None = None,
                 date_to: str | None = None, db: Session = Depends(get_db)):
    return compute_metrics(db, run_id=run_id, date_from=date_from, date_to=date_to)


@router.get("/run-summary/{run_id}")
def get_run_summary(run_id: int, db: Session = Depends(get_db)):
    return compute_run_summary(db, run_id)


@router.get("/row-detail/{record_id}")
def get_row_detail(record_id: int, db: Session = Depends(get_db)):
    return build_row_detail(db, record_id)


@router.get("/not-found")
def get_not_found(db: Session = Depends(get_db)):
    from .metrics import get_unidentified_rows
    return get_unidentified_rows(db)


@router.get("/validation-failures")
def get_validation_failures(db: Session = Depends(get_db)):
    from .metrics import get_conflict_rows
    return get_conflict_rows(db)


@router.get("/processed-shortage-summary")
def get_processed_shortage_summary(
    run_id: int | None = None, date_from: str | None = None,
    date_to: str | None = None, db: Session = Depends(get_db),
):
    return compute_shortage_summary(db, run_id=run_id, date_from=date_from, date_to=date_to)
