"""
app.services.row_detail_service
==================================
Builds the row-detail response (bank_statement / extraction / confirmed_invoices
/ pipeline / oracle / remittance sections), matching the existing frontend's
expected shape from lib/api.ts's getRowDetail() docstring.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import LineItem, RemittanceExtraction, RemittanceInvoiceLine
from ..bff.metrics import _category_for_row, GROUP_LABELS, GROUP_READY_FOR_ORACLE, GROUP_SHORT_PAYMENT, GROUP_DISTRIBUTED
from ..oracle.fusion_client import build_receipt_creation_payload, build_remittance_reference_payloads
from ..rule_engine.fx_service import get_ou_display_name
from ..hitl.actions_registry import get_available_actions


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

    receipt_status = {"success": "passed", "failed": "failed"}.get(r.oracle_post_status, "pending")
    nodes.append({"key": "receipt_creation", "label": "Receipt Creation", "status": receipt_status,
                  "detail": r.post_message or r.oracle_ref_no or "Not yet created — happens automatically after reconciliation"})

    reference_status = {"success": "passed", "failed": "failed"}.get(r.reference_status, "pending")
    nodes.append({"key": "invoice_mapping", "label": "Invoice Mapping", "status": reference_status,
                  "detail": r.reference_message or "Awaiting SPOC approval"})

    return nodes


def build_row_detail(db: Session, record_id: int, user_permission_codes: set[str] | None = None) -> dict:
    r = db.query(LineItem).get(record_id)
    if not r:
        return {"error": "not found"}

    remittance = None
    if r.remittance_extraction_id:
        ext = db.query(RemittanceExtraction).get(r.remittance_extraction_id)
        if ext:
            lines = (
                db.query(RemittanceInvoiceLine)
                .filter(RemittanceInvoiceLine.extraction_id == ext.id)
                .all()
            )
            remittance = {
                "subject": ext.subject,
                "sender": ext.sender,
                "payer": ext.raw_payer_text,
                "customer_name": ext.raw_customer_text,
                "payment_reference": ext.payment_reference,
                "payment_date": ext.payment_date.isoformat() if ext.payment_date else None,
                "payment_amount": float(ext.payment_amount) if ext.payment_amount else None,
                "payment_currency": ext.payment_currency,
                "storage_key": ext.storage_key,
                "filename": ext.filename,
                # PATCH: the actual email/document body — was never returned
                # before (App2 extracted it for Claude but discarded it
                # rather than persisting it, so this field was always empty
                # regardless of frontend support for it). Now backed by
                # RemittanceExtraction.raw_text — see agent/graph/
                # remittance_graph.py's node_persist.
                "raw_body": ext.raw_text,
                # Per-invoice lines from the email, for the "Invoices in
                # email" table the frontend already renders — previously
                # never joined in here even though the data existed.
                "invoices": [{
                    "invoice_number": ln.invoice_number,
                    "amount_paid": float(ln.amount_paid) if ln.amount_paid is not None else None,
                    "document_amount": float(ln.document_amount) if ln.document_amount is not None else None,
                    "document_currency": ln.document_currency,
                } for ln in lines],
                # A real, fetchable link for the original file (the raw
                # .msg/.pdf/.eml App2 archived) — /api/storage/download
                # proxies bytes straight through App1's own storage client,
                # so this works the same whether ENVIRONMENT=local or azure.
                "download_url": (
                    f"/api/storage/download?bucket=remittance-inbox&key={ext.storage_key}"
                    if ext.storage_key else None
                ),
            }

    confirmed_invoices = [{
        "invoice_number": m["invoice_number"],
        "customer_name": m["customer_name"],
        "outstanding_amount": m["outstanding_amount"],
        # PATCH: this used to unconditionally show r.statement_currency
        # (the CREDITED currency) for every invoice here -- wrong whenever
        # the invoice's own currency differs from what was credited (e.g.
        # invoice raised in USD, payment credited in INR) -- confirmed as a
        # real bug, not a display nuance. Automatic matching stores the
        # invoice's real currency under the key "invoice_currency" (see
        # rule_engine/state_machine.py's apply_transition()); manual
        # mapping stores it under "currency" (see
        # hitl/manual_mapping.py's _serialize_invoice()) -- two different
        # key names for the same field depending on which path produced
        # this entry. Check both before ever falling back to the credited
        # currency, which should only happen for stale rows that predate
        # this fix and never had either key populated.
        "currency": m.get("invoice_currency") or m.get("currency") or r.statement_currency,
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
    # r.oracle_payload is set by rule_engine/orchestrator.py's Step 4.5 for
    # EVERY row right after reconciliation — so by the time this endpoint
    # is hit, it should already be populated for virtually every row. The
    # fallback preview below only matters if that step somehow never ran
    # for this row (e.g. data from before this flow existed).
    # A distributed PARENT never gets a receipt of its own (see
    # hitl/split_and_map.py's confirm_distribution() docstring) -- only its
    # children do. Building/showing a payload preview here was misleading:
    # it reflects the ORIGINAL pre-split extraction and can't ever actually
    # post, so it was surfacing confusing "Receipt Failed / Not Yet Mapped"
    # language for a row that was never meant to post at all.
    oracle_payload = None
    is_preview = False
    if _cat != GROUP_DISTRIBUTED:
        oracle_payload = r.oracle_payload
        if not oracle_payload:
            try:
                oracle_payload = build_receipt_creation_payload(r)
                is_preview = True
            except Exception as exc:
                # Never let a broken preview take down the row-detail page —
                # surface it as an audit-style warning instead, same shape as the
                # _receipt_method_unresolved / _fx_leg2_missing fields the builder
                # itself emits.
                oracle_payload = {"_preview_error": f"Could not build payload preview: {exc}"}
                is_preview = True

    # ── Invoice-mapping (reference) payload preview ──────────────────────────
    # r.reference_payload is only set once a SPOC actually approves a
    # ready_for_oracle row (hitl/service.py). For a row currently SITTING
    # in ready_for_oracle awaiting approval, preview what WOULD be sent to
    # Oracle's remittanceReferences child collection, same idea as the
    # receipt-payload preview above.
    reference_payload = r.reference_payload
    reference_is_preview = False
    if not reference_payload and _cat in (GROUP_READY_FOR_ORACLE, GROUP_SHORT_PAYMENT):
        try:
            reference_payload = build_remittance_reference_payloads(r, invoice_breakup=None)
            reference_is_preview = True
        except Exception as exc:
            reference_payload = [{"_preview_error": f"Could not build reference preview: {exc}"}]
            reference_is_preview = True

    # ── Distribution breakdown (no child rows) ────────────────────────────
    # A "distributed" row is the ORIGINAL consolidated bank line -- its
    # per-invoice breakdown lives directly on it now (see
    # hitl/split_and_map.py's confirm_distribution() and
    # hitl/distribution_actions.py for the per-entry Approve & Post/Reject/
    # Edit GL Rate actions that mutate this same column). No separate
    # LineItem rows are queried or created for this at all.
    distribution_breakdown = r.distribution_breakdown if _cat == GROUP_DISTRIBUTED else None

    return {
        "id":                r.id,
        "category":          _cat,
        "category_label":    GROUP_LABELS.get(_cat, ""),
        "distribution_breakdown": distribution_breakdown,
        # Data-driven action list (see hitl/actions_registry.py /
        # db/models.py's ActionDefinition) -- already filtered by both
        # this row's current state AND the caller's permissions, so the
        # frontend renders this directly rather than re-deriving
        # eligibility itself. Empty list if no permission context was
        # supplied (e.g. an internal/background caller).
        "available_actions": (
            get_available_actions(db, r, user_permission_codes) if user_permission_codes is not None else []
        ),
        "run_id":            r.run_id,
        "is_cross_currency": bool(r.is_cross_currency) if hasattr(r, "is_cross_currency") else None,
        "is_cross_ledger":   bool(r.is_cross_ledger)   if hasattr(r, "is_cross_ledger")   else None,
        "is_cross_ou":       bool(getattr(r, "is_cross_ou_currency", False)),
        # The actual evidence behind is_cross_ou above -- which OU(s) the
        # bank account belongs to, and for each OU where the customer was
        # found: the exact matched name, fuzzy match score, and open
        # invoice count/amount. None for rows where cross-OU was never
        # evaluated (no customer signal) or predates this field. See
        # rule_engine/ou_resolver.py::OUResolverResult.customer_ou_details.
        "ou_evidence":       r.ou_evidence,
        # PATCH: persistent record of whether this row's CURRENT mapping
        # came from a SPOC manually picking invoice(s) — see
        # LineItem.manually_mapped's comment in db/models.py. Lets the
        # frontend show "already mapped" clearly instead of re-presenting
        # a blank picker after a successful confirm.
        "manually_mapped":    bool(r.manually_mapped),
        "manually_mapped_at": r.manually_mapped_at.isoformat() if r.manually_mapped_at else None,
        "manually_mapped_by": r.manually_mapped_by,
        # NEW: row identity for credit card / cheque / third-party provider
        # settlements — see LineItem.settlement_type's comment in
        # db/models.py and rule_engine/evaluator.py's R16/R17/R18. Drives
        # specialFlags.tsx's three new badges on the frontend.
        "settlement_type":     getattr(r, "settlement_type", None),
        "settlement_provider": getattr(r, "settlement_provider", None),
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
            # PATCH: was r.oracle_post_status — the frontend
            # (analysis-history/row/[id]/page.tsx) reads oracle.post_status
            # to mean "did this row fully complete" (category derivation +
            # retry-button gating). oracle_post_status now only means "a
            # bare receipt was created during reconciliation" — every row
            # gets one regardless of approval. reference_status (the
            # invoice-mapping outcome) is what "fully complete" actually
            # means now.
            "post_status": r.reference_status,
            "receipt_creation_status": r.oracle_post_status,
            "reference_status": r.reference_status,
            "reference_message": r.reference_message,
            "reference_added_at": r.reference_added_at.isoformat() if r.reference_added_at else None,
            "reference_payload": reference_payload or [],
            "reference_is_preview": reference_is_preview,
            "oracle_ref_no": r.oracle_ref_no,
            "oracle_status_code": r.oracle_status_code,
            "standard_receipt_id": r.standard_receipt_id,
            "oracle_posted_at": r.oracle_posted_at.isoformat() if r.oracle_posted_at else None,
            "post_message": r.post_message,
            # NEW: the actual "receipt created" output from Oracle — was
            # discarded before (only a few extracted fields were kept).
            # None until create_receipt_for_line_item() has actually run
            # for this row (i.e. right after Bank Reconciliation, for
            # every row — see rule_engine/orchestrator.py's Step 4.5).
            "receipt_response_raw": r.oracle_response_raw,
            # NEW: raw response(s) from the invoice-mapping (remittance
            # reference) POST(s) — a list, one per matched invoice. None
            # until a SPOC has actually approved this row.
            "reference_response_raw": r.reference_response_raw,
        },
        "remittance": remittance,
    }