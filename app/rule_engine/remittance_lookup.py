"""
app.rule_engine.remittance_lookup
====================================
App1 queries the shared remittance_extractions / remittance_invoice_lines
tables (owned/populated by App2) to find a remittance matching a given
bank credit row. Matching strategy: amount + currency + date proximity +
fuzzy customer name — since App2 never knows about bank rows, App1 does
the join here.
"""
from __future__ import annotations

import datetime as dt

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from ..db.models import RemittanceExtraction, RemittanceInvoiceLine

AMOUNT_TOLERANCE_PCT = 2.0   # remittance payment_amount vs bank credit_amount
DATE_WINDOW_DAYS = 10
CUSTOMER_FUZZY_MIN = 50.0


def find_matching_remittances(db: Session, credit_amount: float, currency: str,
                               statement_date: dt.datetime | None,
                               narrative: str) -> list[RemittanceExtraction]:
    q = db.query(RemittanceExtraction)
    if currency:
        q = q.filter(RemittanceExtraction.payment_currency == currency)

    candidates = []
    for ext in q.all():
        if ext.payment_amount is None:
            continue
        amt = float(ext.payment_amount)
        if amt == 0:
            continue
        variance_pct = abs(amt - credit_amount) / amt * 100
        if variance_pct > AMOUNT_TOLERANCE_PCT:
            continue
        if statement_date and ext.payment_date:
            if abs((statement_date - ext.payment_date).days) > DATE_WINDOW_DAYS:
                continue
        candidates.append(ext)
    return candidates


def build_remittance_view(db: Session, line_item, aging_customer_hint: str | None) -> dict:
    """
    Returns the `remittance` sub-dict expected by cashapply_shared.rule_engine.
    """
    matches = find_matching_remittances(
        db, float(line_item.credit_amount), line_item.statement_currency,
        line_item.statement_date, line_item.narrative,
    )

    if not matches:
        return {"found": False, "invoices": [], "ambiguous": False}

    if len(matches) > 1:
        return {"found": True, "invoices": [], "ambiguous": True}

    ext = matches[0]
    lines = db.query(RemittanceInvoiceLine).filter(RemittanceInvoiceLine.extraction_id == ext.id).all()
    invoices = [{
        "invoice_number": l.invoice_number,
        "amount_paid": float(l.amount_paid) if l.amount_paid is not None else None,
        "amount_withheld": float(l.amount_withheld) if l.amount_withheld is not None else None,
        "document_amount": float(l.document_amount) if l.document_amount is not None else None,
        # NEW — only populated when ext.document_type is "card_breakdown" /
        # "cheque_scan" (see agent's claude_extractor.py + shared-column
        # note on RemittanceInvoiceLine in db/models.py). None for every
        # ordinary remittance line.
        "customer_name": l.customer_name,
    } for l in lines]

    customer_conflicts = False
    if aging_customer_hint and ext.raw_customer_text:
        score = fuzz.token_sort_ratio(ext.raw_customer_text.upper(), aging_customer_hint.upper())
        customer_conflicts = score < CUSTOMER_FUZZY_MIN

    # NEW: this used to be a hardcoded False with a "future grouping step"
    # comment -- now real, now that App2 can actually tell us which
    # customer each line belongs to. Still harmless for the settlement
    # rows this was written for: a card_breakdown/cheque_scan bank row is
    # tagged by R16/R17/R18 (see rule_engine/evaluator.py) BEFORE this
    # remittance/R6 path is ever reached, so multiple_customers=True here
    # never actually fires R6 for those rows. It's exposed for the
    # upcoming Split & Map screen, which needs exactly this per-line
    # customer breakdown, not for R6 to act on today.
    distinct_customers = {l.customer_name for l in lines if l.customer_name}
    multiple_customers = ext.document_type in ("card_breakdown", "cheque_scan") and len(distinct_customers) > 1

    return {
        "found": True,
        "invoices": invoices,
        "ambiguous": False,
        "customer_conflicts_with_aging": customer_conflicts,
        "multiple_customers": multiple_customers,
        "payer_explains_overpayment": False,
        "extraction_id": ext.id,
        "raw_customer_text": ext.raw_customer_text,
        "document_type": ext.document_type or "customer_remittance",
    }
