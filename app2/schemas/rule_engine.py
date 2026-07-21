"""
app.schemas.rule_engine
========================
CONTRACT: Phase 3 (rule_engine) input and output types.

RuleEngineInputSchema — what the orchestrator assembles and passes to evaluate_row().
RuleResultSchema      — what evaluate_row() returns, consumed by state_machine and DB writer.

Team boundary: rule_engine/orchestrator.py assembles RuleEngineInputSchema from
               ExtractionResultSchema + remittance lookup + aging lookup.
               rule_engine/evaluator.py accepts and returns these types.
"""
from __future__ import annotations

from typing import Optional, Callable

from pydantic import BaseModel, Field


class RemittanceView(BaseModel):
    """
    Sub-contract: result of remittance_lookup.build_remittance_view().
    Embedded inside RuleEngineInputSchema.
    """
    found: bool
    invoices: list[dict] = Field(default_factory=list)
    ambiguous: bool = False
    customer_conflicts_with_aging: bool = False
    multiple_customers: bool = False
    payer_explains_overpayment: bool = False
    extraction_id: Optional[int] = None
    raw_customer_text: Optional[str] = None


class CrossCurrencyView(BaseModel):
    is_cross_currency: bool = False
    fx_rate: Optional[float] = None


class RuleEngineInputSchema(BaseModel):
    """
    The complete input to evaluate_row(). Built by rule_engine/orchestrator.py
    from ExtractionResultSchema + live DB lookups.

    NOTE: aging_lookup is a callable — not serialisable. When passing
    across process boundaries (future Celery workers), serialize the
    aging snapshot separately and reconstruct the lambda on the worker.
    """
    original_row: dict = Field(
        ...,
        description="Raw bank statement fields: credit_amount, currency, narrative, bank_reference, ou_number",
    )
    extraction: dict = Field(
        ...,
        description="From IdentifiedPayment: extracted_invoices, customer_match_pct, invoice_match_pct, customer_text_match",
    )
    remittance: RemittanceView
    cross_currency: CrossCurrencyView = Field(default_factory=CrossCurrencyView)
    ou_mismatch: bool = False
    duplicate_invoice_across_customers: bool = False
    duplicate_ambiguous: bool = False
    already_processed_match: bool = False

    class Config:
        arbitrary_types_allowed = True   # allows the aging_lookup callable


class MatchedInvoiceSchema(BaseModel):
    invoice_number: str
    outstanding_amount: float
    customer_name: str
    ou_number: str
    stated_amount: Optional[float] = None
    deduction_amount: Optional[float] = None


class RuleResultSchema(BaseModel):
    """
    Output of evaluate_row(). Consumed by:
      - rule_engine/state_machine.py  (to write DB state + audit log)
      - bff/results_routes.py         (serialised to frontend)
    """
    rule_id: str = Field(..., description="R0 | R1 | R9a | R9b | R9c | R11 | R14 …")
    reason_code: str = Field(..., description="EXACT_MATCH | OVERPAYMENT_UNEXPLAINED | NO_SIGNAL …")
    category: str = Field(
        ...,
        description="Maps to RowState: unidentified | needs_remittance | conflict_exception | acceptable_short_payment | ready_to_post",
    )
    matched_invoices: list[MatchedInvoiceSchema] = Field(default_factory=list)
    target_total: Optional[float] = None
    received_total: Optional[float] = None
    shortfall_pct: Optional[float] = None
    notes: str = ""
