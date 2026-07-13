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

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory
from ..oracle.fusion_client import OracleFusionClient, build_standard_receipt_payload
from ..bff.metrics import _category_for_row, GROUP_READY_FOR_ORACLE, GROUP_LABELS


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
        "oracle_post_status":  r.oracle_post_status,
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


def _post_to_oracle_and_update(
    db: Session, r: LineItem, invoice_breakup: list[dict] | None
) -> dict:
    payload = build_standard_receipt_payload(r, invoice_breakup)
    r.oracle_payload = payload

    client = OracleFusionClient()
    result = client.post_standard_receipt(payload)

    r.oracle_post_status  = "success" if result["success"] else "failed"
    r.oracle_ref_no       = result.get("oracle_ref_no")
    r.standard_receipt_id = result.get("standard_receipt_id")
    r.oracle_status_code  = result.get("status_code")
    r.post_message        = result.get("message")
    r.oracle_posted_at    = dt.datetime.utcnow()

    return result


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

    # ── PATCH: server-side gate — ONLY ready_for_oracle (R9a/R9b) rows may
    # ever reach an Oracle POST. This is the actual enforcement; the
    # frontend disabling the Approve button is a convenience, not a
    # security boundary. Uses the SAME mapping bff/metrics.py uses for
    # dashboard/ledger grouping, so this can never silently drift out of
    # sync with what the UI calls "Ready for Oracle".
    category = _category_for_row(r)
    if category != GROUP_READY_FOR_ORACLE:
        return {
            "id": r.id,
            "error": "not_approvable",
            "approvable": False,
            "category": category,
            "category_label": GROUP_LABELS.get(category, category),
            "message": (
                f"Row {r.id} is in category '{GROUP_LABELS.get(category, category)}' "
                f"(rule_id={r.rule_id}) — only rows in 'Ready for Oracle' "
                f"(exact match or acceptable short payment) can be approved "
                f"and posted to Oracle. Resolve the underlying issue first "
                f"(provide remittance, correct the match, etc.) to re-evaluate "
                f"this row into Ready for Oracle before approving."
            ),
        }

    r.hitl_status = "approved"
    result = _post_to_oracle_and_update(db, r, invoice_breakup)
    r.current_state = "processed" if result["success"] else "post_failed"
    r.status        = "Processed" if result["success"] else "Post Failed"
    r.version       = (r.version or 0) + 1

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
        "post_status":        r.oracle_post_status,
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

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="review_approve", to_state="rejected",
        trigger="spoc_reject", rule_id=r.rule_id,
        triggered_by=triggered_by, comment=comment,
    ))
    db.commit()
    return {"id": r.id, "status": "Rejected"}


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
    if not r or r.oracle_post_status != "failed":
        return {"error": "Row not eligible for retry"}

    # NOTE: no category gate needed here — oracle_post_status == "failed"
    # can only ever happen for a row that already passed the approve_row()
    # gate above (it had to be ready_for_oracle to attempt a POST at all).
    # Retrying is just re-attempting the same POST, not a new approval
    # decision, so no additional check is required.
    invoice_breakup = [
        {
            "invoice_number":  m["invoice_number"],
            "reference_amount": m.get("stated_amount") or m["outstanding_amount"],
        }
        for m in (r.matched_invoices or [])
    ]
    result = _post_to_oracle_and_update(db, r, invoice_breakup)
    r.current_state = "processed" if result["success"] else "post_failed"
    r.status        = "Processed" if result["success"] else "Post Failed"

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="post_failed", to_state=r.current_state,
        trigger="retry", rule_id=r.rule_id, triggered_by="spoc_ui",
    ))
    db.commit()
    return {
        "id":          r.id,
        "post_status": r.oracle_post_status,
        "message":     r.post_message,
    }