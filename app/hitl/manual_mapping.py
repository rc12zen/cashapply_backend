"""
app.hitl.manual_mapping
==========================
Manual invoice-mapping — lets a SPOC hand-pick invoice(s) from the
currently-loaded aging report for a row that didn't land in
ready_for_oracle automatically (unidentified, needs_remittance,
conflict_exception — anything except ready_for_oracle and processed).

PATCH: post_failed and rejected are NO LONGER eligible here, despite an
earlier version of this docstring listing them. Both categories are only
ever reachable AFTER a SPOC decision has already been recorded
(hitl_status == "approved" for post_failed — see bff/metrics.py's
_category_for_row(), which defines post_failed as reference_status ==
"failed", itself only settable post-approval; hitl_status == "rejected"
for rejected rows). Every function below now refuses outright if
hitl_status is already set, since re-mapping underneath an already-made
human decision could silently "unwind" a row Oracle already has a
receipt for back into a pending-approval state, with no real audit trail
beyond a RowStatusHistory entry. If rejected rows specifically should
remain re-mappable on reconsideration, narrow the guard to
hitl_status == "approved" only — see confirm_manual_mapping()'s
docstring.

DESIGN (confirmed with the business):
  - Confirming a mapping only RE-CLASSIFIES the row into ready_for_oracle
    (rule_id -> R9a/R9b, same as an automatic match) — it does NOT post
    to Oracle. Posting still happens through the existing, separate
    Approve action (hitl/service.py's approve_row -> _map_invoice_and_update).
    This preserves the two-gate safety model: nothing posts without an
    explicit approval click, and this reuses the invoice-mapping code
    that already exists rather than duplicating a second posting path.
  - Amounts are NEVER typed by the SPOC. Every amount shown/used here
    comes directly from the currently-loaded AgingMap's outstanding_amount
    for the selected invoice(s) — auto-loaded the moment an invoice is
    picked.

Reuses the SAME classification math as rule_engine/evaluator.py's R9
family (shortfall_pct formula, tolerance threshold, rule IDs) so a
manually-confirmed mapping is judged by identical business rules to an
automatic one — it just skips the extraction-specific machinery
(duplicate-invoice-across-customers, already-processed-match, AI
confidence scoring, etc.) that doesn't apply when a human has already
visually verified the customer/invoice against the aging report itself.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory, User
from ..aging import aging_store
from ..rule_engine.evaluator import DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT
from ..rule_engine.fx_service import FxService


def _received_total(r: LineItem, selected_currency: str | None) -> tuple[float | None, dict]:
    """
    Converts the credited amount into the currency of the invoice(s) the
    SPOC ACTUALLY SELECTED — not r.invoice_currency / r.is_cross_currency,
    which were frozen at analysis time against whatever (if anything) the
    AUTOMATIC extraction guessed.

    PATCH (root cause of a real bug): orchestrator.py's
    _resolve_invoice_currency() falls back to "same as credited" whenever
    NO invoice was auto-matched:
        invoice_currency = _resolve_invoice_currency(...) or credited_currency
    That is exactly the needs_remittance / unidentified / conflict_exception
    case this manual-mapping flow exists for — meaning r.is_cross_currency
    is very often already False on these rows, NOT because the payment is
    genuinely same-currency, but because nothing was resolved yet when that
    flag was set. If a SPOC then manually picks a REAL invoice in a
    genuinely different currency (e.g. invoice is USD, credited was INR),
    the old code silently reused that stale False flag, skipped conversion
    entirely, and compared raw INR against raw USD as if they were the same
    unit — producing nonsensical shortfall/overpayment percentages (a
    huge, wrong number, not a real business result).

    Fix: ignore the cached fields entirely here. Look at the REAL currency
    of what was actually selected (selected_currency, passed in by the
    caller after confirming every selected invoice shares one currency),
    and resolve a FRESH rate via FxService if it differs from credited
    currency — same service, same priority order (gl_daily_rates table,
    then static fallback) the automatic engine itself uses for Leg 1.

    Returns (received_total_or_None, fx_info). received_total is None only
    when conversion was NEEDED but no rate could be resolved — callers
    MUST treat that as "cannot compare, do not classify" rather than
    falling back to an unconverted number. fx_info carries
    {"is_cross_currency", "credited_currency", "selected_currency",
    "fx_rate", "fx_rate_source"} for both the qualifies/preview payload and
    the row-detail audit trail.
    """
    credit_amount = float(r.credit_amount or 0)
    credited_currency = (r.statement_currency or "").upper().strip()
    selected_currency = (selected_currency or "").upper().strip()

    fx_info = {
        "is_cross_currency": False,
        "credited_currency": credited_currency or None,
        "selected_currency": selected_currency or None,
        "fx_rate": None,
        "fx_rate_source": None,
    }

    if not selected_currency or not credited_currency or selected_currency == credited_currency:
        return credit_amount, fx_info

    fx_info["is_cross_currency"] = True
    fx = FxService()
    rate, source = fx.get_rate_with_source(credited_currency, selected_currency, r.statement_date)
    if not rate:
        return None, fx_info

    fx_info["fx_rate"] = rate
    fx_info["fx_rate_source"] = source
    return round(credit_amount * rate, 2), fx_info


def _serialize_invoice(v) -> dict:
    return {
        "invoice_number": v.invoice_number,
        "outstanding_amount": v.outstanding_amount,
        "currency": v.invoice_currency,
        "ou_number": v.ou_number,
        "customer_name": v.customer_name,
        "customer_number": v.customer_number,
    }


def get_mapping_options(db: Session, line_item_id: int) -> dict:
    """
    What the frontend needs to render the manual-mapping picker:
      - whether a customer is already identified for this row (skip
        straight to invoices if so)
      - if identified: that customer's open invoices, amounts included
      - if not: the list of customer names in this row's OU, for a
        search/select step — get_invoices_for_customer() picks up once
        one is chosen.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}
    if r.hitl_status is not None:
        # Defense-in-depth — the frontend already hides the Manual Mapping
        # card once a decision is recorded (see canManuallyMap in the
        # row-detail page), but this endpoint refuses independently too,
        # in case of a stale page or a direct API call.
        return {
            "error": (
                f"This row already has a recorded SPOC decision (hitl_status="
                f"'{r.hitl_status}') and can no longer be manually mapped."
            ),
        }

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before mapping invoices."}

    matched = r.matched_invoices or []
    customer_name = (matched[0].get("customer_name") if matched else None) or r.extracted_customer_name or None

    if customer_name:
        invoices = [_serialize_invoice(v) for v in aging_map.invoices_for_customer(customer_name)]
        return {
            "customer_identified": True,
            "customer_name": customer_name,
            "invoices": invoices,
        }

    return {
        "customer_identified": False,
        "customer_name": None,
        "customers": aging_map.customers_for_ou(r.ou_number, limit=500) if r.ou_number else [],
        "invoices": [],
    }


