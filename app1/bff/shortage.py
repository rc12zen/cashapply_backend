"""
app.services.shortage_service
================================
Shortage & Reconciliation Audit — splits Processed rows into shortage
(credit < outstanding within acceptable band) vs full_payment.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import LineItem


def compute_shortage_summary(db: Session, run_id: int | None = None,
                              date_from: str | None = None, date_to: str | None = None) -> dict:
    q = db.query(LineItem).filter(LineItem.oracle_post_status == "success")
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    if date_from:
        q = q.filter(LineItem.statement_date >= date_from)
    if date_to:
        q = q.filter(LineItem.statement_date <= date_to)
    rows = q.all()

    shortage, full_payment = [], []
    for r in rows:
        target = float(r.target_total or 0)
        received = float(r.credit_amount or 0)
        variance = target - received
        ratio_pct = round(received / target * 100, 2) if target else 100.0
        is_full = abs(variance) < 0.01

        entry = {
            "id": r.id,
            "narrative": r.narrative,
            "variance": round(variance, 2),
            "ratio_pct": ratio_pct,
            "is_full_payment": is_full,
            "oracle_ref_no": r.oracle_ref_no,
            "standard_receipt_id": r.standard_receipt_id,
            "applications": [{
                "invoice_number": m["invoice_number"],
                "amount_outstanding": m["outstanding_amount"],
                "amount_applied": m.get("stated_amount") or m["outstanding_amount"],
                "shortage_amount": max(m["outstanding_amount"] - (m.get("stated_amount") or m["outstanding_amount"]), 0),
                "is_full_payment": is_full,
                "status": "applied",
                "application_id": None,
                "error": None,
            } for m in (r.matched_invoices or [])],
        }
        (full_payment if is_full else shortage).append(entry)

    return {"shortage": shortage, "full_payment": full_payment}
