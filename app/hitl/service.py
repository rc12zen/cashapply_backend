"""
app.hitl.service  (PATCHED)
=======================================
SPOC review queue actions: approve / reject / breakup-analysis / retry /
history. Approve triggers the Oracle posting client.

PATCH NOTES (this revision):
  - SECURITY/CORRECTNESS GAP CLOSED: approve_row() previously had NO
    server-side check on what category a row was in before posting to
    Oracle — it would query the LineItem by id and post unconditionally,
    regardless of rule_id. The frontend disables the Approve button for
    anything other than "ready_for_oracle" (R9a/R9b), but a disabled
    button is a UI nicety, not enforcement: a direct API call (curl,
    Postman, /approve-bulk with a mixed id list, a stale frontend build)
    could still push a conflict_exception or needs_remittance row straight
    to an Oracle POST. Approve now hard-gates on the row's category,
    computed via the SAME RULE_ID_TO_GROUP mapping bff/metrics.py uses
    for the dashboard/ledger groupings — single source of truth, so the
    business rule ("only ready_for_oracle can post") can never drift out
    of sync between what the UI shows and what the backend actually allows.
  - approve_row() now returns {"error": ..., "approvable": False,
    "category": ...} WITHOUT raising, so a mixed-id call to /approve-bulk
    doesn't blow up the whole batch on the first ineligible row — each
    result in the bulk response reports its own outcome. The single-row
    /api/hitl/approve/{id} route (hitl_routes.py) is responsible for
    translating an ineligible result into a proper HTTP 400 so the
    frontend's existing error-handling path (e.response.data.detail)
    still works for that case.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory
from ..oracle.fusion_client import OracleFusionClient, build_remittance_reference_payloads
from ..oracle.receipt_creation import create_receipt_for_line_item
from ..bff.metrics import _category_for_row, GROUP_READY_FOR_ORACLE, GROUP_SHORT_PAYMENT, GROUP_UNIDENTIFIED, GROUP_NEEDS_DISTRIBUTION, GROUP_LABELS
from ..rule_engine.invoice_ledger import confirm_applications, release_applications

logger = logging.getLogger(__name__)


def serialize_line_item(r: LineItem) -> dict:
    return {
        "id":                  r.id,
        "run_id":              r.run_id,
        "bank_name":           r.bank_name,
        "narrative":           r.narrative,
        "credit_amount":       float(r.credit_amount or 0),
        "currency":            r.statement_currency,
        # ── Currency context ──────────────────────────────────────────────────
        "functional_currency": r.functional_currency,
        "is_cross_currency":   bool(r.is_cross_currency),
        "fx_rate":             float(r.fx_rate) if r.fx_rate else None,
        "fx_rate_source":      r.fx_rate_source,
        # ─────────────────────────────────────────────────────────────────────
        "reason_code":         r.reason_code,
        "current_state":       r.current_state,
        "matched_invoices":    r.matched_invoices,
        "shortfall_pct":       r.shortfall_pct,
        "hitl_status":         r.hitl_status,
        "oracle_post_status":  r.oracle_post_status,  # receipt-creation status (step 1) — see db/models.py
        "reference_status":    r.reference_status,     # invoice-mapping status (step 2), null until approved
        "post_message":        r.post_message,
        # PATCH: surface the same category every other endpoint now shows,
        # so the HITL preview/pending views are consistent with the ledger.
        "category":            _category_for_row(r),
        # Optimistic-locking token — echo back as expected_version on
        # approve/reject so a stale UI gets a conflict instead of silently
        # overwriting another SPOC's action. See design doc §9.
        "version":             r.version,
    }


def build_breakup_analysis(db: Session, line_item_id: int) -> dict:
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"needs_breakup": False, "invoices": []}

    invoices      = r.matched_invoices or []
    needs_breakup = len(invoices) > 1

    # ── Currency context for UI display ──────────────────────────────────────
    is_cross     = bool(r.is_cross_currency)
    credited_ccy = r.statement_currency   or ""
    func_ccy     = r.functional_currency  or credited_ccy
    fx_rate      = float(r.fx_rate) if r.fx_rate else None

    out_invoices = []
    for m in invoices:
        # stated_amount is already in functional currency (set by rule engine).
        # outstanding_amount from aging is also in functional currency for same-OU invoices.
        stated   = m.get("stated_amount")
        outstanding = m["outstanding_amount"]

        out_invoices.append({
            "invoice_number":          m["invoice_number"],
            "outstanding":             outstanding,           # in functional currency
            "remittance_amount":       stated,                # in functional currency, may be None
            "suggested_reference_amount": stated or outstanding,
            # Show original credited amount for SPOC context in cross-currency rows
            "credited_equivalent":     (
                round(stated / fx_rate, 2) if (is_cross and fx_rate and stated) else None
            ),
        })

    return {
        "needs_breakup":       needs_breakup,
        "scenario":            r.reason_code,
        "credit_amount":       float(r.credit_amount or 0),
        "credited_currency":   credited_ccy,
        "functional_currency": func_ccy,
        "is_cross_currency":   is_cross,
        "fx_rate":             fx_rate,
        "fx_rate_source":      r.fx_rate_source,
        # Effective received amount in functional currency (what rule engine used)
        "effective_received":  (
            round(float(r.credit_amount or 0) * fx_rate, 2)
            if (is_cross and fx_rate) else float(r.credit_amount or 0)
        ),
        "invoices":            out_invoices,
        "auto_approved":       False,
    }


def _map_invoice_and_update(
    db: Session, r: LineItem, invoice_breakup: list[dict] | None
) -> dict:
    """
    Invoice mapping — step 2 of the two-step Oracle flow. Attaches
    remittanceReferences to the receipt already created for this row
    during Bank Reconciliation (rule_engine/orchestrator.py's Step 4.5) —
    does NOT create a new receipt in the normal case.

    Recovery path: if this row somehow never got a successful receipt
    (r.oracle_post_status != "success" — e.g. the original bare-receipt
    POST failed, or this row was created before this flow existed), the
    receipt is created fresh right here, then the reference is attached
    to it — both in this one call, so a SPOC approving a row always
    either fully succeeds or fails clearly, never silently skips the
    reference step because of a stale receipt-creation failure.
    """
    if r.oracle_post_status != "success":
        logger.info("[invoice_mapping] row=%s has no successful receipt yet — retrying creation first", r.id)
        create_receipt_for_line_item(db, r)
        if r.oracle_post_status != "success":
            # Can't map an invoice reference without a receipt to attach it
            # to — fail clearly rather than attempting a POST that Oracle
            # would reject anyway (no valid standard_receipt_id).
            r.reference_status  = "failed"
            r.reference_added_at = dt.datetime.utcnow()
            r.reference_message = (
                f"Cannot map invoice — receipt creation failed/retried and still "
                f"failed: {r.post_message or 'unknown error'}"
            )
            logger.warning("[invoice_mapping] row=%s FAILED — %s", r.id, r.reference_message)
            return {"success": False, "message": r.reference_message}

    reference_payloads = build_remittance_reference_payloads(r, invoice_breakup)
    r.reference_payload = reference_payloads

    logger.info(
        "[invoice_mapping] row=%s standard_receipt_id=%s — attaching %d reference(s)...",
        r.id, r.standard_receipt_id, len(reference_payloads),
    )

    client = OracleFusionClient()
    results = [
        client.post_remittance_reference(r.standard_receipt_id, ref)
        for ref in reference_payloads
    ]
    r.reference_response_raw = [res.get("raw") for res in results]

    all_succeeded = all(res["success"] for res in results) if results else True
    r.reference_status   = "success" if all_succeeded else "failed"
    r.reference_added_at = dt.datetime.utcnow()
    if all_succeeded:
        r.reference_message = f"{len(results)} reference(s) added successfully"
        logger.info("[invoice_mapping] row=%s SUCCESS — %s", r.id, r.reference_message)
    else:
        failed_msgs = [res["message"] for res in results if not res["success"]]
        r.reference_message = "; ".join(failed_msgs)[:2000]
        logger.warning("[invoice_mapping] row=%s FAILED — %s", r.id, r.reference_message)

    return {
        "success": all_succeeded,
        "message": r.reference_message,
        "oracle_ref_no": r.oracle_ref_no,
        "standard_receipt_id": r.standard_receipt_id,
    }


def approve_row(
    db: Session,
    line_item_id: int,
    comment: str | None,
    invoice_breakup: list[dict] | None,
    triggered_by: str,
    expected_version: int | None = None,
) -> dict:
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    # ── Optimistic locking (design doc §9) — two SPOCs acting on the same
    # row concurrently get a conflict instead of a silent double-post to
    # Oracle. Only enforced when the caller supplies expected_version (the
    # version they last saw in the UI); omitted for internal/bulk callers
    # that intentionally don't track it.
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

    # ── PATCH: server-side gate — ONLY ready_for_oracle (R9a) and
    # short_payment (R9b/R9d) rows may ever reach an Oracle POST. GROUP_
    # SHORT_PAYMENT was split out of GROUP_READY_FOR_ORACLE into its own
    # dashboard/ledger bucket on request, but it did NOT change which rows
    # are actually approvable — both groups still go through the identical
    # Approve -> Oracle POST path, so both must pass this gate. The
    # frontend disabling the Approve button is a convenience, not a
    # security boundary. Uses the SAME mapping bff/metrics.py uses for
    # dashboard/ledger grouping, so this can never silently drift out of
    # sync with what the UI calls "Ready for Oracle" / "Short Payment".
    category = _category_for_row(r)
    if category not in (GROUP_READY_FOR_ORACLE, GROUP_SHORT_PAYMENT):
        return {
            "id": r.id,
            "error": "not_approvable",
            "approvable": False,
            "category": category,
            "category_label": GROUP_LABELS.get(category, category),
            "message": (
                f"Row {r.id} is in category '{GROUP_LABELS.get(category, category)}' "
                f"(rule_id={r.rule_id}) — only rows in 'Ready for Oracle' or 'Short Payment' "
                f"(exact match, acceptable short payment, or a manually-recorded short "
                f"payment) can be approved and posted to Oracle. Resolve the underlying "
                f"issue first (provide remittance, correct the match, etc.) to re-evaluate "
                f"this row into one of those categories before approving."
            ),
        }

    r.hitl_status = "approved"
    result = _map_invoice_and_update(db, r, invoice_breakup)
    r.current_state = "processed" if result["success"] else "post_failed"
    r.status        = "Processed" if result["success"] else "Post Failed"
    r.version       = (r.version or 0) + 1

    # PATCH: upgrade this row's ledger entries from "pending" to
    # "confirmed" once the Oracle invoice-mapping call actually succeeds —
    # see rule_engine/invoice_ledger.py. Left as "pending" on failure (not
    # released) since the invoice IS still genuinely claimed by this row;
    # a failed Oracle call is a posting problem, not a reason to free the
    # invoice back up for another row to grab.
    if result["success"]:
        confirm_applications(db, r)

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="review_approve",
        to_state=r.current_state, trigger="spoc_approve",
        rule_id=r.rule_id, triggered_by=triggered_by, comment=comment,
    ))
    db.commit()

    applications = [{
        "invoice_number":    inv["invoice_number"],
        "amount_outstanding": inv.get("outstanding_amount") or inv.get("reference_amount"),
        "amount_applied":     inv.get("reference_amount")   or inv.get("outstanding_amount"),
        "status":             "applied" if result["success"] else "error",
        "application_id":    None,
        "error":              None if result["success"] else result.get("message"),
    } for inv in (invoice_breakup or r.matched_invoices or [])]

    return {
        "id":                 r.id,
        "oracle_ref_no":      r.oracle_ref_no,
        "oracle_status_code": r.oracle_status_code,
        "standard_receipt_id": r.standard_receipt_id,
        # PATCH: was r.oracle_post_status — that field now only means
        # "a bare receipt exists" (set at Bank Reconciliation, before this
        # approve action ever ran). The frontend's post_status field means
        # "did this approve action fully succeed" — that's reference_status
        # now (the invoice-mapping outcome this very call just produced).
        "post_status":         r.reference_status,
        "receipt_creation_status": r.oracle_post_status,
        "reference_status":        r.reference_status,
        "is_cross_currency":  bool(r.is_cross_currency),
        "fx_rate_used":       float(r.fx_rate) if r.fx_rate else None,
        "applications":       applications,
    }


def reject_row(
    db: Session,
    line_item_id: int,
    comment: str | None,
    triggered_by: str,
    expected_version: int | None = None,
) -> dict:
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

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

    r.hitl_status   = "rejected"
    r.current_state = "rejected"
    r.status        = "Rejected"
    r.version       = (r.version or 0) + 1

    # PATCH: free every invoice this row had claimed — see
    # rule_engine/invoice_ledger.py. Without this, rejecting a row would
    # leave its invoice(s) permanently marked "pending" in the ledger,
    # falsely blocking a correct future mapping to the same invoice.
    release_applications(db, r)

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="review_approve", to_state="rejected",
        trigger="spoc_reject", rule_id=r.rule_id,
        triggered_by=triggered_by, comment=comment,
    ))
    db.commit()
    return {"id": r.id, "status": "Rejected"}


def _guard_unidentified_undecided(r: LineItem) -> dict | None:
    """Shared guard for both eligibility actions below — returns an error
    dict if this row isn't a genuinely-undecided unidentified row, else
    None. Both actions only make sense once, on a row that's actually
    sitting in Unidentified with no prior eligibility decision."""
    if _category_for_row(r) != GROUP_UNIDENTIFIED:
        return {
            "error": "not_eligible_for_action",
            "message": (
                f"Row {r.id} is not in the Unidentified category — this "
                f"action only applies to rows with no extracted signal at all."
            ),
        }
    if r.receipt_eligibility is not None:
        return {
            "error": "already_decided",
            "message": (
                f"Row {r.id} already has a recorded eligibility decision "
                f"('{r.receipt_eligibility}') and cannot be decided again."
            ),
        }
    return None


def mark_eligible_for_receipt(db: Session, line_item_id: int, triggered_by: str) -> dict:
    """
    The SPOC has looked at an Unidentified row and confirmed it IS a real
    receivable transaction (just one the automatic extraction couldn't
    read) — creates the bare Oracle receipt right now, the same call
    Step 4.5 would have made automatically for every other category (see
    rule_engine/orchestrator.py's Step 4.5 docstring for why unidentified
    rows are held back from that automatic step in the first place).

    The row's category doesn't change here — it's still R8/NO_SIGNAL, still
    Unidentified, still needs a customer/invoice match via Manual Invoice
    Mapping (hitl/manual_mapping.py) same as before. This action ONLY
    unblocks receipt creation; it is not a substitute for actually mapping
    the row.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    guard_error = _guard_unidentified_undecided(r)
    if guard_error:
        return guard_error

    result = create_receipt_for_line_item(db, r)

    r.receipt_eligibility    = "eligible"
    r.receipt_eligibility_at = dt.datetime.utcnow()
    r.receipt_eligibility_by = triggered_by

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="unidentified", to_state="unidentified",
        trigger="spoc_mark_eligible", rule_id=r.rule_id,
        triggered_by=triggered_by,
        comment=(
            f"Marked eligible for receipt creation — "
            f"{'receipt created (' + str(r.oracle_ref_no) + ')' if result.get('success') else 'receipt creation FAILED: ' + str(result.get('message'))}"
        ),
    ))
    db.commit()

    return {
        "id": r.id,
        "receipt_eligibility": "eligible",
        "receipt_created": bool(result.get("success")),
        "oracle_ref_no": r.oracle_ref_no,
        "standard_receipt_id": r.standard_receipt_id,
        "message": result.get("message"),
    }


