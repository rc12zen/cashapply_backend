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
    approve_row, reject_row, build_breakup_analysis,
    get_hitl_history, retry_oracle_post, serialize_line_item,
    get_mapping_options, get_invoices_for_customer,
    preview_manual_mapping, confirm_manual_mapping,
)
from ..rule_engine.remittance_recheck import recheck_needs_remittance_rows

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