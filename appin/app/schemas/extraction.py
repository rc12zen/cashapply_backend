"""
app.schemas.extraction
=======================
CONTRACT: Phase 2 (extraction/merger) → Phase 3 (rule_engine)

IdentifiedPayment  — a row where at least one invoice or customer was confirmed.
UnknownPayment     — a row where neither Layer 2A nor Layer 2B found anything.
ExtractionResultSchema — the merged final output of the full extraction phase,
                          consumed by the rule engine orchestrator.

Team boundary: merger.py produces ExtractionResultSchema;
               rule_engine/orchestrator.py consumes it.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .chunk import CreditRowSchema


class IdentifiedPayment(BaseModel):
    """
    A credit row where the extraction pipeline confirmed at least one
    invoice number or customer name against the AgingMap.

    The rule engine uses confirmed_invoice_numbers + customer_name as its
    primary inputs. All other fields are for audit/traceability.
    """
    original: CreditRowSchema

    # ── What was confirmed ────────────────────────────────────────────────────
    confirmed_invoice_numbers: list[str] = Field(
        default_factory=list,
        description="Invoice numbers confirmed against AgingMap (exact match)",
    )
    customer_name: Optional[str] = Field(
        None,
        description="Best-matched customer name from AgingMap",
    )
    customer_match_pct: float = Field(0.0)
    extraction_method: str = Field(
        ...,
        description="regex | fuzzy | regex+fuzzy | ai | ai+aging_validated",
    )
    confidence_score: Optional[float] = Field(
        None,
        description="0–1 confidence from AI layer; None if regex-only",
    )

    # ── Traceability ──────────────────────────────────────────────────────────
    identified_by_layer: str = Field(
        ...,
        description="2a | 2b — which layer made the final identification",
    )
    ai_raw_response: Optional[str] = Field(
        None,
        description="Raw AI model output, stored for audit. None if Layer 2A was sufficient.",
    )


class UnknownPayment(BaseModel):
    """
    A credit row where neither Layer 2A nor Layer 2B could confirm any
    invoice or customer. Routes to R8 (NO_SIGNAL) in the rule engine.
    """
    original: CreditRowSchema

    # ── Why it stayed unknown ─────────────────────────────────────────────────
    regex_candidates_tried: list[str] = Field(
        default_factory=list,
        description="Invoice-like strings regex found but AgingMap rejected",
    )
    ai_attempted: bool = Field(
        False,
        description="True if Layer 2B was invoked (even if it found nothing)",
    )
    ai_raw_response: Optional[str] = Field(
        None,
        description="AI output even on failure — kept for manual review",
    )
    failure_reason: Optional[str] = Field(
        None,
        description="Human-readable reason: no_regex_candidates | aging_rejected | ai_no_output | ai_validation_failed",
    )


class ExtractionResultSchema(BaseModel):
    """
    Final output of the complete extraction phase (2A + 2B merged).
    This is the data contract the rule engine orchestrator receives.

    identified_payments → routed through R0–R14 rule evaluation
    unknown_payments    → directly assigned R8 (NO_SIGNAL / unidentified state)
    """
    chunk_id: str
    run_id: int
    chunk_index: int
    total_chunks: int

    identified_payments: list[IdentifiedPayment] = Field(default_factory=list)
    unknown_payments: list[UnknownPayment] = Field(default_factory=list)

    # ── Chunk-level stats (for progress tracking, not business logic) ─────────
    total_rows_in_chunk: int
    identified_count: int
    unknown_count: int
    layer_2a_hit_count: int = Field(0, description="Rows resolved by regex alone")
    layer_2b_hit_count: int = Field(0, description="Rows resolved by AI fallback")
