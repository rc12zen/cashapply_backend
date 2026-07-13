"""
app.services.row_detail_service
==================================
Builds the row-detail response (bank_statement / extraction / confirmed_invoices
/ pipeline / oracle / remittance sections), matching the existing frontend's
expected shape from lib/api.ts's getRowDetail() docstring.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import LineItem, RemittanceExtraction
from ..bff.metrics import _category_for_row, GROUP_LABELS, GROUP_READY_FOR_ORACLE
from ..oracle.fusion_client import build_standard_receipt_payload
from ..rule_engine.fx_service import get_ou_display_name


def _build_pipeline(r: LineItem) -> list[dict]:
    """Ordered nodes for the visual flowchart on the row-detail page."""
    nodes = []

    nodes.append({
        "key": "extraction", "label": "Text Extraction",
        "status": "passed" if (r.extracted_invoice_numbers or r.extracted_customer_name) else "failed",
        "detail": f"method={r.extraction_method}, invoice_match={r.invoice_match_pct}%, "
                  f"customer_match={r.customer_match_pct}%",
    })

    remit_status = "passed" if r.remittance_extraction_id else "skipped"
    nodes.append({
        "key": "remittance", "label": "Remittance Lookup",
        "status": remit_status,
        "detail": "Matched remittance found" if r.remittance_extraction_id else "No remittance matched",
    })

    rule_status = "passed" if r.current_state in ("review_approve",) and r.reason_code in (
        "EXACT_MATCH", "ACCEPTABLE_SHORT_PAYMENT", "REMIT_SPLIT_CLEAN", "OVERPAYMENT_EXPLAINED",
    ) else ("failed" if r.is_matched else "skipped")
    nodes.append({
        "key": "rule_engine", "label": "Rule Engine",
        "status": rule_status,
        "detail": f"rule={r.rule_id}, reason={r.reason_code}, shortfall_pct={r.shortfall_pct}",
    })

    hitl_status = {
        "approved": "passed", "rejected": "failed", None: "pending",
    }.get(r.hitl_status, "pending")
    nodes.append({"key": "spoc_review", "label": "SPOC Review", "status": hitl_status,
                  "detail": r.hitl_status or "Awaiting review"})

    post_status = {"success": "passed", "failed": "failed"}.get(r.oracle_post_status, "pending")
    nodes.append({"key": "oracle_post", "label": "Oracle Posting", "status": post_status,
                  "detail": r.post_message or r.oracle_ref_no or "Not yet posted"})

    return nodes


def build_row_detail(db: Session, record_id: int) -> dict:
    r = db.query(LineItem).get(record_id)
    if not r:
        return {"error": "not found"}

    remittance = None
    if r.remittance_extraction_id:
        ext = db.query(RemittanceExtraction).get(r.remittance_extraction_id)
        if ext:
            remittance = {
                "subject": ext.subject,
                "payer": ext.raw_payer_text,
                "payment_reference": ext.payment_reference,
                "payment_date": ext.payment_date.isoformat() if ext.payment_date else None,
                "payment_amount": float(ext.payment_amount) if ext.payment_amount else None,
                "storage_key": ext.storage_key,
            }

    confirmed_invoices = [{
        "invoice_number": m["invoice_number"],
        "customer_name": m["customer_name"],
        "outstanding_amount": m["outstanding_amount"],
        "currency": r.statement_currency,
        "ou_number": m["ou_number"],
        # Oracle's own "NAME(ou)" display string for the invoice's OU — e.g.
        # "DALLAS(205)" — so cross-OU exceptions can show WHICH entity the
        # customer's invoices actually belong to, not just a bare number.
        "ou_display_name": get_ou_display_name(m["ou_number"]) or m["ou_number"],
        "invoice_date": None,
        "remittance_amount": m.get("stated_amount"),
        "computed_amount": m["outstanding_amount"],
    } for m in (r.matched_invoices or [])]

    # Compute category once — drives Approve button + breadcrumb label on frontend
    _cat = _category_for_row(r)

    # ── Oracle payload preview ────────────────────────────────────────────────
    # r.oracle_payload is only ever set by hitl.service._post_to_oracle_and_update
    # AFTER approval, so a row sitting in "Ready for Oracle" always showed an
    # empty payload here before. Build a preview (never posted, just computed)
    # so SPOCs can review Amount/Currency/ConversionRate/receipt-method warnings
    # before they click Approve, not after.
    oracle_payload = r.oracle_payload
    is_preview = False
    if not oracle_payload and _cat == GROUP_READY_FOR_ORACLE:
        try:
            oracle_payload = build_standard_receipt_payload(r, invoice_breakup=None)
            is_preview = True
        except Exception as exc:
            # Never let a broken preview take down the row-detail page —
            # surface it as an audit-style warning instead, same shape as the
            # _receipt_method_unresolved / _fx_leg2_missing fields the builder
            # itself emits.
            oracle_payload = {"_preview_error": f"Could not build payload preview: {exc}"}
            is_preview = True

    return {
        "id":                r.id,
        "category":          _cat,
        "category_label":    GROUP_LABELS.get(_cat, ""),
        "run_id":            r.run_id,
        "is_cross_currency": bool(r.is_cross_currency) if hasattr(r, "is_cross_currency") else None,
        "is_cross_ledger":   bool(r.is_cross_ledger)   if hasattr(r, "is_cross_ledger")   else None,
        "is_cross_ou":       bool(getattr(r, "is_cross_ou_currency", False)),
        "bank_statement": {
            "bank_name": r.bank_name,
            "statement_date": r.statement_date.isoformat() if r.statement_date else None,
            "narrative": r.narrative,
            "bank_account_number": r.account_number,
            "bank_reference": r.bank_reference,
            "credit_amount": float(r.credit_amount or 0),
            "currency": r.statement_currency,
            "business_unit": r.business_unit,
            "ou_number": r.ou_number,
            # Oracle's own "NAME(ou)" display string for the OU the payment
            # was RECEIVED into — e.g. "PUNE(111)". r.business_unit can hold
            # a plain bank-description string instead (see get_ou_display_name
            # docstring), so this is the reliable one to show side-by-side
            # with the invoice's ou_display_name for cross-OU exceptions.
            "ou_display_name": get_ou_display_name(r.ou_number) or r.business_unit,
        },
        "extraction": {
            "method": r.extraction_method,
            "confidence_score": r.confidence_score,
            "extracted_customer": r.extracted_customer_name,
            "primary_invoice": (r.extracted_invoice_numbers or [None])[0],
            "all_invoice_numbers": r.extracted_invoice_numbers,
            "row_type": r.reason_code,
            "is_matched": r.is_matched,
        },
        "confirmed_invoices": confirmed_invoices,
        "sum_outstanding": float(r.target_total or 0),
        "credit_amount": float(r.credit_amount or 0),
        "pipeline": _build_pipeline(r),
        "oracle": {
            "payload": oracle_payload or {},
            "is_preview": is_preview,
            "remittance_scenario": r.reason_code,
            "hitl_status": r.hitl_status,
            "post_status": r.oracle_post_status,
            "oracle_ref_no": r.oracle_ref_no,
            "oracle_status_code": r.oracle_status_code,
            "standard_receipt_id": r.standard_receipt_id,
            "oracle_posted_at": r.oracle_posted_at.isoformat() if r.oracle_posted_at else None,
            "post_message": r.post_message,
        },
        "remittance": remittance,
    }