def get_invoices_for_customer(db: Session, line_item_id: int, customer_name: str) -> dict:
    """Step 2 of the picker when no customer was auto-identified — SPOC
    searched/picked a customer, now fetch THEIR open invoices."""
    if not db.query(LineItem).get(line_item_id):
        return {"error": "Row not found"}
    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before mapping invoices."}
    return {
        "customer_name": customer_name,
        "invoices": [_serialize_invoice(v) for v in aging_map.invoices_for_customer(customer_name)],
    }


def _classify(r: LineItem, selected: list[dict]) -> dict:
    """
    Mirrors rule_engine/evaluator.py's R9 family exactly (same formula,
    same tolerance, same rule IDs) — see that file for the canonical
    version. `selected` amounts always come from the aging report, never
    SPOC-typed.

    PATCH: two real gaps fixed here —
      1. Selected invoices are now required to share ONE currency before
         being summed together (mirrors the existing same-customer check
         just above this call) — previously nothing stopped a SPOC
         selecting invoices in different currencies and having their raw
         outstanding_amounts summed as if they were the same unit.
      2. received_total is now converted into the REAL selected invoice
         currency via a fresh FX lookup (_received_total), not the stale
         analysis-time is_cross_currency/fx_credit_to_invoice fields — see
         that function's docstring for the exact bug this fixes. If the
         rate can't be resolved, this returns a clear FX_RATE_MISSING
         result instead of silently comparing unconverted amounts.
    """
    selected_currencies = {(v.get("currency") or "").upper().strip() for v in selected if v.get("currency")}
    if len(selected_currencies) > 1:
        return {
            "error": (
                f"Selected invoices are in different currencies ({', '.join(sorted(selected_currencies))}) — "
                f"select invoices in a single currency only."
            ),
        }
    selected_currency = next(iter(selected_currencies), None)

    target_total = round(sum(v["outstanding_amount"] for v in selected), 2)
    received_total, fx_info = _received_total(r, selected_currency)

    if received_total is None:
        # Cross-currency but no rate resolved -- mirrors evaluator.py's R13
        # FX_RATE_MISSING. Do NOT fall through and compare raw amounts in
        # different currencies as if they were equal.
        return {
            "target_total": target_total, "received_total": None, "shortfall_pct": None,
            "qualifies": False, "tag": None, "rule_id": "R13", "reason_code": "FX_RATE_MISSING",
            "fx_info": fx_info,
            "message": (
                f"Credited in {fx_info['credited_currency']}, selected invoice(s) are in "
                f"{fx_info['selected_currency']} — FX rate could not be resolved. "
                f"A rate must be available (GL daily rates or static fallback) before this "
                f"selection can be evaluated."
            ),
        }

    shortfall_pct = 0.0 if target_total == 0 else round((target_total - received_total) / target_total * 100, 2)

    base = {
        "target_total": target_total, "received_total": received_total, "shortfall_pct": shortfall_pct,
        "fx_info": fx_info,
    }

    if shortfall_pct < 0:
        return {
            **base, "qualifies": False, "tag": None, "rule_id": "R11", "reason_code": "OVERPAYMENT_UNEXPLAINED",
            "message": f"Overpayment — received {received_total} {fx_info['selected_currency'] or ''} exceeds "
                       f"selected invoice total {target_total} {fx_info['selected_currency'] or ''}. "
                       f"Does not qualify for one-click posting; this needs SPOC review as a conflict/exception.",
        }

    if shortfall_pct == 0:
        return {
            **base, "qualifies": True, "tag": "full_payment", "rule_id": "R9a", "reason_code": "EXACT_MATCH",
            "message": "Exact match — this mapping qualifies for Ready for Oracle.",
        }

    if shortfall_pct <= DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT:
        return {
            **base, "qualifies": True, "tag": "short_payment", "rule_id": "R9b", "reason_code": "ACCEPTABLE_SHORT_PAYMENT",
            "message": f"Short payment within tolerance ({shortfall_pct}% of {DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT}% allowed) "
                       f"— this mapping qualifies for Ready for Oracle.",
        }

    return {
        **base, "qualifies": False, "tag": None, "rule_id": "R9c", "reason_code": "UNEXPLAINED_SHORTAGE",
        "message": f"Shortage of {shortfall_pct}% exceeds the {DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT}% tolerance — "
                   f"does not qualify for one-click posting.",
    }


