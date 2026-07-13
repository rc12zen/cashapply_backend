"""
app.ai_usage.service
=======================
Aggregation over AiUsageLog — backs GET /api/ai-usage/summary, /totals and
/export, which feed the dashboard's "AI Run Details" panel (previously
entirely hardcoded placeholder values — see app/home/page.tsx).

Scoping note: usage can be scoped by run_id OR by a created_at date range
(the same two axes the dashboard's time pills use — "Last Analysis" => run,
Today/WTD/MTD/Custom => date range). Date filtering is on created_at (when we
processed the call), mirroring bff/metrics.py — see backend CLAUDE.md gotcha
#3 for why created_at, and why date_to needs an end-of-day bump.

There is deliberately no bank/BU/user scoping here: AiUsageLog is tagged by
run_id and time only, so token spend can't be attributed to those filters
without a fragile join through run -> files -> account.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import AiUsageLog
from ..db.settings import get_settings


def _apply_scope(q, run_id: int | None, date_from: str | None, date_to: str | None):
    """Filter an AiUsageLog query by run_id and/or a created_at date range.

    date_from/date_to are 'YYYY-MM-DD' strings from the frontend. The
    end-of-day bump on date_to matches bff/metrics.py exactly: created_at has
    a real time-of-day component, so a bare "<= date_to" would exclude almost
    the entire day (see backend CLAUDE.md gotcha #3).
    """
    if run_id:
        q = q.filter(AiUsageLog.run_id == run_id)
    if date_from:
        # Parse to a real datetime rather than comparing against the bare
        # 'YYYY-MM-DD' string: under psycopg3 a str binds as VARCHAR and
        # Postgres rejects "timestamp >= varchar" (no implicit cast).
        start_of_day = dt.datetime.strptime(date_from, "%Y-%m-%d")
        q = q.filter(AiUsageLog.created_at >= start_of_day)
    if date_to:
        end_of_day = (
            dt.datetime.strptime(date_to, "%Y-%m-%d")
            + dt.timedelta(days=1)
            - dt.timedelta(microseconds=1)
        )
        q = q.filter(AiUsageLog.created_at <= end_of_day)
    return q


def _summarize(rows: list[AiUsageLog]) -> dict:
    """Flat totals over a list of AiUsageLog rows (no model breakdown)."""
    total_input = sum(r.input_tokens for r in rows)
    total_output = sum(r.output_tokens for r in rows)
    total_cost = sum(r.cost_usd for r in rows)
    latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
    return {
        "call_count": len(rows),
        "failed_calls": sum(1 for r in rows if not r.succeeded),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_cost_usd": round(total_cost, 4),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
    }


def _breakdown_by_model(rows: list[AiUsageLog]) -> list[dict]:
    """One aggregated entry per model, most-expensive first."""
    by_model: dict[str, dict] = {}
    for r in rows:
        m = by_model.setdefault(
            r.model,
            {
                "model": r.model,
                "call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        m["call_count"] += 1
        m["input_tokens"] += r.input_tokens
        m["output_tokens"] += r.output_tokens
        m["cost_usd"] += r.cost_usd
    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 4)
    return sorted(by_model.values(), key=lambda m: m["cost_usd"], reverse=True)


def get_usage_summary(
    db: Session,
    run_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    rows = _apply_scope(db.query(AiUsageLog), run_id, date_from, date_to).all()

    s = get_settings()
    summary = _summarize(rows)

    # Model + rates: prefer what was actually recorded (accurate even if
    # pricing/model has since changed in .env); fall back to current
    # Settings when there's no usage yet for this scope (e.g. a run that
    # hasn't needed any AI fallback calls — Layer 2A regex resolved
    # everything).
    summary["model"] = rows[0].model if rows else s.CLAUDE_MODEL
    summary["cost_per_input_token"] = (
        rows[0].cost_per_input_token if rows else s.AI_COST_PER_INPUT_TOKEN
    )
    summary["cost_per_output_token"] = (
        rows[0].cost_per_output_token if rows else s.AI_COST_PER_OUTPUT_TOKEN
    )
    summary["by_model"] = _breakdown_by_model(rows)
    return summary


def get_usage_totals(db: Session) -> dict:
    """
    Global figures, independent of the panel's current run/date scope, for the
    "all-time" and "this month" tiles. Month = current UTC calendar month, to
    match how AiUsageLog.created_at is stamped (tracker uses utcnow()).
    """
    all_rows = db.query(AiUsageLog).all()
    all_input = sum(r.input_tokens for r in all_rows)
    all_output = sum(r.output_tokens for r in all_rows)

    now = dt.datetime.utcnow()
    month_start = dt.datetime(now.year, now.month, 1)
    month_rows = [r for r in all_rows if r.created_at and r.created_at >= month_start]
    month_input = sum(r.input_tokens for r in month_rows)
    month_output = sum(r.output_tokens for r in month_rows)

    return {
        "all_time_cost_usd": round(sum(r.cost_usd for r in all_rows), 4),
        "all_time_tokens": all_input + all_output,
        "all_time_call_count": len(all_rows),
        "month_cost_usd": round(sum(r.cost_usd for r in month_rows), 4),
        "month_tokens": month_input + month_output,
        "month_call_count": len(month_rows),
    }
