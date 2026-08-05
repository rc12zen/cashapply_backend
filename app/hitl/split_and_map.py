"""
app.hitl.split_and_map
=========================
The "Path B" screen for needs_distribution rows (credit card / cheque /
third-party provider) — a genuinely multi-customer consolidated bank line
gets broken up here into one child LineItem (and one Oracle receipt) per
customer. "Path A" (the identified name IS the actual paying customer, not
a broker) is handled elsewhere — see hitl/service.py's
override_settlement_as_customer_payment().

Design (confirmed choices):
  - Each customer's share is a FIXED AMOUNT (not a percentage).
  - The breakup must sum to EXACTLY the parent row's credited amount before
    Confirm is allowed — no partial/leave-a-remainder option.
  - Every customer must have at least one ACTIVE invoice selected (an
    invoice with real remaining balance -- not already fully claimed by
    another row's mapping) -- there's nothing to classify a customer's
    share against without one.
  - PATCH: rather than a flat "this is a distribution child" tag, each
    entry is now classified with the SAME rule_id/reason_code the rest of
    the app already uses for "amount vs. selected invoice(s)" (R9a exact
    match / R9b short payment within tolerance / R9d short payment beyond
    tolerance, manually confirmed -- never an overpayment). This reuses
    hitl/manual_mapping.py's _classify() and _received_total() directly,
    rather than re-implementing the same shortfall math a second time --
    it's the exact same question ("does this amount match these invoices"),
    just asked once per customer instead of once for the whole row.
  - Every selected invoice still goes through the SAME duplicate-invoice
    check as manual mapping / automatic matching (rule_engine/
    invoice_ledger.py) — a distribution entry is not a shortcut around that.
  - The PARENT row never gets an Oracle receipt itself (see
    rule_engine/orchestrator.py's Step 4.5) — only the children do, created
    here at confirm time, then flow through the normal Approve & Post path
    same as any other ready_to_post/short_payment row (rule_id already
    resolves to the right dashboard bucket via bff/metrics.py's
    RULE_ID_TO_GROUP -- no new bucket needed for R9a/R9b/R9d, they already
    exist).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory, SettlementIdentifier, SettlementIdentifierType
from ..aging import aging_store
from ..rule_engine.invoice_ledger import get_applied_total, record_application
from ..rule_engine.ou_resolver import resolve_ou_status
from ..rule_engine.fx_service import FxService
from ..oracle.receipt_creation import create_receipt_for_line_item
from .manual_mapping import _classify

DUPLICATE_TOLERANCE = 0.01

# Fields copied VERBATIM from the parent onto every child — same underlying
# bank line, so these facts don't change per customer. Deliberately does
# NOT include credit_amount, customer_name, matched_invoices, or any
# state/status/oracle_* field — those are each child's own.
_COPIED_FIELDS = [
    "bank_name", "account_number", "business_unit", "ou_number",
    "statement_date", "narrative", "bank_reference", "customer_reference_number",
    "statement_currency", "invoice_currency", "functional_currency",
    "is_cross_currency", "fx_credit_to_invoice", "fx_credit_to_invoice_source",
    "is_cross_ledger", "fx_invoice_to_functional", "fx_invoice_to_functional_source",
    "is_cross_ou_currency", "ou_evidence", "settlement_type", "settlement_provider",
]


def get_distribution_context(db: Session, line_item_id: int) -> dict:
    """
    What the Payment Distribution screen needs to render: the parent row's
    total amount, a starting customer roster (pre-filled from the matched
    provider's registered roster for third-party rows; empty — search-only
    — for card/cheque rows, since there's no fixed roster for those), and
    the full aging customer list for the search box either way.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}
    if r.current_state and r.current_state.value == "distributed":
        return {"error": "already_distributed", "message": f"Row {r.id} has already been distributed."}

    roster: list[str] = []
    if r.settlement_type == "third_party_provider" and r.settlement_provider:
        provider = (
            db.query(SettlementIdentifier)
            .filter(
                SettlementIdentifier.identifier_type == SettlementIdentifierType.THIRD_PARTY_PROVIDER,
                SettlementIdentifier.provider_name == r.settlement_provider,
            )
            .first()
        )
        if provider:
            roster = provider.sub_customers or []

    aging_map = aging_store.get_aging_map()
    all_customers = aging_map.all_customer_names() if aging_map else []

    return {
        "line_item_id": r.id,
        "total_amount": float(r.credit_amount or 0),
        "currency": r.statement_currency,
        "settlement_type": r.settlement_type,
        "settlement_provider": r.settlement_provider,
        "roster": roster,
        "all_customers": all_customers,
    }


