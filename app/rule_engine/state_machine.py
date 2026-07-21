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

# Category string (from rule_engine.RuleResult) -> RowState value, and the
# legacy frontend `status` string the existing tabs expect.
CATEGORY_TO_STATE = {
    "unidentified":             ("unidentified", "Not Found"),
    "needs_remittance":         ("review_approve", "Review & Approve"),
    "conflict_exception":       ("review_approve", "Review & Approve"),
    "acceptable_short_payment": ("review_approve", "Review & Approve"),
    "ready_to_post":            ("review_approve", "Review & Approve"),
}


def apply_transition(db: Session, line_item: LineItem, rule_result, trigger: str,
                      triggered_by: str = "system") -> None:
    from_state = line_item.current_state.value if line_item.current_state else None

    state_value, legacy_status = CATEGORY_TO_STATE.get(rule_result.category, ("conflict_exception", "Review & Approve"))

    line_item.current_state = state_value
    line_item.reason_code = rule_result.reason_code
    line_item.rule_id = rule_result.rule_id
    line_item.status = legacy_status
    line_item.is_matched = rule_result.category != "unidentified"
    line_item.passed_validation = rule_result.category in ("ready_to_post", "acceptable_short_payment")
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

    db.add(RowStatusHistory(
        line_item_id=line_item.id,
        from_state=from_state,
        to_state=state_value,
        trigger=trigger,
        rule_id=rule_result.rule_id,
        triggered_by=triggered_by,
        comment=rule_result.reason_code,
    ))