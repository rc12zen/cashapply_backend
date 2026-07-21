"""
app.schemas.chunk
==================
CONTRACT: Phase 1 (bank_statement) → Phase 2 (extraction)

CreditRowSchema  — one credit row parsed from a bank statement.
ChunkPayloadSchema — a batch of CreditRowSchema items dispatched as a
                     single parallel unit to the extraction pipeline.

Team boundary: bank_statement/ layer produces these; extraction/ layer
consumes them. Neither side imports from the other's internal modules.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field


class CreditRowSchema(BaseModel):
    """
    One credit-only row extracted from a bank statement.
    All fields are exactly what the bank statement parser can reliably populate;
    downstream layers must treat optional fields as potentially None.
    """
    # ── Identity ─────────────────────────────────────────────────────────────
    run_id: int = Field(..., description="Parent AnalysisRun.run_id")
    source_filename: str = Field(..., description="Original uploaded filename")
    row_index: int = Field(..., description="0-based row position in the source file")

    # ── Bank statement fields ─────────────────────────────────────────────────
    bank_name: str
    bank_config_key: str = Field(..., description="e.g. SCB_GBP — matched by bank_statement/detector")
    account_number: Optional[str] = None
    business_unit: Optional[str] = None
    ou_number: Optional[str] = None
    # Full set of Business Unit OU numbers this bank account currently
    # belongs to (primary + any additional -- see db/models.py's
    # BankAccount.all_ou_numbers), resolved FRESH at run start time (see
    # orchestrator.py). None for the legacy direct-parse fallback path,
    # where it falls back to just [ou_number]. Used by
    # rule_engine/ou_resolver.py so a multi-BU account's cross-OU check
    # considers ALL of its linked Business Units, not just one.
    bank_ou_numbers: Optional[list[str]] = None
    statement_date: Optional[dt.datetime] = None
    narrative: Optional[str] = Field(None, description="Raw memo / description text from bank")
    credit_amount: float
    currency: str
    bank_reference: Optional[str] = None
    customer_reference_number: Optional[str] = None

    # ── Ingestion-layer link (additive) ────────────────────────────────────────
    # Set when this row was sourced from the durable, hash-deduplicated
    # StatementTransactionRow ledger (app.db.models) rather than a fresh
    # re-parse of the file. Lets the orchestrator stamp consumed_by_run_id on
    # the correct row after a LineItem is created from it. None for any row
    # still going through the legacy direct-parse fallback path.
    statement_row_id: Optional[int] = None

    class Config:
        json_encoders = {dt.datetime: lambda v: v.isoformat()}


class ChunkPayloadSchema(BaseModel):
    """
    A batch of CreditRowSchema items sent as one parallel work unit
    from chunk_processor → extraction pipeline.

    chunk_index + total_chunks lets the orchestrator track progress
    and detect stalled chunks without polling each row individually.
    """
    chunk_id: str = Field(..., description="UUID assigned by chunk_processor")
    run_id: int
    chunk_index: int = Field(..., description="0-based position within the run's chunk list")
    total_chunks: int = Field(..., description="Total chunks dispatched for this run")
    rows: list[CreditRowSchema]
