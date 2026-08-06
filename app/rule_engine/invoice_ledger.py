"""
app.rule_engine.invoice_ledger
=================================
The real implementation behind "has this invoice already been applied
somewhere else" — replacing rule_engine/evaluator.py's old
already_processed_match stub (permanently False, never actually wired to
anything) and the aging snapshot, which has zero memory of what's been
consumed against an invoice since it was loaded.

Used by:
  - hitl/manual_mapping.py   — checked on every preview/confirm, for BOTH
                                 the exact-match/tolerance case and the new
                                 short-payment-beyond-tolerance case.
  - rule_engine/state_machine.py — checked (and recorded) for automatic
                                 R9a/R9b/R9d matches too, so the ledger has
                                 full visibility, not just manually-mapped
                                 rows.
  - hitl/service.py           — confirm_applications() on approve,
                                 release_applications() on reject.

Design: sum of "active" (pending + confirmed) applied_amount per invoice
must never exceed that invoice's outstanding_amount by more than a small
rounding tolerance. This allows the legitimate case (a short payment now,
a second payment covering the remainder later) while blocking the
duplicate case (the same money mapped to the same invoice twice).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import InvoiceApplication

ACTIVE_STATUSES = ("pending", "confirmed")
DUPLICATE_TOLERANCE = 0.01   # currency-unit rounding slack, not a business tolerance


def get_applied_total(
    db: Session, invoice_number: str, ou_number: str | None = None,
    exclude_line_item_id: int | None = None,
) -> float:
    """Sum of everything currently 'claiming' this invoice (pending or
    confirmed), across every OTHER row. Excludes the row currently being
    (re)previewed so a SPOC re-checking their own in-progress mapping
    doesn't see it flagged as a duplicate of itself."""
    q = db.query(InvoiceApplication).filter(
        InvoiceApplication.invoice_number == invoice_number,
        InvoiceApplication.status.in_(ACTIVE_STATUSES),
    )
    if ou_number:
        q = q.filter(InvoiceApplication.ou_number == ou_number)
    if exclude_line_item_id is not None:
        q = q.filter(InvoiceApplication.line_item_id != exclude_line_item_id)
    return sum(float(a.applied_amount or 0) for a in q.all())


def check_duplicate(
    db: Session, invoice_number: str, ou_number: str | None,
    outstanding_amount: float, new_amount: float,
    exclude_line_item_id: int | None = None,
) -> dict:
    """Returns {"blocked", "already_applied", "remaining_before", "message"}.
    `blocked` is True when new_amount would push the invoice's total active
    applications past its outstanding_amount — i.e. this selection would
    double up on money another row already claimed."""
    already_applied = get_applied_total(db, invoice_number, ou_number, exclude_line_item_id)
    remaining_before = round(float(outstanding_amount) - already_applied, 2)

    if new_amount > remaining_before + DUPLICATE_TOLERANCE:
        return {
            "blocked": True,
            "already_applied": round(already_applied, 2),
            "remaining_before": remaining_before,
            "message": (
                f"Invoice {invoice_number} already has {already_applied:,.2f} applied from "
                f"another payment — only {max(remaining_before, 0):,.2f} of it remains available, "
                f"but this selection would apply {new_amount:,.2f}. This looks like the same "
                f"invoice being mapped twice; if the earlier mapping was a mistake, release it "
                f"first (reject that row) rather than mapping over it here."
            ),
        }
    return {
        "blocked": False,
        "already_applied": round(already_applied, 2),
        "remaining_before": remaining_before,
        "message": None,
    }


def record_application(db: Session, line_item, status: str = "pending") -> None:
    """Upserts one InvoiceApplication row per entry in
    line_item.matched_invoices. Idempotent per (line_item_id,
    invoice_number) — re-mapping the SAME row updates its own rows rather
    than creating duplicates (see the UniqueConstraint on the model)."""
    if not line_item.matched_invoices:
        return
    for inv in line_item.matched_invoices:
        invoice_number = inv.get("invoice_number")
        if not invoice_number:
            continue
        amount = inv.get("stated_amount")
        if amount is None:
            amount = inv.get("outstanding_amount") or 0
        existing = (
            db.query(InvoiceApplication)
            .filter(
                InvoiceApplication.line_item_id == line_item.id,
                InvoiceApplication.invoice_number == invoice_number,
            )
            .first()
        )
        if existing:
            existing.applied_amount = amount
            existing.status = status
            existing.customer_name = inv.get("customer_name")
            existing.ou_number = inv.get("ou_number")
            existing.invoice_currency = inv.get("invoice_currency")
            existing.updated_at = dt.datetime.utcnow()
        else:
            db.add(InvoiceApplication(
                line_item_id=line_item.id,
                invoice_number=invoice_number,
                ou_number=inv.get("ou_number"),
                customer_name=inv.get("customer_name"),
                applied_amount=amount,
                invoice_currency=inv.get("invoice_currency"),
                status=status,
            ))
    db.flush()


