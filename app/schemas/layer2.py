"""
app.schemas.layer2
===================
CONTRACT: Phase 2A (layer_2a_regex) → Phase 2B (layer_2b_ai)

Layer2ARow        — a single row annotated with whatever regex could find.
Layer2AResultSchema — the split output of Layer 2A:
                        found_invoices   → rows where regex matched + AgingMap confirmed
                        no_invoice_found → rows to be escalated to Layer 2B AI

Team boundary: layer_2a_regex.py produces Layer2AResultSchema;
               layer_2b_ai.py consumes it. Neither touches the other's code.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .chunk import CreditRowSchema


class Layer2ARow(BaseModel):
    """
    The original CreditRowSchema enriched with whatever Layer 2A could extract.
    All extraction fields are optional — Layer 2B must treat them as hints only.
    """
    original: CreditRowSchema

    # ── Regex extraction results ──────────────────────────────────────────────
    regex_candidate_invoices: list[str] = Field(
        default_factory=list,
        description="Raw invoice-like strings found by regex before AgingMap check",
    )
    confirmed_invoice_numbers: list[str] = Field(
        default_factory=list,
        description="Subset of regex candidates confirmed 100% in AgingMap",
    )
    customer_fuzzy_match: Optional[str] = Field(
        None,
        description="Best fuzzy customer name match from AgingMap, or None",
    )
    customer_match_pct: float = Field(0.0, description="RapidFuzz score 0–100")
    extraction_method: str = Field("none", description="regex | fuzzy | regex+fuzzy | none")


class Layer2AResultSchema(BaseModel):
    """
    Output of Layer 2A — the split into found vs. needs-AI buckets.

    Layer 2B receives only no_invoice_found and must return them
    re-classified as either IdentifiedPayment or UnknownPayment.
    """
    chunk_id: str
    run_id: int
    chunk_index: int
    total_chunks: int

    found_invoices: list[Layer2ARow] = Field(
        default_factory=list,
        description="Rows where at least one invoice confirmed in AgingMap",
    )
    no_invoice_found: list[Layer2ARow] = Field(
        default_factory=list,
        description="Rows with no confirmed invoice — escalate to Layer 2B",
    )
