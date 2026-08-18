"""
app.rule_engine.customer_name_correction
============================================
Lets a human correct a wrongly AI-identified customer name on an existing
LineItem, then re-runs the SAME matching + rule-evaluation pipeline
orchestrator.py's Pass 2 uses (mirrors rule_engine/remittance_recheck.py's
approach almost exactly, but triggered by a human correction rather than a
newly-arrived remittance) -- so the row can move itself into whatever
category is now actually correct: ready_for_oracle if the corrected name
now matches a real customer with a clean invoice, needs_remittance if a
customer is now found but nothing else lines up yet, conflict_exception
if the correction surfaces a genuine conflict, or it can stay unidentified
if even the corrected name doesn't match anyone in the aging report.

Applies to unidentified, needs_remittance, and conflict_exception rows --
anywhere the AI's own customer-name guess could plausibly be the actual
problem. Deliberately refuses on a row a SPOC has already finalized
(approved/rejected/manually mapped) -- see _is_correctable() below --
since overwriting a human decision by re-running matching underneath it
would silently undo something a person already signed off on.

This does NOT let the user correct invoice numbers or amounts -- only the
customer name. r.extracted_invoice_numbers stays exactly as the AI
extracted it; only the customer-name side of matching is re-derived.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory
from ..aging import aging_store
from ..oracle.fusion_client import build_receipt_creation_payload
from .evaluator import evaluate_row
from .ou_resolver import resolve_ou_status
from .remittance_lookup import build_remittance_view
from .state_machine import apply_transition

logger = logging.getLogger(__name__)

# Rows in any of these terminal/human-decided states are OFF LIMITS for a
# customer-name correction -- re-running matching underneath an already
# finalized human decision could silently overturn it. Mirrors the same
# guard remittance_recheck.py uses (manually_mapped / hitl_status), plus
# the terminal current_state values that can only be reached via an
# explicit SPOC action (approve/reject).
_LOCKED_STATES = {"processed", "rejected", "post_failed"}


def _is_correctable(r: LineItem) -> tuple[bool, str | None]:
    if r.manually_mapped:
        return False, "This row's invoice mapping has already been manually confirmed — customer name can no longer be corrected here."
    if r.hitl_status is not None:
        return False, "This row already has a SPOC decision recorded — customer name can no longer be corrected here."
    current = r.current_state.value if r.current_state else None
    if current in _LOCKED_STATES:
        return False, f"Row is already '{current}' — cannot correct customer name on a finalized row."
    return True, None


def get_customer_name_options(db: Session, line_item_id: int) -> dict:
    """
    Real candidate customer names for correcting this row's AI-identified
    customer — mirrors hitl/manual_mapping.py's get_mapping_options()
    customer-list branch exactly (same aging_map.customers_for_ou() call,
    same 500-name cap), so a SPOC corrects a wrong AI guess by PICKING a
    real customer from the aging report, the same way manual invoice
    mapping already works, rather than typing free text. Free text invites
    typos, won't reliably match anything, and duplicates a picker pattern
    the app already has — this reuses it instead of inventing a second one.

    Returns {"customers": [...], "ou_number": ...} or {"error": ...}.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}

    eligible, reason = _is_correctable(r)
    if not eligible:
        return {"error": reason}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before correcting a customer name."}

    return {
        "customers": aging_map.customers_for_ou(r.ou_number, limit=500) if r.ou_number else [],
        "ou_number": r.ou_number,
    }


def apply_customer_fields(r: LineItem, customer_name: str, corrected_by: str) -> None:
    """
    Write a human-confirmed customer name onto the row, with its audit trail.

    EXTRACTED VERBATIM from correct_customer_name(), which is still its main
    caller. Shared so hitl/reopen_with_edits.py can persist the same fields when
    it has to run its own transition (clearing a mapping and correcting the
    customer in one action, where calling correct_customer_name would mean a
    second apply_transition() in the same session — which raises, see that
    function's comment).
    """
    # Preserve the ORIGINAL AI-extracted name exactly once — the first
    # correction records what the AI actually said; a second correction
    # (correcting a correction) should not overwrite that original record.
    if not r.customer_name_corrected:
        r.ai_extracted_customer_name = r.extracted_customer_name

    r.extracted_customer_name = customer_name
    # Human-supplied name is treated as a confirmed exact match, not a
    # fuzzy guess — feeds into the rule input the same way the AI's own
    # match percentage would have.
    r.customer_match_pct = 100.0
    r.customer_name_corrected = True
    r.customer_name_corrected_at = dt.datetime.utcnow()
    r.customer_name_corrected_by = corrected_by


