"""
app.ai_usage.service
=======================
Aggregation over AiUsageLog — backs GET /api/ai-usage/summary, which feeds
the dashboard's "AI Run Details" panel (previously entirely hardcoded
placeholder values — see app/dashboard/page.tsx).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import AiUsageLog
from ..db.settings import get_settings


def get_usage_summary(db: Session, run_id: int | None = None) -> dict:
    q = db.query(AiUsageLog)
    if run_id:
        q = q.filter(AiUsageLog.run_id == run_id)
    rows = q.all()

    s = get_settings()
    call_count = len(rows)
    total_input = sum(r.input_tokens for r in rows)
    total_output = sum(r.output_tokens for r in rows)
    total_cost = sum(r.cost_usd for r in rows)
    latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
    avg_latency_ms = round(sum(latencies) / len(latencies), 1) if latencies else None
    failed_calls = sum(1 for r in rows if not r.succeeded)

    # Model + rates: prefer what was actually recorded (accurate even if
    # pricing/model has since changed in .env); fall back to current
    # Settings when there's no usage yet for this scope (e.g. a run that
    # hasn't needed any AI fallback calls — Layer 2A regex resolved
    # everything).
    model = rows[0].model if rows else s.CLAUDE_MODEL
    cost_per_input = rows[0].cost_per_input_token if rows else s.AI_COST_PER_INPUT_TOKEN
    cost_per_output = rows[0].cost_per_output_token if rows else s.AI_COST_PER_OUTPUT_TOKEN

    return {
        "model": model,
        "call_count": call_count,
        "failed_calls": failed_calls,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_cost_usd": round(total_cost, 4),
        "avg_latency_ms": avg_latency_ms,
        "cost_per_input_token": cost_per_input,
        "cost_per_output_token": cost_per_output,
    }