def get_active_invoices_for_customer(db: Session, line_item_id: int, customer_name: str) -> dict:
    """
    ACTIVE invoices for one customer, for the distribution table's invoice
    picker — "active" means real remaining balance: outstanding_amount
    minus whatever's already claimed elsewhere in the ledger (rule_engine/
    invoice_ledger.py), same source of truth every other invoice picker in
    this app uses. An invoice already fully consumed by another row simply
    isn't offered here at all, rather than being selectable and only
    failing later at Confirm.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "no_aging_report", "message": "No aging report is currently loaded."}

    customer_row, score = aging_map.fuzzy_customer(customer_name, min_pct=40.0)
    if not customer_row:
        return {"error": "unknown_customer", "message": f"'{customer_name}' does not match a customer in the aging report."}

    active = []
    for iv in aging_map.invoices_for_customer(customer_row.customer_name):
        outstanding = float(iv.outstanding_amount)
        already_applied = get_applied_total(db, iv.invoice_number, iv.ou_number)
        remaining = round(outstanding - already_applied, 2)
        if remaining > DUPLICATE_TOLERANCE:
            active.append({
                "invoice_number": iv.invoice_number,
                "outstanding_amount": remaining,   # what's actually left to claim, not the raw aging figure
                "currency": iv.invoice_currency,
            })

    return {"customer_name": customer_row.customer_name, "invoices": active}


def _resolve_entry(db: Session, aging_map, r: LineItem, entry: dict) -> dict:
    """
    Shared per-entry resolution for preview and confirm: validates the
    customer, requires at least one ACTIVE invoice, and classifies the
    entered amount against those invoices via manual_mapping.py's
    _classify() (same R9a/R9b/R9d/overpayment logic used everywhere else
    in this app). Returns either {"error": ...} or a fully resolved entry
    dict ready to build a child LineItem from.
    """
    name = (entry.get("customer_name") or "").strip()
    if not name:
        return {"error": "missing_customer_name", "message": "Every row needs a customer selected."}

    customer_row, score = aging_map.fuzzy_customer(name, min_pct=40.0)
    if not customer_row:
        return {"error": "unknown_customer", "message": f"'{name}' does not match a customer in the aging report."}

    invoice_numbers = entry.get("invoice_numbers") or []
    if not invoice_numbers:
        return {
            "error": "no_invoice_selected",
            "message": f"{customer_row.customer_name} needs at least one invoice selected — there's nothing to "
                       f"classify this amount against otherwise.",
        }

    selected = []
    for inv in invoice_numbers:
        match = next(
            (iv for iv in aging_map.invoices_for_customer(customer_row.customer_name)
             if iv.invoice_number.upper() == inv.upper()),
            None,
        )
        if match is None:
            return {"error": "invalid_invoice", "message": f"Invoice {inv} does not belong to {customer_row.customer_name}."}

        already_applied = get_applied_total(db, match.invoice_number, r.ou_number, exclude_line_item_id=r.id)
        remaining = round(float(match.outstanding_amount) - already_applied, 2)
        if remaining <= DUPLICATE_TOLERANCE:
            return {
                "error": "duplicate_invoice",
                "message": f"Invoice {inv} has no remaining balance left to claim — it's already fully "
                           f"applied elsewhere.",
            }
        selected.append({
            "invoice_number": match.invoice_number,
            "outstanding_amount": remaining,
            "currency": match.invoice_currency,
        })

    amount = round(float(entry.get("amount") or 0), 2)

    # PATCH (real bug, caught by direct question): the parent's
    # is_cross_ou_currency / is_cross_currency / is_cross_ledger / FX rate
    # fields were being copied VERBATIM onto every child via
    # _COPIED_FIELDS -- computed ONCE, against whatever single customer
    # the ORIGINAL extraction guessed before distribution even existed.
    # Each customer in a breakup can have their own real OU and their own
    # invoice's own currency -- these must be recomputed fresh, per entry,
    # exactly the same way rule_engine/orchestrator.py computes them for
    # an ordinary row (same functions: resolve_ou_status, FxService),
    # never inherited from the parent.
    invoice_currency = selected[0]["currency"] or (r.invoice_currency or r.statement_currency)
    credited_currency = (r.statement_currency or "").upper().strip()
    functional_currency = (r.functional_currency or "").upper().strip()
    invoice_currency = (invoice_currency or "").upper().strip()

    is_cross_currency = bool(invoice_currency) and bool(credited_currency) and credited_currency != invoice_currency
    fx_credit_to_invoice, fx_credit_to_invoice_source = (None, None)
    if is_cross_currency:
        fx_service = FxService()
        fx_credit_to_invoice, fx_credit_to_invoice_source = fx_service.get_rate_with_source(
            from_ccy=credited_currency, to_ccy=invoice_currency, rate_date=r.statement_date,
        )

    is_cross_ledger = bool(invoice_currency) and bool(functional_currency) and invoice_currency != functional_currency
    fx_invoice_to_functional, fx_invoice_to_functional_source = (None, None)
    if is_cross_ledger:
        fx_service = FxService()
        fx_invoice_to_functional, fx_invoice_to_functional_source = fx_service.get_rate_with_source(
            from_ccy=invoice_currency, to_ccy=functional_currency, rate_date=r.statement_date,
        )

    fx_fields = {
        "invoice_currency": invoice_currency or r.invoice_currency,
        "is_cross_currency": is_cross_currency,
        "fx_credit_to_invoice": fx_credit_to_invoice,
        "fx_credit_to_invoice_source": fx_credit_to_invoice_source,
        "is_cross_ledger": is_cross_ledger,
        "fx_invoice_to_functional": fx_invoice_to_functional,
        "fx_invoice_to_functional_source": fx_invoice_to_functional_source,
    }

    # ── Cross-OU takes ABSOLUTE precedence over amount classification ─────────
    # Mirrors rule_engine/evaluator.py's R14 exactly: a genuine OU mismatch
    # (this customer's own invoices sit in a DIFFERENT OU than the bank
    # account that received the payment) forces conflict_exception
    # regardless of whether the amount would otherwise have been an exact
    # match or an acceptable short payment -- money in the wrong OU's
    # account needs re-routing BEFORE shortfall is even a relevant question.
    ou_status = resolve_ou_status(
        customer_name=customer_row.customer_name, bank_ou_number=r.ou_number, aging_map=aging_map,
    )
    if ou_status.is_cross_ou:
        target_total = round(sum(v["outstanding_amount"] for v in selected), 2)
        return {
            "customer_name": customer_row.customer_name,
            "customer_match_pct": score,
            "amount": amount,
            "selected_invoices": selected,
            "rule_id": "R14",
            "reason_code": "WRONG_OU_SPLIT_REQUIRED",
            "target_total": target_total,
            "shortfall_pct": None,
            "tag_message": (
                f"{customer_row.customer_name}'s invoice(s) sit in OU {ou_status.customer_ous} — "
                f"different from this bank account's OU ({r.ou_number}). Needs re-routing to the correct OU."
            ),
            "is_cross_ou_currency": True,
            "ou_evidence": {"customer_ous": ou_status.customer_ous, "bank_ou": ou_status.bank_ou,
                             "details": ou_status.customer_ou_details},
            **fx_fields,
        }

    # Build a throwaway, NOT-YET-PERSISTED LineItem purely to feed
    # _classify()/_received_total() the same shape they already expect
    # (they only ever read plain attributes -- no DB id needed). This is
    # the actual child eventually created in confirm_distribution(); here
    # it's just a classification probe -- built with THIS entry's fresh
    # fx_fields, not the parent's.
    copied = {f: getattr(r, f) for f in _COPIED_FIELDS if f not in fx_fields}
    probe = LineItem(**copied, **fx_fields, credit_amount=amount)
    classification = _classify(probe, selected)

    if classification.get("error"):
        return {"error": "fx_mismatch", "message": classification["error"]}
    if not classification.get("qualifies"):
        return {
            "error": "does_not_qualify",
            "message": (
                f"{customer_row.customer_name}'s amount ({amount:,.2f}) doesn't qualify against the selected "
                f"invoice(s) ({classification.get('reason_code')}) — {classification.get('message')}"
            ),
        }

    return {
        "customer_name": customer_row.customer_name,
        "customer_match_pct": score,
        "amount": amount,
        "selected_invoices": selected,
        "rule_id": classification["rule_id"],
        "reason_code": classification["reason_code"],
        "target_total": classification["target_total"],
        "shortfall_pct": classification.get("shortfall_pct"),
        "tag_message": classification.get("message"),
        "is_cross_ou_currency": False,
        "ou_evidence": None,
        **fx_fields,
    }


def _validate_entries(db: Session, r: LineItem, entries: list[dict]) -> dict | None:
    """Whole-breakup validation shared by preview and confirm: at least one
    entry, and amounts must sum to EXACTLY the parent's credited amount
    (confirmed requirement — no partial breakups). Per-entry validation
    (customer, invoices, classification) happens in _resolve_entry()."""
    if not entries:
        return {"error": "no_entries", "message": "Add at least one customer to the breakup."}

    total = round(sum(float(e.get("amount") or 0) for e in entries), 2)
    target = round(float(r.credit_amount or 0), 2)
    if abs(total - target) > DUPLICATE_TOLERANCE:
        return {
            "error": "does_not_add_up",
            "message": (
                f"Breakup totals {total:,.2f} but the credited amount is {target:,.2f} "
                f"— these must match exactly before this can be confirmed."
            ),
            "total": total, "target": target,
        }
    return None


def preview_distribution(db: Session, line_item_id: int, entries: list[dict]) -> dict:
    """Validates a breakup WITHOUT persisting anything — resolves and
    classifies every entry so the frontend can show which tag each
    customer's share would get before Confirm is clicked."""
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "no_aging_report", "message": "No aging report is currently loaded."}

    whole_error = _validate_entries(db, r, entries)
    if whole_error:
        return whole_error

    resolved = []
    for e in entries:
        result = _resolve_entry(db, aging_map, r, e)
        if result.get("error"):
            return result
        resolved.append(result)

    return {"valid": True, "entries": resolved}