def record_application_for_entry(
    db: Session, parent_line_item_id: int, entry_id: str,
    invoice_number: str, ou_number: str | None, customer_name: str | None,
    applied_amount: float, invoice_currency: str | None, status: str = "pending",
) -> None:
    """Same idea as record_application(), for ONE entry inside a
    'distributed' parent's distribution_breakdown -- see
    LineItem.distribution_breakdown / InvoiceApplication.distribution_entry_id.
    Multiple entries share the same parent_line_item_id, so lookups here
    are keyed on (line_item_id, invoice_number, distribution_entry_id), not
    just (line_item_id, invoice_number) -- otherwise a second entry
    referencing a different invoice under the same parent would be fine,
    but two entries that happened to reference the SAME invoice_number
    (rare -- would mean the aging report itself has that number attached
    to more than one customer) could overwrite each other's row. Documented
    as a known edge case, not silently corrected."""
    if not invoice_number:
        return
    existing = (
        db.query(InvoiceApplication)
        .filter(
            InvoiceApplication.line_item_id == parent_line_item_id,
            InvoiceApplication.invoice_number == invoice_number,
            InvoiceApplication.distribution_entry_id == entry_id,
        )
        .first()
    )
    if existing:
        existing.applied_amount = applied_amount
        existing.status = status
        existing.customer_name = customer_name
        existing.ou_number = ou_number
        existing.invoice_currency = invoice_currency
        existing.updated_at = dt.datetime.utcnow()
    else:
        db.add(InvoiceApplication(
            line_item_id=parent_line_item_id,
            invoice_number=invoice_number,
            ou_number=ou_number,
            customer_name=customer_name,
            applied_amount=applied_amount,
            invoice_currency=invoice_currency,
            status=status,
            distribution_entry_id=entry_id,
        ))
    db.flush()


def confirm_application_for_entry(db: Session, parent_line_item_id: int, entry_id: str) -> None:
    """Called once THIS entry's Oracle invoice-mapping call actually
    succeeds -- upgrades only its own InvoiceApplication row(s), leaving
    every sibling entry under the same parent untouched."""
    db.query(InvoiceApplication).filter(
        InvoiceApplication.line_item_id == parent_line_item_id,
        InvoiceApplication.distribution_entry_id == entry_id,
    ).update({"status": "confirmed", "updated_at": dt.datetime.utcnow()})
    db.flush()


def release_application_for_entry(db: Session, parent_line_item_id: int, entry_id: str) -> None:
    """Called when ONE entry is rejected -- frees just that entry's
    invoice claim, leaving every sibling entry under the same parent
    untouched (unlike release_applications(), which releases every
    InvoiceApplication row for a given line_item_id)."""
    db.query(InvoiceApplication).filter(
        InvoiceApplication.line_item_id == parent_line_item_id,
        InvoiceApplication.distribution_entry_id == entry_id,
    ).update({"status": "released", "updated_at": dt.datetime.utcnow()})
    db.flush()


def confirm_applications(db: Session, line_item) -> None:
    """Called by hitl/service.py's approve_row once the Oracle
    invoice-mapping call actually succeeds."""
    db.query(InvoiceApplication).filter(
        InvoiceApplication.line_item_id == line_item.id,
    ).update({"status": "confirmed", "updated_at": dt.datetime.utcnow()})
    db.flush()


def release_applications(db: Session, line_item) -> None:
    """Called by hitl/service.py's reject_row — frees every invoice this
    row had claimed so a correct mapping (on this row or another) isn't
    blocked as a false 'duplicate' by a decision that's being undone."""
    db.query(InvoiceApplication).filter(
        InvoiceApplication.line_item_id == line_item.id,
    ).update({"status": "released", "updated_at": dt.datetime.utcnow()})
    db.flush()