def discard_row(db: Session, line_item_id: int, comment: str | None, triggered_by: str) -> dict:
    """
    The SPOC has looked at an Unidentified row and judged it is NOT a real
    receivable transaction at all (garbage narration, an internal transfer,
    a duplicate bank feed entry, etc.) — moves it to its own terminal
    `discarded` state WITHOUT ever creating an Oracle receipt for it. See
    db/models.py's RowState.DISCARDED and bff/metrics.py's GROUP_DISCARDED
    for why this is deliberately distinct from `rejected` (which implies a
    receipt/mapping decision existed and was reversed).
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    guard_error = _guard_unidentified_undecided(r)
    if guard_error:
        return guard_error

    r.receipt_eligibility    = "discarded"
    r.receipt_eligibility_at = dt.datetime.utcnow()
    r.receipt_eligibility_by = triggered_by
    r.current_state          = "discarded"
    r.status                 = "Discarded"
    r.version                = (r.version or 0) + 1

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="unidentified", to_state="discarded",
        trigger="spoc_discard", rule_id=r.rule_id,
        triggered_by=triggered_by, comment=comment,
    ))
    db.commit()

    return {"id": r.id, "status": "Discarded"}


def override_settlement_as_customer_payment(db: Session, line_item_id: int, triggered_by: str) -> dict:
    """
    A settlement identifier match (R16/R17/R18 — card/cheque/third-party) is
    a PATTERN match, not always an unambiguous fact. The clearest example:
    a registered third-party provider like "Assurant Inc" usually pays on
    behalf of OTHER customers, but can occasionally pay its OWN invoice
    directly — same payer name, two entirely different real-world
    situations. This lets a SPOC say "no, for THIS payment, treat it as a
    normal direct customer payment" instead of a broker split.

    settlement_type/settlement_provider are deliberately left untouched —
    the pattern match itself was still correct, this only records which
    bucket a SPOC chose for this specific payment (see
    LineItem.settlement_override_at's comment). The row falls back to the
    Unidentified bucket (see bff/metrics.py's _category_for_row) so the
    standard Manual Invoice Mapping card takes over — customer_name is
    pre-filled with the matched provider name (third-party rows only) as a
    starting point for that search, since it's very often the right
    customer name too, just not always.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    if _category_for_row(r) != GROUP_NEEDS_DISTRIBUTION:
        return {
            "error": "not_needs_distribution",
            "message": f"Row {r.id} is not currently in Needs Distribution — nothing to override.",
        }
    if r.settlement_override_at is not None:
        return {
            "error": "already_overridden",
            "message": f"Row {r.id} was already moved to the customer-payment bucket.",
        }

    r.settlement_override_at = dt.datetime.utcnow()
    r.settlement_override_by = triggered_by
    if r.settlement_type == "third_party_provider" and not r.extracted_customer_name:
        r.extracted_customer_name = r.settlement_provider

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="needs_distribution", to_state="unidentified",
        trigger="spoc_settlement_override", rule_id=r.rule_id,
        triggered_by=triggered_by,
        comment=(
            f"Moved from Needs Distribution ({r.settlement_type}"
            f"{' — ' + r.settlement_provider if r.settlement_provider else ''}) "
            f"to direct customer payment — Manual Invoice Mapping applies now."
        ),
    ))
    db.commit()

    return {
        "id": r.id,
        "settlement_override": True,
        "customer_name_prefilled": r.extracted_customer_name,
        "message": "Moved to the customer-payment bucket. Use Manual Invoice Mapping to complete it.",
    }


