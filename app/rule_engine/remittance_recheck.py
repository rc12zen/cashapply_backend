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

recheck_remittance_dependent_rows() re-runs the SAME remittance-lookup +
rule-evaluation logic orchestrator.py's Pass 2 uses, but scoped to
EXISTING rows instead of a fresh analysis run — so a row can move itself
into ready_for_oracle / conflict_exception the moment its matching
remittance shows up, with no re-upload and no manual mapping required.

BROADENED on request to cover THREE groups, not just needs_remittance:
  - needs_remittance (R7) — the original purpose, unchanged: no invoice
    matched at all yet, purely waiting on a remittance to say which one.
  - short_payment (R9b/R9d) — a remittance arriving LATER can carry a
    fuller/different invoice breakdown than the bank-narrative-only guess
    that originally produced the shortfall (e.g. the narrative only named
    one invoice; the remittance names three, some of which explain what
    looked like a shortage). Auto re-classified exactly like R7, via the
    SAME _recheck_one() — see its own docstring for the one extra step
    this requires (releasing the row's OLD invoice-ledger claim before
    the new one is staked).
  - overpayment_parked rows specifically parked with
    overpayment_disposition == "awaiting_remittance" — deliberately NOT
    auto-reclassified. A SPOC closed this row out on purpose; a system
    job silently reopening it would undo a human decision without that
    human's involvement. Instead, _recheck_parked_one() only stamps
    LineItem.remittance_available_at once a match appears, so the SPOC
    can see (durably — bff/row_detail.py, not just a live-computed
    badge) that it's time to look again and reopen it themselves. Rows
    parked for any OTHER disposition (duplicate_payment, cross_ou,
    advance_payment, other) are never touched here at all — those
    dispositions mean the SPOC considered the row genuinely done, not
    "waiting on something".

Two entry points, same underlying function:
  - Periodic worker (tasks/remittance_recheck_worker.py) — bulk scan,
    interval from REMITTANCE_RECHECK_INTERVAL_SECONDS.
  - Manual "Recheck Remittance" action (bff/hitl_routes.py) — single row,
    via only_line_item_id. Works for a row in ANY of the three groups
    above; recheck_remittance_dependent_rows() tries each group's query
    with the id filter applied and acts on whichever one actually
    matches (a row can only ever be in one of the three at a time).

Simplifications vs. the original orchestrator Pass 2 (documented, not
silently dropped):
  - FX legs (fx_credit_to_invoice / fx_invoice_to_functional) are reused
    AS-IS from the row's own already-persisted fields rather than
    re-resolved fresh via FxService/Oracle GL. These were already
    resolved once during the original run and shouldn't meaningfully
    drift in the short window before a remittance arrives; re-hitting
    FxService on every recheck cycle for every row would also be
    wasteful for no real benefit.
  - duplicate_invoice_across_customers / already_processed_match are
    passed as False — this matches what orchestrator._build_rule_input()
    ITSELF already does unconditionally today. Not a new gap introduced
    here, just carried forward from the existing behavior.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowState
from ..aging import aging_store
from ..oracle.fusion_client import build_receipt_creation_payload
from .evaluator import evaluate_row
from .invoice_ledger import release_applications
from .ou_resolver import resolve_ou_status
from .remittance_lookup import build_remittance_view
from .state_machine import apply_transition

logger = logging.getLogger(__name__)

NEEDS_REMITTANCE_RULE_ID = "R7"
# NEW — short_payment's two rule_ids (see bff/metrics.py's RULE_ID_TO_GROUP):
# R9b is automatic (within auto-tolerance), R9d is a SPOC-confirmed manual
# mapping beyond it — both sit in the same display group and both are
# recheckable here for the identical reason (a fuller remittance can still
# arrive after either).
SHORT_PAYMENT_RULE_IDS = ("R9b", "R9d")
# NEW — the only park disposition that means "tell me when a remittance
# shows up" (see hitl/overpayment.py's park_overpayment(), and the
# DISPOSITIONS map in bff/row_detail.py). Every other disposition means the
# SPOC considered the row genuinely resolved.
AWAITING_REMITTANCE_DISPOSITION = "awaiting_remittance"


