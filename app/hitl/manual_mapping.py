"""
app.hitl.manual_mapping
==========================
Manual invoice-mapping — lets a SPOC hand-pick invoice(s) from the
currently-loaded aging report for a row that didn't land in
ready_for_oracle automatically (unidentified, needs_remittance,
conflict_exception, post_failed, rejected — anything except
ready_for_oracle and processed).

DESIGN (confirmed with the business):
  - Confirming a mapping only RE-CLASSIFIES the row into ready_for_oracle
    (rule_id -> R9a/R9b, same as an automatic match) — it does NOT post
    to Oracle. Posting still happens through the existing, separate
    Approve action (hitl/service.py's approve_row -> _map_invoice_and_update).
    This preserves the two-gate safety model: nothing posts without an
    explicit approval click, and this reuses the invoice-mapping code
    that already exists rather than duplicating a second posting path.
  - Amounts are NEVER typed by the SPOC. Every amount shown/used here
    comes directly from the currently-loaded AgingMap's outstanding_amount
    for the selected invoice(s) — auto-loaded the moment an invoice is
    picked.

Reuses the SAME classification math as rule_engine/evaluator.py's R9
family (shortfall_pct formula, tolerance threshold, rule IDs) so a
manually-confirmed mapping is judged by identical business rules to an
automatic one — it just skips the extraction-specific machinery
(duplicate-invoice-across-customers, already-processed-match, AI
confidence scoring, etc.) that doesn't apply when a human has already
visually verified the customer/invoice against the aging report itself.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory, User
from ..aging import aging_store
from ..rule_engine.evaluator import DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT


def _received_total(r: LineItem) -> float:
    """Same Leg-1 conversion the automatic engine uses — credited amount
    converted into invoice currency, if cross-currency."""
    credit_amount = float(r.credit_amount or 0)
    if r.is_cross_currency and r.fx_credit_to_invoice:
        return round(credit_amount * float(r.fx_credit_to_invoice), 2)
    return credit_amount


def _serialize_invoice(v) -> dict:
    return {
        "invoice_number": v.invoice_number,
        "outstanding_amount": v.outstanding_amount,
        "currency": v.invoice_currency,
        "ou_number": v.ou_number,
        "customer_name": v.customer_name,
        "customer_number": v.customer_number,
    }


def get_mapping_options(db: Session, line_item_id: int) -> dict:
    """
    What the frontend needs to render the manual-mapping picker:
      - whether a customer is already identified for this row (skip
        straight to invoices if so)
      - if identified: that customer's open invoices, amounts included
      - if not: the list of customer names in this row's OU, for a
        search/select step — get_invoices_for_customer() picks up once
        one is chosen.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before mapping invoices."}

    matched = r.matched_invoices or []
    customer_name = (matched[0].get("customer_name") if matched else None) or r.extracted_customer_name or None

    if customer_name:
        invoices = [_serialize_invoice(v) for v in aging_map.invoices_for_customer(customer_name)]
        return {
            "customer_identified": True,
            "customer_name": customer_name,
            "invoices": invoices,
        }

    return {
        "customer_identified": False,
        "customer_name": None,
        "customers": aging_map.customers_for_ou(r.ou_number, limit=500) if r.ou_number else [],
        "invoices": [],
    }


def get_invoices_for_customer(db: Session, line_item_id: int, customer_name: str) -> dict:
    """Step 2 of the picker when no customer was auto-identified — SPOC
    searched/picked a customer, now fetch THEIR open invoices."""
    if not db.query(LineItem).get(line_item_id):
        return {"error": "Row not found"}
    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before mapping invoices."}
    return {
        "customer_name": customer_name,
        "invoices": [_serialize_invoice(v) for v in aging_map.invoices_for_customer(customer_name)],
    }


