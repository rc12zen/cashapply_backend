"""
app.rule_engine.remittance_recheck
=====================================
Fills a gap identified in review: a bank row that lands in
needs_remittance (R7 — customer identified, no invoice number, no
remittance found YET) was previously only ever evaluated against
remittances that existed at the exact moment the original analysis run
executed. App2 (cashapply-remittance-agent) polls a mailbox
independently and asynchronously — a matching remittance can (and
usually does) arrive well after that run finished, and nothing
previously re-checked these rows once that happened; they were stuck
until a SPOC manually mapped them.

recheck_needs_remittance_rows() re-runs the SAME remittance-lookup +
rule-evaluation logic orchestrator.py's Pass 2 uses, but scoped to
EXISTING needs_remittance rows instead of a fresh analysis run — so a
row can move itself into ready_for_oracle / conflict_exception the
moment its matching remittance shows up, with no re-upload and no
manual mapping required.

Two entry points, same underlying function:
  - Periodic worker (tasks/remittance_recheck_worker.py) — bulk scan,
    interval from REMITTANCE_RECHECK_INTERVAL_SECONDS.
  - Manual "Recheck Remittance" action (bff/hitl_routes.py) — single row,
    via only_line_item_id.

Simplifications vs. the original orchestrator Pass 2 (documented, not
silently dropped):
  - FX legs (fx_credit_to_invoice / fx_invoice_to_functional) are reused
    AS-IS from the row's own already-persisted fields rather than
    re-resolved fresh via FxService/Oracle GL. These were already
    resolved once during the original run and shouldn't meaningfully
    drift in the short window before a remittance arrives; re-hitting
    FxService on every recheck cycle for every needs_remittance row
    would also be wasteful for no real benefit.
  - duplicate_invoice_across_customers / already_processed_match are
    passed as False — this matches what orchestrator._build_rule_input()
    ITSELF already does unconditionally today. Not a new gap introduced
    here, just carried forward from the existing behavior.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..db.models import LineItem
from ..aging import aging_store
from ..oracle.fusion_client import build_receipt_creation_payload
from .evaluator import evaluate_row
from .ou_resolver import resolve_ou_status
from .remittance_lookup import build_remittance_view
from .state_machine import apply_transition

logger = logging.getLogger(__name__)

NEEDS_REMITTANCE_RULE_ID = "R7"


def _recheck_one(db: Session, r: LineItem, aging_map) -> dict:
    """Re-checks a single needs_remittance row. Returns a per-row result dict."""
    remittance_view = build_remittance_view(db, r, r.extracted_customer_name)

    if not remittance_view.get("found"):
        return {"id": r.id, "changed": False, "reason": "No matching remittance yet."}

    if remittance_view.get("ambiguous"):
        # Multiple remittances now match this row's amount/currency/date
        # window — that's exactly the kind of judgment call R3
        # (AMBIGUOUS_REMITTANCE) exists for. Leave it for a SPOC rather
        # than guessing which one is correct.
        return {"id": r.id, "changed": False,
                "reason": "Multiple remittances now match this row — needs SPOC review, not auto-applied."}

    ou_status = resolve_ou_status(
        customer_name=r.extracted_customer_name,
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
            "customer_match_pct":  r.customer_match_pct,
            "invoice_match_pct":   100.0 if r.extracted_invoice_numbers else 0.0,
            "customer_text_match": bool(r.extracted_customer_name),
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

    if rule_result.rule_id == NEEDS_REMITTANCE_RULE_ID:
        # A remittance matched on amount/currency/date, but it STILL
        # didn't carry anything actionable (e.g. no invoice number in it
        # either) — leave the row exactly as it was rather than
        # "transitioning" it to the state it's already in.
        return {"id": r.id, "changed": False,
                "reason": "A remittance matched but did not change this row's classification."}

    from_rule_id = r.rule_id
    r.remittance_extraction_id = remittance_view.get("extraction_id")
    apply_transition(db, r, rule_result, trigger="remittance_recheck", triggered_by="system")

    # Refresh the Oracle payload preview so it reflects the NEW
    # matched_invoices/customer immediately — same fix applied to
    # confirm_manual_mapping() for the same reason (see that module's
    # comment): otherwise the row-detail page would keep showing the
    # stale pre-remittance payload until something else happened to
    # rebuild it.
    try:
        r.oracle_payload = build_receipt_creation_payload(r)
    except Exception:
        pass  # never let a payload-preview rebuild block the recheck itself

    logger.info(
        "[remittance_recheck] row=%s %s -> %s (extraction_id=%s)",
        r.id, from_rule_id, rule_result.rule_id, remittance_view.get("extraction_id"),
    )
    return {
        "id": r.id, "changed": True,
        "from_rule_id": from_rule_id, "to_rule_id": rule_result.rule_id,
        "to_category": rule_result.category,
        "extraction_id": remittance_view.get("extraction_id"),
    }


def recheck_needs_remittance_rows(db: Session, only_line_item_id: int | None = None) -> dict:
    """
    Bulk entry point for the periodic worker (only_line_item_id=None), or
    single-row entry point for the manual "Recheck Remittance" action
    (only_line_item_id=<id>).

    Only ever looks at rows still ACTUALLY waiting on a remittance:
    rule_id == R7, not yet manually mapped, and with no hitl_status set —
    a row a SPOC already hand-mapped, approved, or REJECTED is left
    alone even in the edge case where its rule_id still happens to read
    R7 (reject_row() doesn't require ready_for_oracle, so a SPOC can
    reject a needs_remittance row directly — an automatic recheck must
    never silently un-reject that decision).
    """
    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging map loaded — cannot recheck remittances.", "checked": 0, "changed": 0, "results": []}

    q = db.query(LineItem).filter(
        LineItem.rule_id == NEEDS_REMITTANCE_RULE_ID,
        LineItem.manually_mapped.is_(False),
        LineItem.hitl_status.is_(None),
    )
    if only_line_item_id is not None:
        q = q.filter(LineItem.id == only_line_item_id)

    rows = q.all()
    if only_line_item_id is not None and not rows:
        return {"error": "Row not found, or not currently waiting on a remittance.", "checked": 0, "changed": 0, "results": []}

    results = [_recheck_one(db, r, aging_map) for r in rows]
    db.commit()

    changed = [res for res in results if res["changed"]]
    logger.info("[remittance_recheck] checked=%d changed=%d", len(results), len(changed))
    return {"checked": len(results), "changed": len(changed), "results": results}