def _recheck_one(db: Session, r: LineItem, aging_map) -> dict:
    """
    Re-checks a single needs_remittance (R7) or short_payment (R9b/R9d)
    row against remittances currently on file, and auto re-classifies it
    if anything actually changed. Returns a per-row result dict.
    """
    from_rule_id = r.rule_id
    from_invoice_numbers = {
        m.get("invoice_number") for m in (r.matched_invoices or []) if m.get("invoice_number")
    }

    remittance_view = build_remittance_view(db, r, r.extracted_customer_name, aging_map=aging_map)

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
        # Required by evaluate_row -- see _require_credit_memos_lookup().
        # MUST stay identical to orchestrator.py's: if this re-evaluation
        # path used different scoping, a row held for review on the main
        # path would flip to auto-accepted the moment a remittance arrived.
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

    rule_result = evaluate_row(rule_input)

    extraction_id = remittance_view.get("extraction_id")
    new_invoice_numbers = {m.invoice_number for m in (rule_result.matched_invoices or [])}
    classification_changed = rule_result.rule_id != from_rule_id
    invoices_changed = new_invoice_numbers != from_invoice_numbers
    link_changed = extraction_id != r.remittance_extraction_id

    if not classification_changed and not invoices_changed:
        # A remittance matched but produced the IDENTICAL result (e.g. an
        # R7 row whose matching remittance still carries no invoice
        # number either, or a short_payment row whose remittance simply
        # confirms the exact invoice set already matched). Still worth
        # linking the email so the SPOC can see it — see
        # orchestrator.py's _update_line_item_fx() PATCH note for why a
        # found-but-unlinked remittance was a real bug on the main path;
        # same fix applies here.
        if link_changed:
            r.remittance_extraction_id = extraction_id
        return {"id": r.id, "changed": False,
                "reason": "A remittance matched but did not change this row's classification.",
                "extraction_id": extraction_id}

    # Something REAL changed (category and/or the actual invoice set) --
    # release whatever this row previously staked in the ledger BEFORE
    # apply_transition() stakes the NEW set below. Safe no-op for a fresh
    # needs_remittance row (matched_invoices was already empty, so this
    # UPDATE affects zero rows). Without this, a short_payment row whose
    # remittance reveals a DIFFERENT invoice set than its original
    # narrative-only guess would leave the OLD invoice's ledger claim
    # stuck "pending" forever -- record_application() inside
    # apply_transition() only ever adds/upserts the NEW set, it never
    # releases anything on its own.
    if from_invoice_numbers:
        release_applications(db, r)

    r.remittance_extraction_id = extraction_id
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
        r.id, from_rule_id, rule_result.rule_id, extraction_id,
    )
    return {
        "id": r.id, "changed": True,
        "from_rule_id": from_rule_id, "to_rule_id": rule_result.rule_id,
        "to_category": rule_result.category,
        "extraction_id": extraction_id,
    }


def _recheck_parked_one(db: Session, r: LineItem, aging_map) -> dict:
    """
    Re-checks a single overpayment_parked row (parked specifically with
    disposition == "awaiting_remittance") for a newly-arrived remittance.

    Deliberately does NOT reopen or re-classify the row — see this
    module's docstring for why. Only stamps LineItem.remittance_available_at
    the first time a match appears, so the SPOC has a durable signal to
    act on. Never re-checked again once set (nothing more useful happens
    by re-running the lookup before a human actually reopens the row);
    the caller's query already excludes rows where this is set.
    """
    remittance_view = build_remittance_view(db, r, r.extracted_customer_name, aging_map=aging_map)

    if not remittance_view.get("found"):
        return {"id": r.id, "changed": False, "reason": "No matching remittance yet."}

    # Ambiguous is still useful information here (unlike the auto-
    # reclassify path, there's no "apply the wrong one" risk since nothing
    # is being applied) -- but it's not a clean single match either, so
    # don't flag it as definitively available; leave it for a SPOC to
    # notice via the existing live-computed badge if they open the row.
    if remittance_view.get("ambiguous"):
        return {"id": r.id, "changed": False,
                "reason": "Multiple remittances now match this row's amount/date — not a clean match."}

    r.remittance_available_at = dt.datetime.utcnow()
    logger.info(
        "[remittance_recheck] parked row=%s flagged remittance_available (extraction_id=%s)",
        r.id, remittance_view.get("extraction_id"),
    )
    return {
        "id": r.id, "changed": True, "group": "overpayment_parked",
        "reason": "Remittance now available — awaiting SPOC reopen.",
        "extraction_id": remittance_view.get("extraction_id"),
    }


