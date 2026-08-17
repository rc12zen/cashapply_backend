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

    NOTE: aging_lookup and credit_memos_lookup are callables — not
    serialisable. When passing across process boundaries (future Celery
    workers), serialize the aging snapshot separately and reconstruct the
    lambdas on the worker.

    NOTE: this model is DOCUMENTATION ONLY — it is never instantiated
    anywhere (no constructor call, no model_validate, no parse_obj), so it
    validates nothing at runtime. The two callables below are declared to
    stop it drifting further from the real dict, not because declaring them
    enforces anything. The real contract is evaluate_row()'s own docstring
    plus _require_credit_memos_lookup(), which does check at runtime.
    """
    aging_lookup: Optional[Callable] = Field(
        default=None,
        description="callable(invoice_number, ou_number) -> AgingInvoiceView | None",
    )
    credit_memos_lookup: Optional[Callable] = Field(
        default=None,
        description=(
            "REQUIRED at runtime. callable(customer_number, ou_number, currency) "
            "-> list[CreditMemoView]. Every site building this dict must supply it; "
            "see rule_engine/evaluator.py::_require_credit_memos_lookup for why a "
            "missing value raises rather than defaulting to 'no credit memos'."
        ),
    )
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
    settlement_type: Optional[str] = Field(
        default=None,
        description="'card_narrative' | 'cheque_narrative' | 'third_party_provider' | None — see R16/R17/R18",
    )
    settlement_provider: Optional[str] = Field(
        default=None, description="Matched provider_name — only set when settlement_type == 'third_party_provider'",
    )

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
        description="Maps to RowState: unidentified | needs_remittance | conflict_exception | acceptable_short_payment | ready_to_post | needs_distribution",
    )
    matched_invoices: list[MatchedInvoiceSchema] = Field(default_factory=list)
    target_total: Optional[float] = None
    received_total: Optional[float] = None
    shortfall_pct: Optional[float] = None
    notes: str = ""
    settlement_type: Optional[str] = None
    settlement_provider: Optional[str] = None
