"""
app.hitl.split_and_map
=========================
The "Path B" screen for needs_distribution rows (credit card / cheque /
third-party provider) — a genuinely multi-customer consolidated bank line
gets broken up here into a per-invoice breakdown stored directly on the
PARENT row (LineItem.distribution_breakdown) -- NOT separate child
LineItem rows. "Path A" (the identified name IS the actual paying
customer, not a broker) is handled elsewhere — see hitl/service.py's
override_settlement_as_customer_payment().

Design (confirmed choices):
  - Each SELECTED INVOICE gets its own amount entered against it — NOT one
    blended amount per customer classified against their invoices' combined
    total. A customer can have one invoice resolve as an exact match and
    another resolve as a short payment in the same breakup; each invoice
    is classified independently.
  - The breakup must sum to EXACTLY the parent row's credited amount before
    Confirm is allowed — no partial/leave-a-remainder option. That sum is
    across every invoice of every customer, not one number per customer.
  - Every customer must have at least one ACTIVE invoice selected (an
    invoice with real remaining balance -- not already fully claimed by
    another row's mapping) -- there's nothing to classify an amount against
    without one.
  - Each invoice is classified with the SAME rule_id/reason_code the rest of
    the app already uses for "amount vs. one invoice" (R9a exact match /
    R9b short payment within tolerance / R9d short payment beyond
    tolerance, manually confirmed -- never an overpayment). This reuses
    hitl/manual_mapping.py's _classify() and _received_total() directly,
    rather than re-implementing the same shortfall math a second time.
  - Every selected invoice still goes through the SAME duplicate-invoice
    check as manual mapping / automatic matching (rule_engine/
    invoice_ledger.py) — a distribution entry is not a shortcut around that.
  - PATCH (confirmed direction change): NO child LineItem rows are created
    at all anymore -- creating one row per invoice was inflating
    total_rows/total_credit_amount (the parent's full amount AND every
    child's share both counted), cluttering Analysis History with rows
    that don't represent real independent bank lines, and copying stale
    parent-level fields (settlement_type, etc.) onto children that no
    longer applied to them. Confirm now just resolves and classifies each
    invoice (same as before) and writes the result straight onto the
    PARENT's distribution_breakdown -- Approve & Post / Reject / Edit GL
    Rate per entry all happen from there via hitl/distribution_actions.py,
    which reuses the exact same Oracle payload/classification logic
    against a throwaway (never persisted) LineItem built from each entry,
    instead of a real child row. See that module for the entry action
    functions themselves -- this file only builds the breakdown.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..db.models import LineItem, RowStatusHistory, SettlementIdentifier, SettlementIdentifierType
from ..aging import aging_store
from ..rule_engine.invoice_ledger import get_applied_total, record_application_for_entry
from ..rule_engine.ou_resolver import resolve_ou_status
from ..rule_engine.fx_service import FxService
from ..rule_engine.evaluator import DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT
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
        "short_payment_tolerance_pct": DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT,
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

    credited_currency = (r.statement_currency or "").upper().strip()
    fx_service = FxService()
    rate_cache: dict[str, tuple] = {}   # one lookup per distinct invoice currency, not per invoice

    active = []
    for iv in aging_map.invoices_for_customer(customer_row.customer_name):
        outstanding = float(iv.outstanding_amount)
        # PATCH (real bug, caught by direct question): must filter by the
        # BANK ROW's ou_number (r.ou_number), not the aging invoice's own
        # ou_number (iv.ou_number) -- InvoiceApplication.ou_number is always
        # written from the bank row (see record_application_for_entry() /
        # _resolve_entry()'s exclude-my-own-claim query at line ~228), and
        # those two OUs are legitimately different values for the same
        # invoice. Filtering by iv.ou_number here meant an existing claim
        # NEVER matched, so this picker always showed the full un-clawed-
        # back outstanding no matter what was already applied elsewhere.
        already_applied = get_applied_total(db, iv.invoice_number, r.ou_number)
        remaining = round(outstanding - already_applied, 2)
        if remaining > DUPLICATE_TOLERANCE:
            invoice_currency = (iv.invoice_currency or "").upper().strip()
            is_cross_currency = bool(invoice_currency) and bool(credited_currency) and invoice_currency != credited_currency
            fx_rate = None
            if is_cross_currency:
                if invoice_currency not in rate_cache:
                    rate_cache[invoice_currency] = fx_service.get_rate_with_source(
                        from_ccy=credited_currency, to_ccy=invoice_currency, rate_date=r.statement_date,
                    )
                fx_rate, _source = rate_cache[invoice_currency]
            active.append({
                "invoice_number": iv.invoice_number,
                "outstanding_amount": remaining,   # what's actually left to claim, not the raw aging figure
                "currency": iv.invoice_currency,
                "is_cross_currency": is_cross_currency,
                "fx_rate": fx_rate,   # credited_currency -> invoice currency; same lookup _resolve_entry() uses
            })

    return {"customer_name": customer_row.customer_name, "credited_currency": credited_currency, "invoices": active}


def _resolve_entry(db: Session, aging_map, r: LineItem, entry: dict) -> dict:
    """
    Shared per-customer resolution for preview and confirm: validates the
    customer once, then resolves and classifies EACH selected invoice
    independently — its own amount, its own R9a/R9b/R9d/overpayment tag
    (same _classify() used everywhere else in this app), its own FX
    fields. Returns either {"error": ...} or {"customer_name": ...,
    "customer_match_pct": ..., "invoices": [<one resolved dict per
    invoice>]} ready to build one child LineItem PER INVOICE from.
    """
    name = (entry.get("customer_name") or "").strip()
    if not name:
        return {"error": "missing_customer_name", "message": "Every row needs a customer selected."}

    customer_row, score = aging_map.fuzzy_customer(name, min_pct=40.0)
    if not customer_row:
        return {"error": "unknown_customer", "message": f"'{name}' does not match a customer in the aging report."}

    invoice_entries = entry.get("invoices") or []
    if not invoice_entries:
        return {
            "error": "no_invoice_selected",
            "message": f"{customer_row.customer_name} needs at least one invoice selected — there's nothing to "
                       f"classify an amount against otherwise.",
        }

    # ── Cross-OU takes ABSOLUTE precedence over amount classification ─────
    # Mirrors rule_engine/evaluator.py's R14 exactly: a genuine OU mismatch
    # is a customer-level fact (this customer's invoices sit in a DIFFERENT
    # OU than the bank account that received the payment) — checked ONCE
    # here, but forces conflict_exception on every one of their invoices in
    # this breakup, regardless of whether each amount would otherwise have
    # been an exact match or an acceptable short payment.
    ou_status = resolve_ou_status(
        customer_name=customer_row.customer_name, bank_ou_number=r.ou_number, aging_map=aging_map,
    )

    resolved_invoices = []
    for inv_entry in invoice_entries:
        inv_number = (inv_entry.get("invoice_number") or "").strip()
        if not inv_number:
            return {
                "error": "missing_invoice_number",
                "message": f"{customer_row.customer_name} has an invoice row with no invoice selected.",
            }

        match = next(
            (iv for iv in aging_map.invoices_for_customer(customer_row.customer_name)
             if iv.invoice_number.upper() == inv_number.upper()),
            None,
        )
        if match is None:
            return {"error": "invalid_invoice", "message": f"Invoice {inv_number} does not belong to {customer_row.customer_name}."}

        already_applied = get_applied_total(db, match.invoice_number, r.ou_number, exclude_line_item_id=r.id)
        remaining = round(float(match.outstanding_amount) - already_applied, 2)
        if remaining <= DUPLICATE_TOLERANCE:
            return {
                "error": "duplicate_invoice",
                "message": f"Invoice {inv_number} has no remaining balance left to claim — it's already fully "
                           f"applied elsewhere.",
            }

        amount = round(float(inv_entry.get("amount") or 0), 2)
        if amount <= 0:
            return {
                "error": "missing_amount",
                "message": f"Invoice {inv_number} ({customer_row.customer_name}) needs an amount entered against it.",
            }

        # PATCH (real bug, caught by direct question): the parent's
        # is_cross_ou_currency / is_cross_currency / is_cross_ledger / FX
        # rate fields were being copied VERBATIM onto every child via
        # _COPIED_FIELDS -- computed ONCE, against whatever single customer
        # the ORIGINAL extraction guessed before distribution even existed.
        # Each invoice here can have its own real currency -- these must be
        # recomputed fresh, per invoice, exactly the same way
        # rule_engine/orchestrator.py computes them for an ordinary row
        # (same functions: resolve_ou_status, FxService), never inherited
        # from the parent.
        invoice_currency = (match.invoice_currency or r.invoice_currency or r.statement_currency or "").upper().strip()
        credited_currency = (r.statement_currency or "").upper().strip()
        functional_currency = (r.functional_currency or "").upper().strip()

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

        if ou_status.is_cross_ou:
            resolved_invoices.append({
                "invoice_number": match.invoice_number,
                "currency": match.invoice_currency,
                "amount": amount,
                "rule_id": "R14",
                "reason_code": "WRONG_OU_SPLIT_REQUIRED",
                "target_total": remaining,
                "shortfall_pct": None,
                "tag_message": (
                    f"{customer_row.customer_name}'s invoice(s) sit in OU {ou_status.customer_ous} — "
                    f"different from this bank account's OU ({r.ou_number}). Needs re-routing to the correct OU."
                ),
                "is_cross_ou_currency": True,
                "ou_evidence": {"customer_ous": ou_status.customer_ous, "bank_ou": ou_status.bank_ou,
                                 "details": ou_status.customer_ou_details},
                **fx_fields,
            })
            continue

        # Build a throwaway, NOT-YET-PERSISTED LineItem purely to feed
        # _classify()/_received_total() the same shape they already expect
        # (they only ever read plain attributes -- no DB id needed). This
        # is the actual child eventually created in confirm_distribution();
        # here it's just a classification probe -- one per INVOICE now,
        # not one per customer.
        copied = {f: getattr(r, f) for f in _COPIED_FIELDS if f not in fx_fields}
        probe = LineItem(**copied, **fx_fields, credit_amount=amount)
        classification = _classify(probe, [{
            "invoice_number": match.invoice_number,
            "outstanding_amount": remaining,
            "currency": match.invoice_currency,
        }])

        if classification.get("error"):
            return {"error": "fx_mismatch", "message": classification["error"]}
        if not classification.get("qualifies"):
            return {
                "error": "does_not_qualify",
                "message": (
                    f"Invoice {inv_number} ({customer_row.customer_name}, amount {amount:,.2f}) doesn't qualify "
                    f"({classification.get('reason_code')}) — {classification.get('message')}"
                ),
            }

        resolved_invoices.append({
            "invoice_number": match.invoice_number,
            "currency": match.invoice_currency,
            "amount": amount,
            "rule_id": classification["rule_id"],
            "reason_code": classification["reason_code"],
            "target_total": classification["target_total"],
            "shortfall_pct": classification.get("shortfall_pct"),
            "tag_message": classification.get("message"),
            "is_cross_ou_currency": False,
            "ou_evidence": None,
            **fx_fields,
        })

    return {
        "customer_name": customer_row.customer_name,
        "customer_match_pct": score,
        "invoices": resolved_invoices,
    }


def _validate_entries(db: Session, r: LineItem, entries: list[dict]) -> dict | None:
    """Whole-breakup validation shared by preview and confirm: at least one
    entry, and every invoice's amount across every customer must sum to
    EXACTLY the parent's credited amount (confirmed requirement — no
    partial breakups). Per-invoice validation (customer, invoice,
    classification) happens in _resolve_entry()."""
    if not entries:
        return {"error": "no_entries", "message": "Add at least one customer to the breakup."}

    total = round(sum(
        float(inv.get("amount") or 0)
        for e in entries
        for inv in (e.get("invoices") or [])
    ), 2)
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
    entry resolves cleanly, writes the full per-invoice breakdown straight
    onto the PARENT row's distribution_breakdown — NO child LineItem rows
    are created. Each entry gets its own reserved InvoiceApplication claim
    (status="pending") via record_application_for_entry(), same duplicate
    protection as any other mapping, but nothing posts to Oracle yet —
    that happens per entry, on demand, via hitl/distribution_actions.py's
    approve_distribution_entry(). All-or-nothing: if any single entry
    fails to resolve, NOTHING is written.
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
            return result   # all-or-nothing -- stop at the first problem, write nothing
        resolved.append(result)

    breakdown: list[dict] = []
    for customer_entry in resolved:
        for inv in customer_entry["invoices"]:
            # entry_id is a short numeric string, unique within this
            # parent's breakdown -- see hitl/distribution_actions.py's
            # _pseudo_line_item_id() for why it needs to be numeric
            # (feeds Oracle's ReceiptNumber generation, which reads
            # line_item.id -- these entries have no real one).
            entry_id = str(len(breakdown))
            breakdown.append({
                "entry_id": entry_id,
                "customer_name": customer_entry["customer_name"],
                "customer_match_pct": customer_entry["customer_match_pct"],
                "invoice_number": inv["invoice_number"],
                "currency": inv["currency"],
                "amount": inv["amount"],
                "invoice_currency": inv["invoice_currency"],
                "is_cross_currency": inv["is_cross_currency"],
                "fx_credit_to_invoice": inv["fx_credit_to_invoice"],
                "fx_credit_to_invoice_source": inv["fx_credit_to_invoice_source"],
                "is_cross_ledger": inv["is_cross_ledger"],
                "fx_invoice_to_functional": inv["fx_invoice_to_functional"],
                "fx_invoice_to_functional_source": inv["fx_invoice_to_functional_source"],
                "is_cross_ou_currency": inv["is_cross_ou_currency"],
                "ou_evidence": inv["ou_evidence"],
                "target_total": inv["target_total"],
                "shortfall_pct": inv.get("shortfall_pct"),
                "rule_id": inv["rule_id"],
                "reason_code": inv["reason_code"],
                # PATCH: a cross-OU (R14) entry needs SPOC re-routing before
                # it can ever be approved -- same idea as passed_validation
                # on a normal row, just gating the entry-level Approve
                # action in distribution_actions.py instead.
                "passed_validation": inv["rule_id"] != "R14",
                "hitl_status": "pending",
                "oracle_post_status": None,
                "oracle_ref_no": None,
                "standard_receipt_id": None,
                "oracle_status_code": None,
                "post_message": None,
                "oracle_posted_at": None,
                "oracle_response_raw": None,
                "oracle_payload": None,
                "reference_status": None,
                "reference_payload": None,
                "reference_response_raw": None,
                "reference_message": None,
                "gl_rate_original": None,
                "gl_rate_edited_at": None,
                "gl_rate_edited_by": None,
                "gl_rate_edit_reason": None,
                "rejected_at": None,
                "rejected_by": None,
                "rejected_reason": None,
            })
            record_application_for_entry(
                db, parent_line_item_id=r.id, entry_id=entry_id,
                invoice_number=inv["invoice_number"], ou_number=r.ou_number,
                customer_name=customer_entry["customer_name"],
                applied_amount=inv["amount"], invoice_currency=inv["currency"],
                status="pending",
            )

    r.distribution_breakdown = breakdown
    flag_modified(r, "distribution_breakdown")
    r.current_state = "distributed"
    r.status = "Distributed"
    r.version = (r.version or 0) + 1

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="needs_distribution", to_state="distributed",
        trigger="spoc_confirm_distribution", rule_id=r.rule_id,
        triggered_by=triggered_by,
        comment=f"Split into {len(breakdown)} invoice(s) across {len(resolved)} customer(s): " +
                ", ".join(f"{e['customer_name']} / {e['invoice_number']} ({e['amount']:,.2f}, {e['reason_code']})"
                          for e in breakdown),
    ))
    db.commit()

    return {
        "id": r.id,
        "success": True,
        "breakdown": breakdown,
        "message": f"Distributed into {len(breakdown)} entries — approve & post each one from this row.",
    }