def evaluate_as_customer(db: Session, r: LineItem, customer_name: str, aging_map):
    """
    Builds this row's rule-engine input AS IF its customer were
    `customer_name`, and evaluates it.

    EXTRACTED VERBATIM from correct_customer_name() below, which is still its
    only mutating caller — the input dict, the hardcoded values and the single
    evaluate_row() call are all unchanged, so this is not a new call site and
    evaluator.py's documented three-call-site invariant still holds.

    Deliberately PURE with respect to the LineItem: it reads `r` but writes
    nothing to it and nothing to the session (build_remittance_view and
    resolve_ou_status are both read-only lookups). That is what lets
    hitl/reopen_with_edits.py dry-run a proposed customer change to preview the
    resulting rule/bucket without persisting anything — the alternative was a
    fourth hand-written copy of this input dict, which is exactly the
    divergence risk the credit_memos_lookup comment below warns about.

    Returns (rule_result, remittance_view) — the caller needs the view too, for
    r.remittance_extraction_id.
    """
    remittance_view = build_remittance_view(db, r, customer_name)
    ou_status = resolve_ou_status(
        customer_name=customer_name,
        bank_ou_number=r.ou_number,
        aging_map=aging_map,
        fuzzy_min_pct=60.0,
    )

    rule_input = {
        "original_row": {
            "credit_amount":       float(r.credit_amount or 0),
            "currency":            r.statement_currency,
            "functional_currency": r.functional_currency,
            "narrative":           r.narrative,
            "bank_reference":      r.bank_reference,
            "ou_number":           r.ou_number,
        },
        "extraction": {
            "extracted_invoices":  r.extracted_invoice_numbers or [],
            "customer_match_pct":  100.0,
            "invoice_match_pct":   100.0 if r.extracted_invoice_numbers else 0.0,
            "customer_text_match": True,
        },
        "remittance": remittance_view,
        "aging_lookup": lambda inv_no, ou: aging_map.lookup_invoice(inv_no, ou),
        # Required by evaluate_row -- see _require_credit_memos_lookup().
        # MUST stay identical to orchestrator.py's: if this re-evaluation
        # path used different scoping, a row held for review on the main
        # path would flip to auto-accepted once a customer name was fixed.
        "credit_memos_lookup": lambda cust_no, ou, ccy: aging_map.credit_memos_for(
            customer_number=cust_no, ou_number=ou, currency=ccy,
        ),
        "cross_currency": {
            "is_cross_currency":              bool(r.is_cross_currency),
            "credited_currency":              r.statement_currency,
            "invoice_currency":               r.invoice_currency,
            "fx_credit_to_invoice":           float(r.fx_credit_to_invoice) if r.fx_credit_to_invoice else None,
            "fx_credit_to_invoice_source":    r.fx_credit_to_invoice_source,
            "is_cross_ledger":                bool(r.is_cross_ledger),
            "functional_currency":            r.functional_currency,
            "fx_invoice_to_functional":       float(r.fx_invoice_to_functional) if r.fx_invoice_to_functional else None,
            "fx_invoice_to_functional_source": r.fx_invoice_to_functional_source,
        },
        "ou_mismatch":                        ou_status.is_cross_ou,
        "customer_ou_numbers":                ou_status.customer_ous,
        "duplicate_invoice_across_customers": False,
        "already_processed_match":            False,
    }

    return evaluate_row(rule_input), remittance_view


