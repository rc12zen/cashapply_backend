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


def correct_customer_name(
    db: Session, line_item_id: int, corrected_customer_name: str, corrected_by: str,
) -> dict:
    """
    Overwrites LineItem.extracted_customer_name with a human-supplied
    correction, then re-runs matching + rule evaluation against it —
    exactly like remittance_recheck.py's _recheck_one(), but keyed off a
    name correction instead of a newly-arrived remittance, and not
    restricted to rows currently sitting in needs_remittance.

    Returns:
      {"error": "not_found"}
      {"error": "not_eligible", "message": ...}
      {"error": "no_aging_map", "message": ...}
      {"error": "invalid_input", "message": ...}
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

    from_rule_id = r.rule_id
    from_customer_name = r.extracted_customer_name

    # Preserve the ORIGINAL AI-extracted name exactly once — the first
    # correction records what the AI actually said; a second correction
    # (correcting a correction) should not overwrite that original record.
    if not r.customer_name_corrected:
        r.ai_extracted_customer_name = r.extracted_customer_name

    r.extracted_customer_name = corrected_customer_name
    # Human-supplied name is treated as a confirmed exact match, not a
    # fuzzy guess — feeds into rule_input below the same way the AI's own
    # match percentage would have.
    r.customer_match_pct = 100.0
    r.customer_name_corrected = True
    r.customer_name_corrected_at = dt.datetime.utcnow()
    r.customer_name_corrected_by = corrected_by

    remittance_view = build_remittance_view(db, r, corrected_customer_name)
    ou_status = resolve_ou_status(
        customer_name=corrected_customer_name,
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

    rule_result = evaluate_row(rule_input)
    r.remittance_extraction_id = remittance_view.get("extraction_id")
    apply_transition(db, r, rule_result, trigger="customer_name_correction", triggered_by=corrected_by)

    # A separate, explicit audit entry for the NAME change itself — the
    # transition's own RowStatusHistory row (added by apply_transition
    # above) only records reason_code, which says nothing about what a
    # human actually typed in. Without this, "why did this row's customer
    # change" would be invisible in the row's history.
    db.add(RowStatusHistory(
        line_item_id=r.id,
        from_state=r.current_state.value if r.current_state else None,
        to_state=r.current_state.value if r.current_state else None,
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