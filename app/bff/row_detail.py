"""
app.services.row_detail_service
==================================
Builds the row-detail response (bank_statement / extraction / confirmed_invoices
/ pipeline / oracle / remittance sections), matching the existing frontend's
expected shape from lib/api.ts's getRowDetail() docstring.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..common.account_masking import mask_account_number
from ..db.models import LineItem, RemittanceExtraction, RemittanceInvoiceLine
from ..bff.metrics import _category_for_row, GROUP_LABELS, GROUP_READY_FOR_ORACLE, GROUP_SHORT_PAYMENT, GROUP_DISTRIBUTED
from ..oracle.fusion_client import build_receipt_creation_payload, build_remittance_reference_payloads
from ..rule_engine.evaluator import DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT
from ..rule_engine.fx_service import get_ou_display_name
from ..rule_engine.remittance_lookup import build_remittance_view
from ..hitl.actions_registry import get_available_actions
from ..hitl.overpayment import DISPOSITIONS


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


def _build_overpayment(db: Session, r: LineItem) -> dict | None:
    """
    Everything the row-detail screen needs to explain an overpayment, or None
    for a row that has nothing to do with one.

    Covers all three points in the lifecycle:
      - OPEN (R11)              — the excess, the computed cause, the evidence
      - PARKED                  — plus the recorded disposition and who made it
      - PROCESSED via R9e       — plus how much was deliberately left unapplied

    The remittance_now_available flag is computed HERE, on read, rather than by
    a background sweep. A parked row waiting on remittance advice was parked
    pending exactly that document, so the useful moment to check is when someone
    is looking at the list — no worker, no state changing on its own, and the
    row still only moves when a human clicks Reopen.
    """
    is_open_overpayment = r.rule_id == "R11"
    is_parked = bool(r.current_state and r.current_state.value == "overpayment_parked")
    is_capped = r.rule_id == "R9e"

    if not (is_open_overpayment or is_parked or is_capped):
        return None

    target = float(r.target_total or 0)
    shortfall_pct = float(r.shortfall_pct or 0)
    received = round(target * (1 - shortfall_pct / 100), 2) if target else 0.0

    block: dict = {
        "is_open_overpayment": is_open_overpayment and not is_parked,
        "is_parked": is_parked,
        "is_capped": is_capped,
        "target_total": target,
        "received_total": received,
        "excess_amount": round(received - target, 2),
        "invoice_currency": r.invoice_currency,
        # Computed by rule_engine/overpayment_reason.py at analysis time.
        "reason": r.overpayment_reason,
        "evidence": r.overpayment_evidence,
        "disposition": r.overpayment_disposition,
        "disposition_label": DISPOSITIONS.get(r.overpayment_disposition or ""),
        "disposition_at": (
            r.overpayment_disposition_at.isoformat() if r.overpayment_disposition_at else None
        ),
        "disposition_by": r.overpayment_disposition_by,
        # Only set once a capped mapping actually posts — see hitl/service.py's
        # approve_row(). Present but null before that.
        "unapplied_amount": float(r.unapplied_amount) if r.unapplied_amount is not None else None,
        "disposition_options": [
            {"code": code, "label": label} for code, label in DISPOSITIONS.items()
        ],
    }

    if is_parked and r.overpayment_disposition == "awaiting_remittance":
        # Cheap read-time check: has App2 archived a remittance that matches
        # this payment since it was parked? build_remittance_view() is the same
        # matcher the automatic path uses, so a hit here means the row would
        # genuinely re-evaluate differently if reopened.
        try:
            view = build_remittance_view(db, r, r.extracted_customer_name)
            block["remittance_now_available"] = bool(view and view.get("found"))
        except Exception:
            # Advisory badge only — never let it break the detail page.
            block["remittance_now_available"] = False

    return block


def _build_shortage(r: LineItem) -> dict | None:
    """
    Everything the row-detail screen needs to explain a shortage, or None
    for a row that isn't short.

    The mirror of _build_overpayment() above, minus the lifecycle: a
    shortage has no "parked" state and no disposition to record, so this is
    just the arithmetic plus whatever rule_engine/shortage_reason.py worked
    out at analysis time.

    Note `within_tolerance`. It distinguishes the two roads to R9c — a
    shortfall that exceeded the 12% rule (always went to review) from one
    that did NOT but was held back anyway because the customer holds open
    credit memos. That second kind used to be auto-accepted silently, so
    the SPOC seeing it for the first time deserves to be told why it is in
    front of them rather than assuming the tolerance changed.
    """
    if r.rule_id != "R9c":
        return None

    target = float(r.target_total or 0)
    shortfall_pct = float(r.shortfall_pct or 0)
    received = round(target * (1 - shortfall_pct / 100), 2) if target else 0.0

    return {
        "target_total": target,
        "received_total": received,
        "shortfall_amount": round(target - received, 2),
        "shortfall_pct": round(shortfall_pct, 2),
        "invoice_currency": r.invoice_currency,
        "within_tolerance": 0 < shortfall_pct <= DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT,
        "tolerance_pct": DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT,
        # Computed by rule_engine/shortage_reason.py at analysis time.
        "reason": r.shortage_reason,
        "evidence": r.shortage_evidence,
    }


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

    # ── FX: the credited amount expressed in INVOICE currency ────────────────
    # Leg 1 (credited → invoice). The rule engine already did this conversion
    # to decide the shortfall band (see rule_engine/evaluator.py's
    # received_total = round(credit_amount * fx_credit_to_invoice, 2)); we
    # reproduce the SAME value here from the persisted columns so every
    # consumer compares invoice-currency-to-invoice-currency instead of
    # subtracting a credited-currency number from an invoice-currency one.
    # Before this block the payload only carried the raw credited amount
    # (credit_amount / bank_statement.credit_amount), which made the frontend
    # amount comparisons (WhyStatusCard "Over by …%", AgingSnapshotCard footer,
    # OraclePayloadTable) mix currencies on any cross-currency row.
    _credit_credited_ccy = float(r.credit_amount or 0)
    _rate = float(r.fx_credit_to_invoice) if r.fx_credit_to_invoice else None
    if r.is_cross_currency and _rate:
        _credit_invoice_ccy = round(_credit_credited_ccy * _rate, 2)
    else:
        # Same currency, or Leg 1 rate unresolved (R13) — the converted amount
        # is just the credited amount; the frontend uses is_cross_currency +
        # a null rate to decide whether to render the conversion sub-line.
        _credit_invoice_ccy = _credit_credited_ccy
    _invoice_ccy = (
        r.invoice_currency
        or (confirmed_invoices[0]["currency"] if confirmed_invoices else None)
        or r.statement_currency
    )

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
            # Masked -- VAPT flagged the full number shipping in every row
            # response. Full value only via GET
            # /row-detail/{record_id}/reveal-account-number (results_routes.py).
            "bank_account_number": mask_account_number(r.account_number),
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
        # Currency-aware view of the credited amount. sum_outstanding above is
        # in invoice currency; credit_amount above is in credited currency —
        # comparing them directly is only valid when they're the same currency.
        # Use fx.credit_amount_invoice_ccy for any amount comparison instead.
        "fx": {
            "is_cross_currency": bool(r.is_cross_currency),
            "credited_currency": r.statement_currency,
            "invoice_currency": _invoice_ccy,
            "credit_amount_credited_ccy": _credit_credited_ccy,
            "credit_amount_invoice_ccy": _credit_invoice_ccy,
            "fx_credit_to_invoice": _rate,
            "fx_credit_to_invoice_source": r.fx_credit_to_invoice_source,
        },
        "overpayment": _build_overpayment(db, r),
        # Null for every row that isn't R9c, same contract as "overpayment"
        # above — the front end renders the card only when this is present.
        "shortage": _build_shortage(r),
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