"""
app.services.shortage_service
================================
Shortage & Reconciliation Audit — splits Processed rows into shortage
(credit < outstanding within acceptable band) vs full_payment.

PATCH NOTES:
  - Response shape fixed: the frontend (app/shortage-review/page.tsx) reads
    `data.shortage.rows` / `data.full_payment.rows`, but this previously
    returned `{"shortage": [...], "full_payment": [...]}` as bare lists —
    `list.rows` is always undefined in JS, so the table silently showed
    "No records" no matter what was in the database. Now wrapped as
    {"rows": [...]}.
  - Each entry now includes every field the frontend's ProcessedRecord
    type actually reads (bank_name, business_unit, statement_date,
    bank_reference, currency, customer_name, primary_invoice,
    sum_outstanding, total_shortage, oracle_posted_at, run_id) — previously
    only id/narrative/variance/ratio_pct/is_full_payment/oracle_ref_no/
    standard_receipt_id/applications were returned, so the Bank, Date,
    Customer, Invoice(s), Outstanding, Applied, and Posted At columns were
    always blank even once the .rows fix above landed.
  - Added bank_name / business_unit filters (parity with /metrics and
    /executive-summary) and a top-level `total` count, so the Shortage
    Review page's new timeline + bank/BU filters have something real to
    call (previously that page had no filters at all beyond a text box).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import LineItem
from .date_range import parse_date_from, parse_date_to


def compute_shortage_summary(db: Session, run_id: int | None = None,
                              date_from: str | None = None, date_to: str | None = None,
                              bank_name: str | None = None,
                              business_unit: str | None = None) -> dict:
    q = db.query(LineItem).filter(LineItem.oracle_post_status == "success")
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    if date_from:
        # PATCH: was LineItem.statement_date (the bank transaction's own
        # date) — same fix as bff/metrics.py: the timeline pills mean
        # "when did we process this", not "what date is on the statement".
        q = q.filter(LineItem.created_at >= parse_date_from(date_from))
    if date_to:
        q = q.filter(LineItem.created_at <= parse_date_to(date_to))
    if bank_name:
        q = q.filter(LineItem.bank_name == bank_name)
    if business_unit:
        q = q.filter(LineItem.business_unit == business_unit)
    rows = q.all()

    shortage, full_payment = [], []
    for r in rows:
        target = float(r.target_total or 0)
        received = float(r.credit_amount or 0)
        variance = target - received
        ratio_pct = round(received / target * 100, 2) if target else 100.0
        is_full = abs(variance) < 0.01
        total_shortage = max(variance, 0.0)

        matched = r.matched_invoices or []
        primary_invoice = ",".join(m.get("invoice_number", "") for m in matched)
        customer_name = matched[0].get("customer_name") if matched else None

        entry = {
            "id": r.id,
            "run_id": r.run_id,
            "bank_name": r.bank_name,
            "business_unit": r.business_unit,
            "statement_date": r.statement_date.isoformat() if r.statement_date else None,
            "narrative": r.narrative,
            "bank_reference": r.bank_reference,
            "credit_amount": received,
            "currency": r.statement_currency,
            "customer_name": customer_name,
            "primary_invoice": primary_invoice,
            "sum_outstanding": target,
            "variance": round(variance, 2),
            "ratio_pct": ratio_pct,
            "is_full_payment": is_full,
            "total_shortage": round(total_shortage, 2),
            "oracle_ref_no": r.oracle_ref_no,
            "standard_receipt_id": r.standard_receipt_id,
            "oracle_posted_at": r.oracle_posted_at.isoformat() if r.oracle_posted_at else None,
            "applications": [{
                "invoice_number": m["invoice_number"],
                "amount_outstanding": m["outstanding_amount"],
                "amount_applied": m.get("stated_amount") or m["outstanding_amount"],
                "shortage_amount": max(m["outstanding_amount"] - (m.get("stated_amount") or m["outstanding_amount"]), 0),
                "is_full_payment": is_full,
                "status": "applied",
                "application_id": None,
                "error": None,
            } for m in matched],
        }
        (full_payment if is_full else shortage).append(entry)

    return {
        "shortage": {"rows": shortage},
        "full_payment": {"rows": full_payment},
        "total": len(rows),
    }