def correct_customer_name(
    db: Session, line_item_id: int, corrected_customer_name: str, corrected_by: str,
) -> dict:
    """
    Overwrites LineItem.extracted_customer_name with a human-supplied
    correction, then re-runs matching + rule evaluation against it —
    exactly like remittance_recheck.py's _recheck_one(), but keyed off a
    name correction instead of a newly-arrived remittance, and not
    restricted to rows currently sitting in needs_remittance.

    PATCH: corrected_customer_name must now be a REAL customer name that
    actually exists in the currently-loaded aging report — validated
    server-side via aging_map.invoices_for_customer(), regardless of
    what the frontend sends. Previously this accepted ANY string
    verbatim, which meant a typo'd or entirely made-up name could be
    "corrected" in without ever matching anything real, silently wasting
    the correction. The frontend should now offer a pick-list sourced
    from get_customer_name_options() above (same pattern as manual invoice
    mapping's customer picker) rather than a free-text box — but this
    validation is the actual enforcement, not just a UI nicety, the same
    way every other backend-vs-frontend boundary in this app works.

    Returns:
      {"error": "not_found"}
      {"error": "not_eligible", "message": ...}
      {"error": "no_aging_map", "message": ...}
      {"error": "invalid_input", "message": ...}
      {"error": "invalid_customer", "message": ...}
      {"id":..., "from_rule_id":..., "to_rule_id":..., "to_category":...,
       "from_customer_name":..., "to_customer_name":...}
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not_found"}

    eligible, reason = _is_correctable(r)
    if not eligible:
        return {"error": "not_eligible", "message": reason}

    corrected_customer_name = (corrected_customer_name or "").strip()
    if not corrected_customer_name:
        return {"error": "invalid_input", "message": "Customer name cannot be blank."}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "no_aging_map", "message": "No aging report loaded — cannot re-evaluate."}

    if not aging_map.invoices_for_customer(corrected_customer_name):
        return {
            "error": "invalid_customer",
            "message": (
                f"'{corrected_customer_name}' was not found in the currently-loaded aging "
                f"report. Select a real customer name from the list, not free text."
            ),
        }

    from_rule_id = r.rule_id
    from_customer_name = r.extracted_customer_name

    apply_customer_fields(r, corrected_customer_name, corrected_by)

    rule_result, remittance_view = evaluate_as_customer(db, r, corrected_customer_name, aging_map)
    r.remittance_extraction_id = remittance_view.get("extraction_id")
    apply_transition(db, r, rule_result, trigger="customer_name_correction", triggered_by=corrected_by)

    # A separate, explicit audit entry for the NAME change itself — the
    # transition's own RowStatusHistory row (added by apply_transition
    # above) only records reason_code, which says nothing about what a
    # human actually typed in. Without this, "why did this row's customer
    # change" would be invisible in the row's history.
    db.add(RowStatusHistory(
        line_item_id=r.id,
        # PATCH: apply_transition() above already reassigns
        # line_item.current_state to a plain string internally (see
        # state_machine.py's apply_transition()) -- by this point it is NO
        # LONGER an Enum instance, so calling .value on it again raises
        # AttributeError: 'str' object has no attribute 'value'. Confirmed
        # via a live traceback. r.current_state is already the correct
        # plain string here; use it directly.
        from_state=r.current_state,
        to_state=r.current_state,
        trigger="customer_name_correction",
        rule_id=rule_result.rule_id,
        triggered_by=corrected_by,
        comment=f"Customer name corrected from '{from_customer_name}' to '{corrected_customer_name}'.",
    ))

    # Refresh the Oracle payload preview so it reflects the corrected
    # customer/matched-invoices immediately — same fix already applied to
    # confirm_manual_mapping() and remittance_recheck() for the same
    # reason (see those modules' comments).
    try:
        r.oracle_payload = build_receipt_creation_payload(r)
    except Exception:
        pass  # never let a payload-preview rebuild block the correction itself

    logger.info(
        "[customer_name_correction] row=%s '%s' -> '%s' | rule %s -> %s",
        r.id, from_customer_name, corrected_customer_name, from_rule_id, rule_result.rule_id,
    )

    return {
        "id": r.id,
        "from_rule_id": from_rule_id,
        "to_rule_id": rule_result.rule_id,
        "to_category": rule_result.category,
        "from_customer_name": from_customer_name,
        "to_customer_name": corrected_customer_name,
    }