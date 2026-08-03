"""
app.rule_engine.state_machine
================================
Thin wrapper that logs every state transition to row_status_history,
per the agreed state-machine design (re-evaluation triggers, audit trail).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory
from .invoice_ledger import check_duplicate, record_application

# Category string (from rule_engine.RuleResult) -> RowState value, and the
# legacy frontend `status` string the existing tabs expect.
CATEGORY_TO_STATE = {
    "unidentified":             ("unidentified", "Not Found"),
    "needs_remittance":         ("review_approve", "Review & Approve"),
    "conflict_exception":       ("review_approve", "Review & Approve"),
    "acceptable_short_payment": ("review_approve", "Review & Approve"),
    "ready_to_post":            ("review_approve", "Review & Approve"),
    # NEW: R16/R17/R18 (card / cheque / third-party settlement identity) —
    # own distinct current_state so it never collapses into the generic
    # "Review & Approve" bucket other conflict/needs_remittance rows share.
    # No invoice mapping exists yet at this point (see evaluator.py's
    # docstring on R16-R18) — a SPOC must fill in the Split & Map breakdown
    # before this row can move anywhere else.
    "needs_distribution":       ("needs_distribution", "Needs Distribution"),
}


def apply_transition(db: Session, line_item: LineItem, rule_result, trigger: str,
                      triggered_by: str = "system") -> None:
    from_state = line_item.current_state.value if line_item.current_state else None

    category = rule_result.category
    reason_code = rule_result.reason_code
    rule_id = rule_result.rule_id

    # PATCH: duplicate-invoice protection for the AUTOMATIC path too — see
    # rule_engine/invoice_ledger.py's module docstring. Only relevant when
    # this result would actually consume an invoice (ready_to_post /
    # acceptable_short_payment); every other category either has no
    # matched_invoices or is already headed to conflict/needs_remittance,
    # where nothing is being claimed yet. Any duplicate hit overrides
    # category AND rule_id to a dedicated "R19" — not just reason_code —
    # so bff/metrics.py's rule_id-keyed dashboard grouping doesn't keep
    # bucketing this row as Ready for Oracle underneath the override.
    if category in ("ready_to_post", "acceptable_short_payment") and rule_result.matched_invoices:
        for m in rule_result.matched_invoices:
            dup = check_duplicate(
                db, m.invoice_number, m.ou_number,
                outstanding_amount=m.outstanding_amount,
                new_amount=m.stated_amount if m.stated_amount is not None else m.outstanding_amount,
                exclude_line_item_id=line_item.id,
            )
            if dup["blocked"]:
                category = "conflict_exception"
                reason_code = "INVOICE_ALREADY_APPLIED"
                rule_id = "R19"
                break

    state_value, legacy_status = CATEGORY_TO_STATE.get(category, ("conflict_exception", "Review & Approve"))

    line_item.current_state = state_value
    line_item.reason_code = reason_code
    line_item.rule_id = rule_id
    line_item.status = legacy_status
    line_item.is_matched = category != "unidentified"
    line_item.passed_validation = category in ("ready_to_post", "acceptable_short_payment")
    line_item.target_total = rule_result.target_total
    line_item.shortfall_pct = rule_result.shortfall_pct
    # PATCH: these two were computed on RuleResult (evaluator.py) but never
    # actually copied onto the LineItem here -- is_cross_ou_currency stayed
    # permanently False in the DB regardless of what the rule engine
    # decided (the frontend worked around this by deriving cross-OU status
    # from reason_code/WRONG_OU_* instead — see analysis-history/row/[id]/
    # page.tsx's `isCrossOU` fallback). Fixed while wiring up ou_evidence,
    # since both describe the same decision and belong together.
    line_item.is_cross_ou_currency = bool(rule_result.is_cross_ou_currency)
    line_item.ou_evidence = rule_result.ou_evidence
    # Only ever set by R16/R17/R18 — left untouched (None) for every other
    # rule, since a row that WAS tagged card/cheque/third-party and then
    # re-evaluated (e.g. via remittance recheck) must keep that identity;
    # see remittance_recheck.py's own settlement_type handling for why it
    # never tries to re-derive this itself.
    if rule_result.settlement_type is not None:
        line_item.settlement_type = rule_result.settlement_type
        line_item.settlement_provider = rule_result.settlement_provider
    line_item.matched_invoices = [
        {
            "invoice_number": m.invoice_number,
            "outstanding_amount": m.outstanding_amount,
            "customer_name": m.customer_name,
            "ou_number": m.ou_number,
            "invoice_currency": m.invoice_currency,
            "customer_number": m.customer_number,
            "stated_amount": m.stated_amount,
            "deduction_amount": m.deduction_amount,
        } for m in rule_result.matched_invoices
    ]
    line_item.updated_at = dt.datetime.utcnow()

    # PATCH: register automatic matches in the ledger too (status="pending"
    # — the same status manual mapping uses; upgraded to "confirmed" at
    # Approve by hitl/service.py). Only for the two categories that
    # actually consume an invoice — see the duplicate-check block above,
    # which already ran against the SAME condition.
    if category in ("ready_to_post", "acceptable_short_payment") and line_item.matched_invoices:
        record_application(db, line_item, status="pending")

    db.add(RowStatusHistory(
        line_item_id=line_item.id,
        from_state=from_state,
        to_state=state_value,
        trigger=trigger,
        rule_id=rule_id,
        triggered_by=triggered_by,
        comment=reason_code,
    ))