"""
app.hitl.overpayment
=======================
Route B for an overpaid row: record WHY the excess exists and close the row out
WITHOUT posting anything.

Why this exists
---------------
An R11 row used to have exactly one action that could actually complete:
Reject. That was the wrong verb. The money genuinely arrived, Oracle already
holds a receipt for it (created during Bank Reconciliation, Step 4.5), and
nothing about the payment was invalid — so recording it as a rejection
described something that did not happen, and the row either sat in the
exception queue indefinitely or left a misleading audit trail.

The two routes out, and when each applies
-----------------------------------------
Route A — hitl/manual_mapping.py's capped mapping (rule R9e). Use it when the
    invoices this payment covers ARE known. Each reference is capped at its own
    invoice's outstanding, so the part that is genuinely owed settles and only
    the excess stays unapplied on the receipt.

Route B — this module. Use it when nothing should post: the excess is a
    duplicate payment, belongs to another OU's books, is an advance against
    work not yet invoiced, or is simply unexplained until the customer sends
    remittance advice. Nothing is sent to Oracle. The bare receipt stays
    exactly as it is, still holding the cash unapplied.

What parking deliberately does NOT do
-------------------------------------
It does not reconcile anything in Oracle, and it does not track the residual as
a balance. It records a decision. Oracle remains the system of record for the
unapplied cash — see the LineItem.unapplied_amount comment for the one case
(Route A) where an amount IS written down, and why.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory, User
from ..bff.metrics import _category_for_row, GROUP_CONFLICT_EXCEPTION, GROUP_LABELS
from ..rule_engine.invoice_ledger import release_applications

# The recorded explanations, mapped to the BRD scenarios they come from.
# `other` always requires a comment — see park_overpayment().
DISPOSITIONS: dict[str, str] = {
    "awaiting_remittance": "Waiting for the customer's remittance advice",   # BRD Scenario 3
    "duplicate_payment":   "Duplicate payment — refund or hold for future",  # BRD Scenario 9
    "cross_ou":            "Part of this belongs to another OU's books",     # BRD Scenario 13
    "advance_payment":     "Customer paid in advance of invoicing",
    "other":               "Other (comment required)",
}


def park_overpayment(
    db: Session,
    line_item_id: int,
    disposition: str,
    comment: str | None,
    user: User | None,
    expected_version: int | None = None,
) -> dict:
    """
    Record a disposition for an overpaid row and move it out of the exception
    queue into Overpayment Parked.

    Guarded before anything is mutated; any failure returns a structured error
    and leaves the row exactly as it was:
      1. the row must still be an OPEN overpayment (R11, in Conflict /
         Exception, no SPOC decision already recorded);
      2. optimistic version check — same contract as approve_row / reject_row /
         reopen_row;
      3. the disposition must be a known one, and `other` must carry a comment.

    Reversible via service.py's reopen_row(), which restores pre_park_state and
    re-stakes the invoice claims released here.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    # ── Guard 1: must be an open overpayment ─────────────────────────────────
    if r.rule_id != "R11":
        return {
            "id": r.id,
            "error": "not_an_overpayment",
            "message": (
                f"Row {r.id} is not an open overpayment (rule_id={r.rule_id}) — this "
                f"action only applies to a row where the amount received exceeds the "
                f"matched invoice total."
            ),
        }

    category = _category_for_row(r)
    if category != GROUP_CONFLICT_EXCEPTION:
        # Catches an already-parked row (Overpayment Parked), an approved or
        # rejected one, and anything else that has since moved on.
        return {
            "id": r.id,
            "error": "not_open",
            "category": category,
            "message": (
                f"Row {r.id} is in '{GROUP_LABELS.get(category, category)}' and is no "
                f"longer an open overpayment awaiting a decision."
            ),
        }

    if r.hitl_status is not None:
        return {
            "id": r.id,
            "error": "already_decided",
            "message": (
                f"Row {r.id} already has a recorded SPOC decision "
                f"(hitl_status='{r.hitl_status}')."
            ),
        }

    # ── Guard 2: optimistic locking ──────────────────────────────────────────
    if expected_version is not None and r.version != expected_version:
        return {
            "id": r.id,
            "error": "version_conflict",
            "message": (
                f"Row {r.id} was modified by another user since you loaded it "
                f"(expected version {expected_version}, current version {r.version}). "
                f"Refresh and try again."
            ),
            "current_version": r.version,
        }

    # ── Guard 3: the decision itself must be real ────────────────────────────
    disposition = (disposition or "").strip()
    if disposition not in DISPOSITIONS:
        return {
            "id": r.id,
            "error": "invalid_disposition",
            "message": (
                f"'{disposition}' is not a recognised disposition. Expected one of: "
                f"{', '.join(DISPOSITIONS)}."
            ),
        }
    if disposition == "other" and not (comment or "").strip():
        return {
            "id": r.id,
            "error": "comment_required",
            "message": "A comment is required when the disposition is 'other'.",
        }

    # ── All guards passed — park it ──────────────────────────────────────────
    from_state = r.current_state.value if r.current_state else None
    # Same role pre_reject_state plays for reject: without it, reopen would
    # have to guess where the row came from.
    r.pre_park_state = from_state

    r.overpayment_disposition    = disposition
    r.overpayment_disposition_at = dt.datetime.utcnow()
    r.overpayment_disposition_by = user.email if user else None

    r.current_state = "overpayment_parked"
    r.status        = "Overpayment — Parked"
    r.version       = (r.version or 0) + 1

    # Nothing was applied, so the invoices this row had staked a claim on go
    # back to the pool — holding them would block another payment from settling
    # them for no reason. reopen_row() re-stakes them, with the duplicate check,
    # if this decision is later undone.
    if r.matched_invoices:
        release_applications(db, r)

    db.add(RowStatusHistory(
        line_item_id=r.id,
        from_state=from_state,
        to_state="overpayment_parked",
        trigger="spoc_park_overpayment",
        rule_id=r.rule_id,
        triggered_by=user.email if user else None,
        comment=f"{disposition}" + (f" | {comment.strip()}" if (comment or "").strip() else ""),
    ))
    db.commit()

    return {
        "id": r.id,
        "status": "Overpayment — Parked",
        "current_state": "overpayment_parked",
        "disposition": disposition,
        "disposition_label": DISPOSITIONS[disposition],
        "version": r.version,
    }
