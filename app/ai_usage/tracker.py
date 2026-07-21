"""
app.ai_usage.tracker
======================
Records AI token consumption + cost for every AI call made during Layer 2B
extraction (see extraction/ai_providers.py / extraction/layer_2b_ai.py).

Opens its own short-lived DB session rather than requiring callers to
thread a `db` session through several layers of extraction/chunk-processor
code that don't currently have one in scope (same self-contained pattern
used by app.aging.aging_store for the same reason). Failures here are
swallowed (logged, not raised) — a usage-logging hiccup should never take
down an actual extraction call.

Model + per-token rates are read from Settings at the moment of the call,
and BOTH the rate actually applied AND which provider made the call are
stored on the row itself, so historical totals stay correct even if
pricing changes later or the AI_PROVIDER switch is flipped mid-history —
each row keeps whatever was true when it actually happened.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def record_usage(
    run_id: int | None,
    call_type: str,              # "batch" | "single"
    input_tokens: int,
    output_tokens: int,
    latency_ms: int | None = None,
    batch_ref: str | None = None,
    succeeded: bool = True,
    model: str | None = None,     # defaults to Settings' current provider's model if omitted
    provider: str | None = None,  # "anthropic" | "openai" -- defaults to Settings.AI_PROVIDER if omitted
) -> None:
    try:
        from ..db.session import get_session_factory
        from ..db.models import AiUsageLog
        from ..db.settings import get_settings

        s = get_settings()
        resolved_provider = (provider or s.AI_PROVIDER or "anthropic").strip().lower()

        if resolved_provider in ("openai", "azure_openai"):
            resolved_model = model or (s.AZURE_OPENAI_DEPLOYMENT if resolved_provider == "azure_openai" else s.OPENAI_MODEL)
            cost_per_input = s.OPENAI_COST_PER_INPUT_TOKEN
            cost_per_output = s.OPENAI_COST_PER_OUTPUT_TOKEN
        else:
            resolved_model = model or s.CLAUDE_MODEL
            cost_per_input = s.AI_COST_PER_INPUT_TOKEN
            cost_per_output = s.AI_COST_PER_OUTPUT_TOKEN

        cost = round(
            (input_tokens or 0) * cost_per_input
            + (output_tokens or 0) * cost_per_output,
            6,
        )

        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            db.add(AiUsageLog(
                run_id=run_id,
                call_type=call_type,
                batch_ref=batch_ref,
                model=resolved_model,
                input_tokens=input_tokens or 0,
                output_tokens=output_tokens or 0,
                cost_per_input_token=cost_per_input,
                cost_per_output_token=cost_per_output,
                cost_usd=cost,
                latency_ms=latency_ms,
                succeeded=succeeded,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        # Never let usage-logging failures break an actual extraction call.
        logger.warning("Failed to record AI usage log", exc_info=True)