def edit_gl_rate(
    db: Session, line_item_id: int, new_rate: float, reason: str | None, triggered_by: str,
) -> dict:
    """
    Overrides the Leg 2 (invoice -> functional) GL conversion rate for a
    cross-ledger-currency row. Branches on whether a receipt already
    exists in Oracle:

      CASE A -- receipt already created (standard_receipt_id is set):
        PATCHes it directly (OracleFusionClient.patch_standard_receipt).
        GUARD (business decision, confirmed): only allowed BEFORE invoice
        mapping exists on this receipt (reference_status != "success") --
        once remittanceReferences are posted, changing the rate afterward
        would desync the applied amounts from what the receipt now claims
        its rate was. That case needs a reverse-and-recreate correction,
        not a field edit, and isn't handled here.

      CASE B -- receipt creation itself FAILED (standard_receipt_id is
        still None, oracle_post_status == "failed"):
        A PATCH requires an existing StandardReceiptId -- there's nothing
        to patch, since Oracle never returned one. See
        oracle/receipt_creation.py's create_receipt_for_line_item(): the
        exact same wrong rate showing in the failed payload could well be
        WHY creation failed in the first place. So instead: set
        fx_invoice_to_functional to the corrected rate (which
        build_receipt_creation_payload reads directly -- see
        fusion_client.py), then RE-ATTEMPT receipt creation with a fresh
        POST using the corrected payload. This is a real gap the original
        version of this function had -- it only ever covered Case A.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    if not r.is_cross_ledger:
        return {
            "error": "not_cross_ledger",
            "message": f"Row {r.id} is not a cross-ledger-currency row -- there is no GL rate to edit.",
        }

    old_rate = r.fx_invoice_to_functional
    # Keep the ORIGINAL Oracle-resolved rate exactly once -- a second edit
    # must not overwrite gl_rate_original with the first edit's value, or
    # the audit trail loses what the original calculation actually
    # produced.
    if r.gl_rate_original is None:
        r.gl_rate_original = old_rate

    if not r.standard_receipt_id:
        # -- CASE B: no receipt exists yet -- correct the rate and RETRY creation --
        if r.oracle_post_status != "failed":
            return {
                "error": "no_receipt",
                "message": (
                    f"Row {r.id} has no Oracle receipt yet, and receipt creation hasn't "
                    f"actually been attempted (or hasn't failed) -- nothing to retry."
                ),
            }

        r.fx_invoice_to_functional        = new_rate
        r.fx_invoice_to_functional_source = "spoc_manual"
        r.gl_rate_edited_at               = dt.datetime.utcnow()
        r.gl_rate_edited_by               = triggered_by
        r.gl_rate_edit_reason             = reason

        from ..oracle.receipt_creation import create_receipt_for_line_item
        result = create_receipt_for_line_item(db, r)   # rebuilds the payload fresh -- picks up the new rate

        db.add(RowStatusHistory(
            line_item_id=r.id, from_state="post_failed", to_state=r.current_state.value if r.current_state else None,
            trigger="spoc_edit_gl_rate_retry", rule_id=r.rule_id,
            triggered_by=triggered_by,
            comment=(
                f"GL rate changed from {old_rate} to {new_rate} and receipt creation retried "
                f"({'succeeded' if result.get('success') else 'failed again: ' + str(result.get('message'))})"
                + (f" -- {reason}" if reason else "")
            ),
        ))
        db.commit()

        if not result.get("success"):
            return {
                "error": "retry_failed",
                "old_rate": old_rate,
                "new_rate": new_rate,
                "message": f"Rate updated, but retrying receipt creation failed again: {result.get('message')}",
            }

        return {
            "id": r.id,
            "success": True,
            "old_rate": old_rate,
            "new_rate": new_rate,
            "gl_rate_original": r.gl_rate_original,
            "standard_receipt_id": r.standard_receipt_id,
            "message": f"GL rate updated to {new_rate} and receipt created successfully ({r.standard_receipt_id}).",
        }

    # -- CASE A: receipt already exists -- PATCH it directly --------------------
    if r.reference_status == "success":
        return {
            "error": "already_mapped",
            "message": (
                f"Row {r.id} already has invoice mapping (remittanceReferences) posted "
                f"against this receipt -- the GL rate can no longer be edited here. "
                f"This needs a reverse-and-recreate correction instead."
            ),
        }

    client = OracleFusionClient()
    result = client.patch_standard_receipt(
        r.standard_receipt_id,
        {"ConversionRateType": "User", "ConversionRate": new_rate},
    )

    if not result.get("success"):
        return {
            "error": "oracle_patch_failed",
            "message": f"Oracle rejected the rate change: {result.get('message')}",
        }

    r.fx_invoice_to_functional        = new_rate
    r.fx_invoice_to_functional_source = "spoc_manual"
    r.gl_rate_edited_at               = dt.datetime.utcnow()
    r.gl_rate_edited_by               = triggered_by
    r.gl_rate_edit_reason             = reason

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state=r.current_state.value if r.current_state else None,
        to_state=r.current_state.value if r.current_state else None,
        trigger="spoc_edit_gl_rate", rule_id=r.rule_id,
        triggered_by=triggered_by,
        comment=f"GL rate changed from {old_rate} to {new_rate}" + (f" -- {reason}" if reason else ""),
    ))
    db.commit()

    return {
        "id": r.id,
        "success": True,
        "old_rate": old_rate,
        "new_rate": new_rate,
        "gl_rate_original": r.gl_rate_original,
        "message": f"GL rate updated to {new_rate} on Oracle receipt {r.standard_receipt_id}.",
    }


def get_hitl_history(db: Session) -> dict:
    rows = (
        db.query(RowStatusHistory)
        .filter(RowStatusHistory.trigger.in_(["spoc_approve", "spoc_reject", "retry"]))
        .order_by(RowStatusHistory.created_at.desc())
        .limit(500)
        .all()
    )
    return {"history": [{
        "line_item_id": h.line_item_id,
        "from_state":   h.from_state,
        "to_state":     h.to_state,
        "trigger":      h.trigger,
        "triggered_by": h.triggered_by,
        "comment":      h.comment,
        "created_at":   h.created_at.isoformat() if h.created_at else None,
    } for h in rows]}


def retry_oracle_post(db: Session, line_item_id: int) -> dict:
    r = db.query(LineItem).get(line_item_id)
    # PATCH: was r.oracle_post_status != "failed" — that field now means
    # "receipt creation failed", not "the row failed to fully process".
    # The retry button/action is about redoing invoice mapping (and, if
    # needed, the receipt creation underneath it too — see
    # _map_invoice_and_update's recovery path), so gate on reference_status.
    if not r or r.reference_status != "failed":
        return {"error": "Row not eligible for retry"}

    # NOTE: no category gate needed here — reference_status == "failed"
    # can only ever happen for a row that already passed the approve_row()
    # gate above (it had to be ready_for_oracle to attempt invoice mapping
    # at all). Retrying is just re-attempting the same mapping (and, if
    # the underlying receipt itself never succeeded, recreating that too),
    # not a new approval decision, so no additional check is required.
    invoice_breakup = [
        {
            "invoice_number":  m["invoice_number"],
            "reference_amount": m.get("stated_amount") or m["outstanding_amount"],
        }
        for m in (r.matched_invoices or [])
    ]
    result = _map_invoice_and_update(db, r, invoice_breakup)
    r.current_state = "processed" if result["success"] else "post_failed"
    r.status        = "Processed" if result["success"] else "Post Failed"

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="post_failed", to_state=r.current_state,
        trigger="retry", rule_id=r.rule_id, triggered_by="spoc_ui",
    ))
    db.commit()
    return {
        "id":          r.id,
        "post_status": r.reference_status,
        "message":     r.post_message,
    }


def check_receipt_retry_eligibility_for_run(db: Session, run_id: int) -> dict:
    """
    Read-only check: would retry_receipt_creation_bulk_for_run() actually be
    allowed to run for this run_id right now? Used by the frontend to decide
    whether to even SHOW the "Retry All Failed Receipts" button on a run's
    detail view, so it only ever appears where clicking it would genuinely
    do something -- not on every run, refusing most of them after the fact.

    Returns exactly the same shape retry_receipt_creation_bulk_for_run()
    would return on a refusal, PLUS an explicit "eligible" boolean:
      {"eligible": False, "error": "no_receipt_attempts", ...}
      {"eligible": False, "error": "not_all_failed", "succeeded_count":...,
       "failed_count":..., "total_count":...}
      {"eligible": True, "failed_count":..., "total_count":...}
    Never mutates anything -- no receipt is touched, no commit happens.
    """
    rows = (
        db.query(LineItem)
        .filter(LineItem.run_id == run_id, LineItem.oracle_post_status.isnot(None))
        .all()
    )
    if not rows:
        return {
            "eligible": False,
            "error": "no_receipt_attempts",
            "message": f"No receipt-creation attempts found for run {run_id} -- nothing to retry.",
        }

    succeeded_count = sum(1 for r in rows if r.oracle_post_status == "success")
    failed_count = sum(1 for r in rows if r.oracle_post_status == "failed")
    if succeeded_count > 0 or failed_count != len(rows):
        return {
            "eligible": False,
            "error": "not_all_failed",
            "message": (
                f"Run {run_id} has {succeeded_count} successful and {failed_count} failed "
                f"receipt(s) out of {len(rows)} total -- bulk retry is only offered when "
                f"EVERY receipt in the run failed. Retry the failed row(s) individually instead."
            ),
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "total_count": len(rows),
        }

    return {"eligible": True, "failed_count": failed_count, "total_count": len(rows)}


def retry_receipt_creation_bulk_for_run(db: Session, run_id: int, triggered_by: str | None = None) -> dict:
    """
    Bulk-retries Oracle RECEIPT CREATION (Step 4.5 -- create_receipt_for_
    line_item, NOT invoice mapping/retry_oracle_post above, which is a
    separate later stage) for every row in a given run.

    Deliberately different from retry_oracle_post(): that one only ever
    applies to rows that already passed approval and then failed at the
    invoice-mapping step (reference_status == "failed"). This one targets
    the EARLIER failure mode -- oracle_post_status == "failed" -- which is
    what happens when the bare receipt itself never got created in the
    first place (e.g. a wrong OrganizationUnit name/currency causing every
    single receipt in the run to 404 against Oracle, before any SPOC ever
    got the chance to approve anything).

    SAFETY GATE (by design, not a technical limitation): only proceeds when
    EVERY row in this run that attempted receipt creation currently shows
    oracle_post_status == "failed" -- i.e. a genuine whole-run failure
    (systemic cause: bad OU/BU config, Oracle outage, etc). If the run has
    a MIX of successes and failures, this refuses to run at all -- a mixed
    result means the failures are row-specific, not run-wide, and mass-
    retrying could re-POST receipts for rows that have nothing wrong with
    them just because they happen to share a run_id. Row-specific failures
    should go through the individual per-row retry/reconfigure path instead.

    Shares its eligibility rule with check_receipt_retry_eligibility_for_run()
    above (single source of truth) -- see that function's docstring for the
    exact shape of a refusal.

    Returns:
      {"error": "no_receipt_attempts", ...}          -- no rows to retry
      {"error": "not_all_failed", "failed_count":..., -- mixed run, refused
       "succeeded_count": ..., "total_count": ...}
      {"run_id":..., "attempted":..., "succeeded":...,
       "failed":..., "results": [...]}                -- ran, per-row results
    """
    eligibility = check_receipt_retry_eligibility_for_run(db, run_id)
    if not eligibility["eligible"]:
        return {k: v for k, v in eligibility.items() if k != "eligible"}

    rows = (
        db.query(LineItem)
        .filter(LineItem.run_id == run_id, LineItem.oracle_post_status.isnot(None))
        .all()
    )

    results = []
    for r in rows:
        res = create_receipt_for_line_item(db, r)
        results.append({
            "id": r.id,
            "bank_reference": r.bank_reference,
            "success": bool(res.get("success")),
            "message": r.post_message,
        })

    for r, res in zip(rows, results):
        prior_state = r.current_state
        if res["success"]:
            # Receipt now exists -- state itself doesn't change here (this
            # function only recreates the bare receipt, same as Step 4.5;
            # it does NOT re-run rule evaluation or invoice mapping), but
            # record the transition for the audit trail.
            db.add(RowStatusHistory(
                line_item_id=r.id, from_state=prior_state, to_state=prior_state,
                trigger="retry_bulk_for_run", rule_id=r.rule_id, triggered_by=triggered_by or "spoc_ui",
                comment="Receipt creation retried successfully (bulk, whole-run retry).",
            ))
        else:
            db.add(RowStatusHistory(
                line_item_id=r.id, from_state=prior_state, to_state=prior_state,
                trigger="retry_bulk_for_run", rule_id=r.rule_id, triggered_by=triggered_by or "spoc_ui",
                comment=f"Receipt creation retried (bulk) and FAILED AGAIN — {r.post_message}",
            ))
    db.commit()

    succeeded_after = sum(1 for res in results if res["success"])
    return {
        "run_id": run_id,
        "attempted": len(results),
        "succeeded": succeeded_after,
        "failed": len(results) - succeeded_after,
        "results": results,
    }