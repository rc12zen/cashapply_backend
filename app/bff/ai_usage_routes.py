"""app.bff.ai_usage_routes — /api/ai-usage/*"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..ai_usage.service import get_usage_summary, get_usage_totals, get_recent_runs_usage
from ..db.models import User
from ..deps import get_db
from ..auth import require_permission

router = APIRouter()


@router.get("/summary")
def get_ai_usage_summary(
    run_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run:view")),
):
    """
    AI token consumption + cost, scoped to one run (run_id) OR a created_at
    date range (date_from/date_to, 'YYYY-MM-DD'), optionally further narrowed
    to one user's own runs (user — matches AnalysisRun.triggered_by, same
    convention as /api/results/metrics' run_by filter; ignored when run_id is
    set, same reasoning as that filter). Includes a per-model breakdown.
    Backs the AI Usage page — no permission gate here since it's read-only
    aggregate cost data, same tier as /api/results/metrics.
    """
    return get_usage_summary(db, run_id=run_id, date_from=date_from, date_to=date_to, user=user)


@router.get("/recent")
def get_ai_usage_recent(
    user: str | None = None,
    limit: int = 5,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run:view")),
):
    """
    Per-run AI usage for the last `limit` completed analysis runs, optionally
    narrowed to one user's own runs. Backs the "Last 5 Analyses" component.
    """
    return {"runs": get_recent_runs_usage(db, user=user, limit=limit)}


@router.get("/totals")
def get_ai_usage_totals(db: Session = Depends(get_db),
                         current_user: User = Depends(require_permission("run:view"))):
    """
    Global all-time and current-month token/cost totals, independent of the
    panel's current run/date scope. Backs the "all-time" / "this month" tiles.
    """
    return get_usage_totals(db)


@router.get("/export")
def export_ai_usage_csv(
    run_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run:view")),
):
    """
    CSV export of AI usage aggregated by model, honoring the same run_id /
    date range / user scope as /summary. Read-only cost data — no permission
    gate, matching /summary.
    """
    summary = get_usage_summary(db, run_id=run_id, date_from=date_from, date_to=date_to, user=user)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["model", "call_count", "input_tokens", "output_tokens", "cost_usd"]
    )
    for m in summary["by_model"]:
        writer.writerow(
            [
                m["model"],
                m["call_count"],
                m["input_tokens"],
                m["output_tokens"],
                f'{m["cost_usd"]:.4f}',
            ]
        )
    # Trailing TOTAL row so the file stands alone for finance.
    writer.writerow(
        [
            "TOTAL",
            summary["call_count"],
            summary["total_input_tokens"],
            summary["total_output_tokens"],
            f'{summary["total_cost_usd"]:.4f}',
        ]
    )

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ai-usage.csv"'},
    )