def preview_manual_mapping(db: Session, line_item_id: int, invoice_numbers: list[str]) -> dict:
    """Read-only — persists nothing. Tells the SPOC whether a selection
    would qualify, before Confirm is clicked."""
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}
    if r.hitl_status is not None:
        return {
            "error": (
                f"This row already has a recorded SPOC decision (hitl_status="
                f"'{r.hitl_status}') and can no longer be manually mapped."
            ),
        }
    if not invoice_numbers:
        return {"error": "Select at least one invoice."}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before mapping invoices."}

    selected = []
    for inv_num in invoice_numbers:
        view = aging_map.lookup_invoice(inv_num, ou_number=r.ou_number)
        if not view:
            return {"error": f"Invoice '{inv_num}' not found in the loaded aging report."}
        selected.append(_serialize_invoice(view))

    customer_names = {v["customer_name"] for v in selected if v["customer_name"]}
    if len(customer_names) > 1:
        return {
            "error": f"Selected invoices belong to different customers ({', '.join(customer_names)}) — "
                     f"select invoices from one customer only."
        }

    result = _classify(r, selected)
    result["matched_invoices_preview"] = selected
    return result


def confirm_manual_mapping(
    db: Session, line_item_id: int, invoice_numbers: list[str], user: User | None
) -> dict:
    """
    Re-validates from scratch (never trusts a stale client-side preview —
    the aging report or the row itself could have changed in between) and,
    only if it still qualifies, RE-CLASSIFIES the row into ready_for_oracle.
    Does NOT call Oracle — see module docstring for why that's a deliberate,
    separate step via the existing Approve action.

    PATCH: refuses outright if this row already has a SPOC decision
    recorded (hitl_status == "approved" or "rejected"). There was
    previously NO check here at all — this endpoint would happily
    overwrite matched_invoices and reset current_state back to
    "review_approve" on a row that had ALREADY been approved (including
    one sitting in post_failed, which — per bff/metrics.py's
    _category_for_row() — can only ever be reached AFTER approval,
    meaning every post_failed row already has hitl_status="approved" set)
    or rejected. That's a real data-integrity gap, not just a frontend
    display issue: it could silently "unwind" a row Oracle already has a
    receipt for back into a pending-approval state in this system, with
    no trace beyond a RowStatusHistory entry. Blocking on hitl_status
    covers both approved AND rejected rows under one rule (a decision was
    already made, of any kind) — narrow this to hitl_status == "approved"
    only if rejected rows should remain re-mappable on reconsideration.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}
    if r.hitl_status is not None:
        return {
            "error": (
                f"This row already has a recorded SPOC decision (hitl_status="
                f"'{r.hitl_status}') and can no longer be manually mapped."
            ),
            "qualifies": False,
        }

    preview = preview_manual_mapping(db, line_item_id, invoice_numbers)
    if preview.get("error"):
        return preview
    if not preview["qualifies"]:
        return {"error": preview["message"], "qualifies": False}

    selected = preview["matched_invoices_preview"]
    fx_info = preview.get("fx_info") or {}
    from_state = r.current_state

    r.matched_invoices = [{**v, "stated_amount": v["outstanding_amount"]} for v in selected]
    r.target_total   = preview["target_total"]
    r.shortfall_pct  = preview["shortfall_pct"]
    r.rule_id        = preview["rule_id"]
    r.reason_code    = preview["reason_code"]
    r.is_matched     = True
    r.current_state  = "review_approve"  # same state automatic ready_for_oracle rows sit in, awaiting Approve
    r.status         = "Ready for Oracle (Manual Mapping)"

    # PATCH: the row's own currency/FX fields were frozen at analysis time
    # against whatever (if anything) automatic extraction guessed -- often
    # wrong for exactly the rows this flow exists for (see
    # _received_total()'s docstring). Overwrite them with the REAL
    # selected-invoice currency and the freshly-resolved rate, so
    # oracle/fusion_client.py's build_receipt_creation_payload() -- which
    # reads these exact fields -- sends the correct Currency/ConversionRate
    # to Oracle instead of silently reusing the stale, pre-mapping values.
    if fx_info.get("selected_currency"):
        r.invoice_currency = fx_info["selected_currency"]
    r.is_cross_currency = bool(fx_info.get("is_cross_currency"))
    r.fx_credit_to_invoice = fx_info.get("fx_rate")
    r.fx_credit_to_invoice_source = fx_info.get("fx_rate_source")

    # PATCH: this used to be a persistent record that THIS row's current
    # mapping came from a SPOC, not automatic extraction — see the
    # LineItem.manually_mapped field comment in db/models.py for why this
    # matters (previously the only trace was a RowStatusHistory log entry,
    # unreadable by the row-detail page, which is why the Manual Mapping
    # card couldn't tell "already mapped" apart from "needs mapping").
    r.manually_mapped    = True
    r.manually_mapped_at = dt.datetime.utcnow()
    r.manually_mapped_by = user.email if user else None

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state=from_state, to_state=r.current_state,
        trigger="manual_mapping", rule_id=r.rule_id,
        triggered_by=user.email if user else None,
        comment=f"Manually mapped to invoice(s): {', '.join(invoice_numbers)}",
    ))
    db.commit()

    return {
        "success": True,
        "rule_id": r.rule_id,
        "reason_code": r.reason_code,
        "message": preview["message"] + " Row moved to Ready for Oracle — use Approve to post it.",
    }