def confirm_distribution(db: Session, line_item_id: int, entries: list[dict], triggered_by: str) -> dict:
    """
    Re-validates (never trust the client's earlier preview) and, if every
    entry resolves cleanly, creates one CHILD LineItem per entry — each
    tagged with its own R9a/R9b/R9d classification (see _resolve_entry())
    and given its own Oracle receipt immediately (mirrors what Step 4.5
    does for an ordinary row). The PARENT row is marked `distributed`
    (terminal) and never gets a receipt of its own. All-or-nothing: if any
    single entry fails to resolve, NOTHING is created.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "not found"}
    if r.current_state and r.current_state.value == "distributed":
        return {"error": "already_distributed", "message": f"Row {r.id} has already been distributed."}

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "no_aging_report", "message": "No aging report is currently loaded."}

    whole_error = _validate_entries(db, r, entries)
    if whole_error:
        return whole_error

    resolved = []
    for e in entries:
        result = _resolve_entry(db, aging_map, r, e)
        if result.get("error"):
            return result   # all-or-nothing -- stop at the first problem, create nothing
        resolved.append(result)

    created_children: list[dict] = []
    for entry in resolved:
        matched_invoices = [{
            "invoice_number": iv["invoice_number"], "outstanding_amount": iv["outstanding_amount"],
            "stated_amount": iv["outstanding_amount"], "customer_name": entry["customer_name"],
            "ou_number": r.ou_number, "invoice_currency": iv["currency"],
        } for iv in entry["selected_invoices"]]

        fx_field_names = ["invoice_currency", "is_cross_currency", "fx_credit_to_invoice",
                          "fx_credit_to_invoice_source", "is_cross_ledger", "fx_invoice_to_functional",
                          "fx_invoice_to_functional_source"]
        copied = {f: getattr(r, f) for f in _COPIED_FIELDS if f not in fx_field_names}

        child = LineItem(
            run_id=r.run_id,
            parent_line_item_id=r.id,
            **copied,
            invoice_currency=entry["invoice_currency"],
            is_cross_currency=entry["is_cross_currency"],
            fx_credit_to_invoice=entry["fx_credit_to_invoice"],
            fx_credit_to_invoice_source=entry["fx_credit_to_invoice_source"],
            is_cross_ledger=entry["is_cross_ledger"],
            fx_invoice_to_functional=entry["fx_invoice_to_functional"],
            fx_invoice_to_functional_source=entry["fx_invoice_to_functional_source"],
            is_cross_ou_currency=entry["is_cross_ou_currency"],
            ou_evidence=entry["ou_evidence"],
            credit_amount=entry["amount"],
            customer_name=entry["customer_name"],
            extracted_customer_name=entry["customer_name"],
            customer_match_pct=entry["customer_match_pct"],
            extraction_method="split_and_map",
            matched_invoices=matched_invoices,
            target_total=entry["target_total"],
            shortfall_pct=entry.get("shortfall_pct"),
            rule_id=entry["rule_id"],
            reason_code=entry["reason_code"],
            current_state="review_approve",
            status="Review & Approve",
            is_matched=True,
            # PATCH: was hardcoded True regardless of outcome -- a cross-OU
            # (R14) child needs SPOC re-routing, same as any other
            # conflict_exception row, and must not be marked as having
            # "passed validation" just because it was successfully matched
            # to a customer and invoice.
            passed_validation=entry["rule_id"] != "R14",
            created_at=dt.datetime.utcnow(),
            updated_at=dt.datetime.utcnow(),
            version=1,
        )
        db.add(child)
        db.flush()   # need child.id before creating its receipt / recording the ledger

        record_application(db, child, status="pending")
        receipt_result = create_receipt_for_line_item(db, child)

        created_children.append({
            "id": child.id,
            "customer_name": child.customer_name,
            "amount": entry["amount"],
            "invoice_numbers": [iv["invoice_number"] for iv in entry["selected_invoices"]],
            "rule_id": entry["rule_id"],
            "reason_code": entry["reason_code"],
            "receipt_created": bool(receipt_result.get("success")),
            "standard_receipt_id": child.standard_receipt_id,
        })

    r.current_state = "distributed"
    r.status = "Distributed"
    r.version = (r.version or 0) + 1

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="needs_distribution", to_state="distributed",
        trigger="spoc_confirm_distribution", rule_id=r.rule_id,
        triggered_by=triggered_by,
        comment=f"Split into {len(created_children)} customer(s): " +
                ", ".join(f"{c['customer_name']} ({c['amount']:,.2f}, {c['reason_code']})" for c in created_children),
    ))
    db.commit()

    return {
        "id": r.id,
        "success": True,
        "children": created_children,
        "message": f"Distributed into {len(created_children)} receipt(s) — review and Approve & Post each one.",
    }