def recheck_remittance_dependent_rows(db: Session, only_line_item_id: int | None = None) -> dict:
    """
    Bulk entry point for the periodic worker (only_line_item_id=None), or
    single-row entry point for the manual "Recheck Remittance" action
    (only_line_item_id=<id>).

    Covers three groups (see module docstring): needs_remittance (R7),
    short_payment (R9b/R9d) -- both auto re-classified via _recheck_one();
    and overpayment_parked rows specifically parked
    "awaiting_remittance" -- flagged only, via _recheck_parked_one().

    Each group's query already excludes rows a SPOC decision has moved
    past: manually_mapped/hitl_status guards for the first two, and
    remittance_available_at IS NULL for parked rows (already flagged
    once -- no need to keep re-checking before a human acts on it).
    """
    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging map loaded — cannot recheck remittances.", "checked": 0, "changed": 0, "results": []}

    needs_remittance_q = db.query(LineItem).filter(
        LineItem.rule_id == NEEDS_REMITTANCE_RULE_ID,
        LineItem.manually_mapped.is_(False),
        LineItem.hitl_status.is_(None),
    )
    # NEW — short_payment group. No manually_mapped filter here: R9d rows
    # are BY DEFINITION manually_mapped=True (hitl/manual_mapping.py's
    # apply_selection() is the only thing that ever produces R9d), so
    # reusing the R7 query's manually_mapped.is_(False) filter verbatim
    # would silently drop every R9d row from this recheck entirely.
    short_payment_q = db.query(LineItem).filter(
        LineItem.rule_id.in_(SHORT_PAYMENT_RULE_IDS),
        LineItem.hitl_status.is_(None),
    )
    # NEW — overpayment_parked group, awaiting_remittance disposition
    # only (see AWAITING_REMITTANCE_DISPOSITION above), not yet flagged.
    parked_q = db.query(LineItem).filter(
        LineItem.current_state == RowState.OVERPAYMENT_PARKED,
        LineItem.overpayment_disposition == AWAITING_REMITTANCE_DISPOSITION,
        LineItem.remittance_available_at.is_(None),
    )

    if only_line_item_id is not None:
        needs_remittance_q = needs_remittance_q.filter(LineItem.id == only_line_item_id)
        short_payment_q    = short_payment_q.filter(LineItem.id == only_line_item_id)
        parked_q           = parked_q.filter(LineItem.id == only_line_item_id)

    needs_remittance_rows = needs_remittance_q.all()
    short_payment_rows    = short_payment_q.all()
    parked_rows           = parked_q.all()

    if only_line_item_id is not None and not (needs_remittance_rows or short_payment_rows or parked_rows):
        return {"error": "Row not found, or not currently eligible for a remittance recheck.",
                "checked": 0, "changed": 0, "results": []}

    results = (
        [_recheck_one(db, r, aging_map) for r in needs_remittance_rows]
        + [_recheck_one(db, r, aging_map) for r in short_payment_rows]
        + [_recheck_parked_one(db, r, aging_map) for r in parked_rows]
    )
    db.commit()

    changed = [res for res in results if res["changed"]]
    logger.info(
        "[remittance_recheck] checked=%d (needs_remittance=%d short_payment=%d overpayment_parked=%d) changed=%d",
        len(results), len(needs_remittance_rows), len(short_payment_rows), len(parked_rows), len(changed),
    )
    return {"checked": len(results), "changed": len(changed), "results": results}


# Backward-compat alias -- the function was scoped to needs_remittance
# only when it was named this; kept in case anything outside this
# codebase's two known callers (tasks/remittance_recheck_worker.py,
# bff/hitl_routes.py -- both updated to the new name) still imports the
# old one.
recheck_needs_remittance_rows = recheck_remittance_dependent_rows