def _classify(r: LineItem, selected: list[dict]) -> dict:
    """
    Mirrors rule_engine/evaluator.py's R9 family exactly (same formula,
    same tolerance, same rule IDs) — see that file for the canonical
    version. `selected` amounts always come from the aging report, never
    SPOC-typed.
    """
    target_total = round(sum(v["outstanding_amount"] for v in selected), 2)
    received_total = _received_total(r)
    shortfall_pct = 0.0 if target_total == 0 else round((target_total - received_total) / target_total * 100, 2)

    base = {"target_total": target_total, "received_total": received_total, "shortfall_pct": shortfall_pct}

    if shortfall_pct < 0:
        return {
            **base, "qualifies": False, "tag": None, "rule_id": "R11", "reason_code": "OVERPAYMENT_UNEXPLAINED",
            "message": f"Overpayment — received {received_total} exceeds selected invoice total {target_total}. "
                       f"Does not qualify for one-click posting; this needs SPOC review as a conflict/exception.",
        }

    if shortfall_pct == 0:
        return {
            **base, "qualifies": True, "tag": "full_payment", "rule_id": "R9a", "reason_code": "EXACT_MATCH",
            "message": "Exact match — this mapping qualifies for Ready for Oracle.",
        }

    if shortfall_pct <= DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT:
        return {
            **base, "qualifies": True, "tag": "short_payment", "rule_id": "R9b", "reason_code": "ACCEPTABLE_SHORT_PAYMENT",
            "message": f"Short payment within tolerance ({shortfall_pct}% of {DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT}% allowed) "
                       f"— this mapping qualifies for Ready for Oracle.",
        }

    return {
        **base, "qualifies": False, "tag": None, "rule_id": "R9c", "reason_code": "UNEXPLAINED_SHORTAGE",
        "message": f"Shortage of {shortfall_pct}% exceeds the {DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT}% tolerance — "
                   f"does not qualify for one-click posting.",
    }


def preview_manual_mapping(db: Session, line_item_id: int, invoice_numbers: list[str]) -> dict:
    """Read-only — persists nothing. Tells the SPOC whether a selection
    would qualify, before Confirm is clicked."""
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}
    if not invoice_numbers:
        return {"error": "Select at least one invoice."}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before mapping invoices."}

    selected = []
    for inv_num in invoice_numbers:
        view = aging_map.lookup_invoice(inv_num, ou_number=r.ou_number)
        if not view:
            return {"error": f"Invoice '{inv_num}' not found in the loaded aging report."}
        selected.append(_serialize_invoice(view))

    customer_names = {v["customer_name"] for v in selected if v["customer_name"]}
    if len(customer_names) > 1:
        return {
            "error": f"Selected invoices belong to different customers ({', '.join(customer_names)}) — "
                     f"select invoices from one customer only."
        }

    result = _classify(r, selected)
    result["matched_invoices_preview"] = selected
    return result


def confirm_manual_mapping(
    db: Session, line_item_id: int, invoice_numbers: list[str], user: User | None
) -> dict:
    """
    Re-validates from scratch (never trusts a stale client-side preview —
    the aging report or the row itself could have changed in between) and,
    only if it still qualifies, RE-CLASSIFIES the row into ready_for_oracle.
    Does NOT call Oracle — see module docstring for why that's a deliberate,
    separate step via the existing Approve action.
    """
    preview = preview_manual_mapping(db, line_item_id, invoice_numbers)
    if preview.get("error"):
        return preview
    if not preview["qualifies"]:
        return {"error": preview["message"], "qualifies": False}

    r = db.query(LineItem).get(line_item_id)
    selected = preview["matched_invoices_preview"]
    from_state = r.current_state

    r.matched_invoices = [{**v, "stated_amount": v["outstanding_amount"]} for v in selected]
    r.target_total   = preview["target_total"]
    r.shortfall_pct  = preview["shortfall_pct"]
    r.rule_id        = preview["rule_id"]
    r.reason_code    = preview["reason_code"]
    r.is_matched     = True
    r.current_state  = "review_approve"  # same state automatic ready_for_oracle rows sit in, awaiting Approve
    r.status         = "Ready for Oracle (Manual Mapping)"

    # PATCH: persistent record that THIS row's current mapping came from a
    # SPOC, not automatic extraction — see the LineItem.manually_mapped
    # field comment in db/models.py for why this matters (previously the
    # only trace was a RowStatusHistory log entry, unreadable by the
    # row-detail page, which is why the Manual Mapping card couldn't tell
    # "already mapped" apart from "needs mapping").
    r.manually_mapped    = True
    r.manually_mapped_at = dt.datetime.utcnow()
    r.manually_mapped_by = user.email if user else None

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state=from_state, to_state=r.current_state,
        trigger="manual_mapping", rule_id=r.rule_id,
        triggered_by=user.email if user else None,
        comment=f"Manually mapped to invoice(s): {', '.join(invoice_numbers)}",
    ))
    db.commit()

    return {
        "success": True,
        "rule_id": r.rule_id,
        "reason_code": r.reason_code,
        "message": preview["message"] + " Row moved to Ready for Oracle — use Approve to post it.",
    }