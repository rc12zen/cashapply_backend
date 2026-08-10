"""app.bff.hitl_routes — /api/hitl/*  (PATCHED)

PATCH NOTES (original):
  - approve_row() in hitl_service.py now returns {"error": "not_approvable",
    ...} instead of raising when a row isn't in the "ready_for_oracle"
    category (see hitl_service.py for the full rationale — this closes a
    gap where the Oracle-POST gate only existed as a disabled button on
    the frontend, with no server-side enforcement at all).
  - The single-row /approve/{id} endpoint translates that into a real
    HTTP 400 with a `detail` message, since the frontend's existing catch
    block reads `e?.response?.data?.detail` for error toasts — returning
    a plain 200 with an "error" key inside the body would have looked
    like a silent success to the UI.
  - /approve-bulk does NOT raise on a per-item rejection — it keeps
    collecting every row's individual result (success or "not_approvable")
    so one ineligible row in a mixed selection doesn't abort the whole
    batch. The frontend is expected to surface any "not_approvable"
    entries in the bulk response to the user (e.g. "3 of 5 approved; 2
    skipped — not in Ready for Oracle").

PATCH NOTES (auth/RBAC/audit integration):
  - /approve, /approve-bulk, /retry-oracle require "oracle:post" — held by
    Administrator and Oracle Operator only (see scripts/seed_rbac.py).
  - /reject requires "hitl:reject" — held by Administrator and Oracle
    Operator only (Analyst can map invoices but not approve/reject).
  - /{id}/mapping-confirm and /{id}/recheck-remittance require "hitl:map" —
    held by Administrator, Analyst, AND Oracle Operator (both can map
    invoices; only Oracle Operator/Administrator can also approve/reject).
  - Read-only endpoints require "run:view" (held by every role except
    Viewer).
  - version_conflict (optimistic locking, hitl/service.py) is translated to
    HTTP 409 for the single-row endpoints, same treatment as not_approvable.
  - Every approve/reject/retry is logged via audit.service.log_activity().
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db.models import LineItem, RowState, User
from ..deps import get_db
from ..auth import require_permission
from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..audit.service import log_activity
from ..hitl import (
    approve_row, reject_row, reopen_row, build_breakup_analysis,
    get_hitl_history, retry_oracle_post, retry_receipt_creation_bulk_for_run,
    check_receipt_retry_eligibility_for_run, serialize_line_item,
    get_mapping_options, get_invoices_for_customer,
    preview_manual_mapping, confirm_manual_mapping,
    mark_eligible_for_receipt, discard_row, edit_gl_rate,
    override_settlement_as_customer_payment,
)
from ..hitl.split_and_map import get_distribution_context, preview_distribution, confirm_distribution, get_active_invoices_for_customer
from ..hitl.distribution_actions import (
    approve_distribution_entry, reject_distribution_entry, reopen_distribution_entry,
    edit_gl_rate_for_distribution_entry,
)
from ..rule_engine.remittance_recheck import recheck_needs_remittance_rows
from ..rule_engine.customer_name_correction import correct_customer_name, get_customer_name_options

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/pending")
def get_pending(db: Session = Depends(get_db), user: User = Depends(require_permission("run:view"))):
    rows = db.query(LineItem).filter(LineItem.current_state == RowState.REVIEW_APPROVE).all()
    return {"pending": [serialize_line_item(r) for r in rows]}


@router.get("/preview/{id}")
def get_approval_preview(id: int, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("run:view"))):
    row = db.query(LineItem).get(id)
    if not row:
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    return serialize_line_item(row)


@router.get("/breakup-analysis/{id}")
def breakup_analysis(id: int, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("run:view"))):
    return build_breakup_analysis(db, id)


@router.post("/approve/{id}")
def approve(id: int, payload: dict, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_permission("oracle:post"))):
    result = approve_row(
        db, id, payload.get("comment"), payload.get("invoice_breakup"),
        triggered_by=user.email, expected_version=payload.get("expected_version"),
    )

    # PATCH: translate a category-gate rejection / version conflict / "not
    # found" into a real HTTP error so the frontend's existing
    # `e?.response?.data?.detail` error-toast handling fires correctly,
    # instead of returning 200 with a body that looks like a success shape
    # but contains an "error" key the UI was never checking for.
    if result.get("error") == "not_approvable":
        raise AppError(ErrorCode.ROW_NOT_APPROVABLE, detail=result.get("message"))
    if result.get("error") == "version_conflict":
        raise AppError(ErrorCode.ROW_VERSION_CONFLICT, detail=result.get("message"))
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)

    log_activity(db, user, action="hitl.approve", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 metadata={"oracle_ref_no": result.get("oracle_ref_no"),
                           "post_status": result.get("post_status")})
    db.commit()
    return result


@router.post("/reject/{id}")
def reject(id: int, payload: dict, request: Request, db: Session = Depends(get_db),
           user: User = Depends(require_permission("hitl:reject"))):
    result = reject_row(
        db, id, payload.get("comment"), triggered_by=user.email,
        expected_version=payload.get("expected_version"),
    )
    if result.get("error") == "version_conflict":
        raise AppError(ErrorCode.ROW_VERSION_CONFLICT, detail=result.get("message"))
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)

    log_activity(db, user, action="hitl.reject", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request), metadata={"comment": payload.get("comment")})
    db.commit()
    return result


@router.post("/reopen/{id}")
def reopen(id: int, payload: dict, request: Request, db: Session = Depends(get_db),
           user: User = Depends(require_permission("hitl:reject"))):
    """Undo a rejection — restore the row to the state it was rejected from and
    reuse its existing Oracle receipt (see hitl/service.py's reopen_row()).
    Same permission tier as /reject (hitl:reject). Blocks with a clear reason
    if the row isn't rejected, was changed concurrently, or its invoice can no
    longer be safely re-claimed (gone from aging / taken by another payment)."""
    result = reopen_row(
        db, id, payload.get("comment"), triggered_by=user.email,
        expected_version=payload.get("expected_version"),
    )
    if result.get("error") == "version_conflict":
        raise AppError(ErrorCode.ROW_VERSION_CONFLICT, detail=result.get("message"))
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") in (
        "not_rejected", "aging_unavailable", "invoice_not_in_aging", "invoice_claimed_elsewhere",
    ):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(db, user, action="hitl.reopen", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 metadata={"comment": payload.get("comment"),
                           "restored_state": result.get("current_state")})
    db.commit()
    return result


@router.post("/mark-eligible/{id}")
def mark_eligible(id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_permission("hitl:map"))):
    """
    Unidentified rows only — see hitl/service.py's mark_eligible_for_receipt().
    Confirms this row IS a real receivable transaction and creates the bare
    Oracle receipt right now (Step 4.5 holds it back automatically for
    unidentified rows — see rule_engine/orchestrator.py).
    """
    result = mark_eligible_for_receipt(db, id, triggered_by=user.email)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") in ("not_eligible_for_action", "already_decided"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(db, user, action="hitl.mark_eligible", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 metadata={"receipt_created": result.get("receipt_created"),
                           "oracle_ref_no": result.get("oracle_ref_no")})
    db.commit()
    return result


@router.post("/discard/{id}")
def discard(id: int, payload: dict, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_permission("hitl:reject"))):
    """
    Unidentified rows only — see hitl/service.py's discard_row(). SPOC has
    judged this row is NOT a real receivable transaction at all; moves it
    to its own `discarded` state without ever creating an Oracle receipt.
    Same permission tier as Reject — this is the same weight of decision.
    """
    result = discard_row(db, id, payload.get("comment"), triggered_by=user.email)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") in ("not_eligible_for_action", "already_decided"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(db, user, action="hitl.discard", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request), metadata={"comment": payload.get("comment")})
    db.commit()
    return result


@router.post("/settlement-override/{id}")
def settlement_override(id: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("hitl:map"))):
    """
    Needs Distribution rows only — see hitl/service.py's
    override_settlement_as_customer_payment(). The same payer name (e.g. a
    registered third-party provider) can legitimately be a broker payment
    on some rows and a direct customer payment on others — this moves THIS
    row out of Needs Distribution into the standard Manual Invoice Mapping
    flow instead.
    """
    result = override_settlement_as_customer_payment(db, id, triggered_by=user.email)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") in ("not_needs_distribution", "already_overridden"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(db, user, action="hitl.settlement_override", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 metadata={"customer_name_prefilled": result.get("customer_name_prefilled")})
    db.commit()
    return result


@router.put("/gl-rate/{id}")
def update_gl_rate(id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("oracle:post"))):
    """
    Cross-ledger-currency rows only, and only BEFORE invoice mapping exists
    on the receipt — see hitl/service.py's edit_gl_rate() docstring for the
    exact guard and why. Same permission tier as Approve/Retry — this
    directly PATCHes an Oracle receipt.
    """
    new_rate = payload.get("new_rate")
    if new_rate is None:
        raise AppError(ErrorCode.VALIDATION_FAILED, detail="new_rate is required.")

    result = edit_gl_rate(db, id, float(new_rate), payload.get("reason"), triggered_by=user.email)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") in ("not_cross_ledger", "no_receipt", "already_mapped", "oracle_patch_failed", "retry_failed"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(db, user, action="hitl.edit_gl_rate", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 metadata={"old_rate": result.get("old_rate"), "new_rate": result.get("new_rate"),
                           "reason": payload.get("reason")})
    db.commit()
    return result


@router.post("/approve-bulk")
def approve_bulk(payload: dict, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("oracle:post"))):
    ids = payload.get("ids", [])
    # PATCH: intentionally NOT raising per-item — each id's result (success
    # or {"error": "not_approvable", ...}) is collected individually so a
    # mixed selection (some ready_for_oracle, some conflict_exception)
    # doesn't abort the whole batch on the first ineligible row. The
    # frontend should inspect each entry in `results` and report any
    # "not_approvable" ones back to the SPOC rather than assuming bulk
    # approve is all-or-nothing.
    results = [approve_row(db, i, None, None, triggered_by=user.email) for i in ids]
    skipped = [r for r in results if r.get("error") in ("not_approvable", "version_conflict", "not found")]

    log_activity(db, user, action="hitl.approve_bulk", entity_type="LineItem",
                 ip_address=_client_ip(request),
                 metadata={"ids": ids, "approved_count": len(results) - len(skipped),
                           "skipped_count": len(skipped)})
    db.commit()
    return {
        "results": results,
        "approved_count": len(results) - len(skipped),
        "skipped_count": len(skipped),
    }


@router.get("/history")
def hitl_history(db: Session = Depends(get_db), user: User = Depends(require_permission("run:view"))):
    return get_hitl_history(db)


@router.post("/retry-oracle/{id}")
def retry_oracle(id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("oracle:post"))):
    result = retry_oracle_post(db, id)
    log_activity(db, user, action="oracle.retry", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 status="success" if result.get("success") else "failure",
                 metadata={"post_status": result.get("post_status")})
    db.commit()
    return result


@router.get("/retry-oracle-bulk-for-run/{run_id}/eligibility")
def retry_oracle_bulk_eligibility(run_id: int, db: Session = Depends(get_db),
                                    user: User = Depends(require_permission("oracle:post"))):
    """
    Read-only — never mutates anything. Lets the frontend decide whether to
    even SHOW the "Retry All Failed Receipts" button on a run's detail view,
    rather than showing it on every run and refusing most clicks after the
    fact. Same eligibility rule the actual retry endpoint enforces (single
    source of truth — see hitl/service.py's
    check_receipt_retry_eligibility_for_run()).
    """
    return check_receipt_retry_eligibility_for_run(db, run_id)


@router.post("/retry-oracle-bulk-for-run/{run_id}")
def retry_oracle_bulk_for_run(run_id: int, request: Request, db: Session = Depends(get_db),
                                user: User = Depends(require_permission("oracle:post"))):
    """
    Bulk-retries RECEIPT CREATION (not invoice mapping — see
    retry_receipt_creation_bulk_for_run()'s docstring) for every row in a
    run. Deliberately refuses to run unless every attempted receipt in the
    run is currently failed — see that function for the exact safety gate.
    Surfacing this button on the frontend should itself be conditional on
    the same "every receipt in this run failed" state (e.g. driven off
    GET /run/pending-by-account or a per-run summary), so the button
    doesn't even appear for a run with a normal mix of outcomes.
    """
    result = retry_receipt_creation_bulk_for_run(db, run_id, triggered_by=user.email)
    log_activity(
        db, user, action="oracle.retry_bulk_for_run", entity_type="AnalysisRun", entity_id=run_id,
        ip_address=_client_ip(request),
        status="failure" if "error" in result else "success",
        metadata=result,
    )
    db.commit()
    return result


# ── Manual invoice mapping ────────────────────────────────────────────────────
# For rows that didn't land in ready_for_oracle automatically. Confirming a
# mapping only RE-CLASSIFIES the row into ready_for_oracle — it does NOT post
# to Oracle. Posting still happens through the existing, separate Approve
# action above. See hitl/manual_mapping.py's module docstring for the full
# rationale.

@router.get("/{id}/mapping-options")
def mapping_options(id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("run:view"))):
    result = get_mapping_options(db, id)
    if result.get("error"):
        raise AppError(ErrorCode.MAPPING_INVALID, detail=result["error"])
    return result


@router.get("/{id}/mapping-options/customer")
def mapping_options_for_customer(id: int, customer_name: str, db: Session = Depends(get_db),
                                  user: User = Depends(require_permission("run:view"))):
    result = get_invoices_for_customer(db, id, customer_name)
    if result.get("error"):
        raise AppError(ErrorCode.MAPPING_INVALID, detail=result["error"])
    return result


@router.post("/{id}/mapping-preview")
def mapping_preview(id: int, body: dict, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("run:view"))):
    invoice_numbers = body.get("invoice_numbers") or []
    result = preview_manual_mapping(db, id, invoice_numbers)
    if result.get("error"):
        raise AppError(ErrorCode.MAPPING_INVALID, detail=result["error"])
    return result


@router.post("/{id}/mapping-confirm")
def mapping_confirm(id: int, body: dict, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("hitl:map"))):
    invoice_numbers = body.get("invoice_numbers") or []
    result = confirm_manual_mapping(db, id, invoice_numbers, user)
    if result.get("error"):
        raise AppError(ErrorCode.MAPPING_INVALID, detail=result["error"])
    log_activity(db, user, action="hitl.manual_mapping", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 metadata={"invoice_numbers": invoice_numbers, "rule_id": result.get("rule_id")})
    db.commit()
    return result


@router.post("/{id}/recheck-remittance")
def recheck_remittance(id: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("hitl:map"))):
    """
    Manual counterpart to the periodic remittance_recheck_worker — lets a
    SPOC re-check a SINGLE needs_remittance row on demand (e.g. "the
    customer just told me they sent it, I don't want to wait for the
    next scheduled sweep") instead of only ever happening automatically
    on an interval. Same underlying logic either way — see
    rule_engine/remittance_recheck.py.
    """
    result = recheck_needs_remittance_rows(db, only_line_item_id=id)
    if result.get("error"):
        raise AppError(ErrorCode.REMITTANCE_RECHECK_FAILED, detail=result["error"])

    row_result = result["results"][0] if result["results"] else None
    log_activity(db, user, action="hitl.recheck_remittance", entity_type="LineItem", entity_id=id,
                 ip_address=_client_ip(request),
                 metadata={"changed": bool(row_result and row_result.get("changed")),
                           "to_rule_id": row_result.get("to_rule_id") if row_result else None})
    db.commit()
    return row_result or {"id": id, "changed": False, "reason": "No result produced."}


@router.get("/{id}/customer-name-options")
def customer_name_options_route(id: int, db: Session = Depends(get_db),
                                  user: User = Depends(require_permission("hitl:map"))):
    """
    Real candidate customer names for correcting this row's AI-identified
    customer — same aging-report-backed pick-list pattern as manual
    invoice mapping's /mapping-options, so the frontend can offer a
    searchable list instead of a free-text box. See
    rule_engine/customer_name_correction.py's get_customer_name_options()
    for the exact source (aging_map.customers_for_ou()).
    """
    result = get_customer_name_options(db, id)
    if result.get("error"):
        raise AppError(ErrorCode.CUSTOMER_NAME_CORRECTION_FAILED, detail=result["error"])
    return result


@router.post("/{id}/correct-customer-name")
def correct_customer_name_route(id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                                  user: User = Depends(require_permission("hitl:map"))):
    """
    Lets a SPOC correct a wrongly AI-identified customer name on a row —
    applies to unidentified, needs_remittance, and conflict_exception rows
    (anywhere the AI's own customer guess could plausibly be the actual
    problem). Re-runs the same matching + rule-evaluation pipeline the
    original analysis run used, so the row falls into whatever category
    is now actually correct — see
    rule_engine/customer_name_correction.py for the full mechanics and the
    guard against correcting an already-finalized (approved/rejected/
    manually-mapped) row.

    PATCH: `customer_name` in the payload must now be a REAL name from the
    aging report (see get_customer_name_options() above / the
    /customer-name-options route) — validated server-side regardless of
    what the frontend sends, not just a UI restriction.
    """
    corrected_name = (payload.get("customer_name") or "").strip()
    result = correct_customer_name(db, id, corrected_name, corrected_by=user.email)
    if result.get("error"):
        raise AppError(ErrorCode.CUSTOMER_NAME_CORRECTION_FAILED, detail=result.get("message") or result["error"])

    log_activity(
        db, user, action="hitl.correct_customer_name", entity_type="LineItem", entity_id=id,
        ip_address=_client_ip(request),
        metadata={
            "from_customer_name": result.get("from_customer_name"),
            "to_customer_name": result.get("to_customer_name"),
            "from_rule_id": result.get("from_rule_id"),
            "to_rule_id": result.get("to_rule_id"),
            "to_category": result.get("to_category"),
        },
    )
    db.commit()
    return result


@router.get("/distribution-context/{id}")
def distribution_context(id: int, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("run:view"))):
    """Everything the Payment Distribution screen needs to render for this
    needs_distribution row -- see hitl/split_and_map.py's
    get_distribution_context()."""
    result = get_distribution_context(db, id)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") == "already_distributed":
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))
    return result


@router.get("/distribution-customer-invoices/{id}")
def distribution_customer_invoices(id: int, customer_name: str, db: Session = Depends(get_db),
                                    user: User = Depends(require_permission("run:view"))):
    """ACTIVE invoices for one customer in the distribution table's invoice
    picker -- see hitl/split_and_map.py's get_active_invoices_for_customer()."""
    result = get_active_invoices_for_customer(db, id, customer_name)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    return result


@router.post("/distribution-preview/{id}")
def distribution_preview(id: int, payload: dict, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("run:view"))):
    """Validates a breakup WITHOUT persisting anything -- see hitl/
    split_and_map.py's preview_distribution()."""
    entries = payload.get("entries", [])
    result = preview_distribution(db, id, entries)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    return result


@router.post("/distribution-confirm/{id}")
def distribution_confirm(id: int, payload: dict, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("hitl:map"))):
    """Writes the full per-invoice breakdown onto the parent row (NO child
    rows created) and marks it `distributed` -- see hitl/split_and_map.py's
    confirm_distribution() for the full mechanics and validation. Each
    entry still needs its own Approve & Post via
    /distribution-entry-approve below."""
    entries = payload.get("entries", [])
    result = confirm_distribution(db, id, entries, triggered_by=user.email)
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(
        db, user, action="hitl.confirm_distribution", entity_type="LineItem", entity_id=id,
        ip_address=_client_ip(request),
        metadata={"entry_ids": [e["entry_id"] for e in result.get("breakdown", [])]},
    )
    db.commit()
    return result


@router.post("/distribution-entry-approve/{id}/{entry_id}")
def distribution_entry_approve(id: int, entry_id: str, payload: dict, request: Request,
                                db: Session = Depends(get_db),
                                user: User = Depends(require_permission("oracle:post"))):
    """Approve & Post ONE entry inside a distributed parent's
    distribution_breakdown -- see hitl/distribution_actions.py's
    approve_distribution_entry(). Same permission tier as the normal
    single-row /approve, since this directly posts to Oracle."""
    result = approve_distribution_entry(
        db, id, entry_id, payload.get("comment"), triggered_by=user.email,
        expected_version=payload.get("expected_version"),
    )
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") == "version_conflict":
        raise AppError(ErrorCode.ROW_VERSION_CONFLICT, detail=result.get("message"))
    if result.get("error") in ("entry_not_found", "already_approved", "already_rejected", "not_approvable", "payload_error"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(
        db, user, action="hitl.distribution_entry_approve", entity_type="LineItem", entity_id=id,
        ip_address=_client_ip(request),
        metadata={"entry_id": entry_id, "oracle_ref_no": result.get("oracle_ref_no"),
                  "reference_status": result.get("reference_status")},
    )
    db.commit()
    return result


@router.post("/distribution-entry-reject/{id}/{entry_id}")
def distribution_entry_reject(id: int, entry_id: str, payload: dict, request: Request,
                               db: Session = Depends(get_db),
                               user: User = Depends(require_permission("hitl:reject"))):
    """Reject ONE entry inside a distributed parent's distribution_breakdown
    -- see hitl/distribution_actions.py's reject_distribution_entry()."""
    result = reject_distribution_entry(
        db, id, entry_id, payload.get("comment"), triggered_by=user.email,
        expected_version=payload.get("expected_version"),
    )
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") == "version_conflict":
        raise AppError(ErrorCode.ROW_VERSION_CONFLICT, detail=result.get("message"))
    if result.get("error") in ("entry_not_found", "already_approved"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(
        db, user, action="hitl.distribution_entry_reject", entity_type="LineItem", entity_id=id,
        ip_address=_client_ip(request), metadata={"entry_id": entry_id, "comment": payload.get("comment")},
    )
    db.commit()
    return result


@router.post("/distribution-entry-reopen/{id}/{entry_id}")
def distribution_entry_reopen(id: int, entry_id: str, payload: dict, request: Request,
                               db: Session = Depends(get_db),
                               user: User = Depends(require_permission("hitl:reject"))):
    """Undo a rejected entry inside a distributed parent's distribution_breakdown
    -- see hitl/distribution_actions.py's reopen_distribution_entry(). Same
    permission tier as the entry reject."""
    result = reopen_distribution_entry(
        db, id, entry_id, payload.get("comment"), triggered_by=user.email,
        expected_version=payload.get("expected_version"),
    )
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") == "version_conflict":
        raise AppError(ErrorCode.ROW_VERSION_CONFLICT, detail=result.get("message"))
    if result.get("error") in (
        "entry_not_found", "not_rejected", "aging_unavailable",
        "invoice_not_in_aging", "invoice_claimed_elsewhere",
    ):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(
        db, user, action="hitl.distribution_entry_reopen", entity_type="LineItem", entity_id=id,
        ip_address=_client_ip(request), metadata={"entry_id": entry_id, "comment": payload.get("comment")},
    )
    db.commit()
    return result


@router.put("/distribution-entry-gl-rate/{id}/{entry_id}")
def distribution_entry_gl_rate(id: int, entry_id: str, payload: dict, request: Request,
                                db: Session = Depends(get_db),
                                user: User = Depends(require_permission("oracle:post"))):
    """Edit the GL conversion rate for ONE entry inside a distributed
    parent's distribution_breakdown -- see hitl/distribution_actions.py's
    edit_gl_rate_for_distribution_entry()."""
    new_rate = payload.get("new_rate")
    if new_rate is None:
        raise AppError(ErrorCode.VALIDATION_FAILED, detail="new_rate is required.")

    result = edit_gl_rate_for_distribution_entry(
        db, id, entry_id, float(new_rate), payload.get("reason"), triggered_by=user.email,
    )
    if result.get("error") == "not found":
        raise AppError(ErrorCode.ROW_NOT_FOUND)
    if result.get("error") in ("entry_not_found", "not_cross_ledger", "no_receipt", "already_mapped",
                                "oracle_patch_failed", "retry_failed", "payload_error"):
        raise AppError(ErrorCode.VALIDATION_FAILED, detail=result.get("message"))

    log_activity(
        db, user, action="hitl.distribution_entry_edit_gl_rate", entity_type="LineItem", entity_id=id,
        ip_address=_client_ip(request),
        metadata={"entry_id": entry_id, "old_rate": result.get("old_rate"),
                  "new_rate": result.get("new_rate"), "reason": payload.get("reason")},
    )
    db.commit()
    return result