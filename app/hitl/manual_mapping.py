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
from typing import Optional

from sqlalchemy.orm import Session

from ..db.models import LineItem, RowStatusHistory, User
from ..aging import aging_store
from ..aging.aging_map import KIND_CREDIT_MEMO, KIND_UNAPPLIED_RECEIPT
from ..rule_engine.evaluator import DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT
from ..rule_engine.fx_service import FxService
from ..rule_engine.invoice_ledger import check_duplicate, record_application
from ..rule_engine.remittance_lookup import build_remittance_view


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


def _serialize_credit(c) -> dict:
    """One CreditMemoView -> the shape the mapping card renders."""
    return {
        "document_number": c.document_number,
        "amount": c.amount,             # positive magnitude; source row is negative
        "currency": c.currency,
        "ou_number": c.ou_number,
        "customer_name": c.customer_name,
        "customer_number": c.customer_number,
        "kind": c.kind,                 # "credit_memo" | "unapplied_receipt"
        "description": c.description,   # "C-Worker Program Rebate ..." — blank on receipts
        "document_date": c.document_date,
        "document_type": c.document_type,
    }


def _shortfall_amount(r: LineItem) -> Optional[float]:
    """
    How much this row is short, in invoice currency.

    Derived from target_total x shortfall_pct rather than
    target_total - credit_amount on purpose: credit_amount is in the
    CREDITED currency, target_total is in the INVOICE currency, and the two
    are only comparable after the FX leg the rule engine already resolved.
    shortfall_pct is that reconciled result, so this needs no FX of its own
    and cannot silently subtract two different units.

    None when the row isn't short (or was never evaluated).
    """
    if r.target_total is None or r.shortfall_pct is None:
        return None
    if float(r.shortfall_pct) <= 0:
        return None
    return round(float(r.target_total) * float(r.shortfall_pct) / 100.0, 2)


def _credit_context(aging_map, r: LineItem, customer_name: Optional[str]) -> dict:
    """
    The customer's negative aging rows, plus which of the three situations
    the SPOC is looking at.

    Scoped to customer + OU + currency — of the 164 credit memos in the
    31-Mar export that name a specific invoice, all 164 agree with it on all
    three, and BRD Scenario 13 keeps money out of the wrong OU rather than
    applying it across OUs.

    SUGGESTION POLICY — a single exact amount match, and nothing else.
    Never a sum of several. Assurant holds 164 open credit memos in one OU,
    and some subset of 164 numbers fits almost any target, so combination
    search would reliably produce a confident wrong answer that somebody
    then approves. Two credit memos of the same amount is real ambiguity and
    is reported as such rather than resolved by picking one.
    """
    matched = r.matched_invoices or []
    first = matched[0] if matched else {}
    customer_number = first.get("customer_number") or None
    ou_number = first.get("ou_number") or r.ou_number or None
    currency = first.get("invoice_currency") or None

    credit_memos = aging_map.credit_memos_for(
        customer_number=customer_number, customer_name=customer_name,
        ou_number=ou_number, currency=currency, kind=KIND_CREDIT_MEMO,
    )
    unapplied = aging_map.credit_memos_for(
        customer_number=customer_number, customer_name=customer_name,
        ou_number=ou_number, currency=currency, kind=KIND_UNAPPLIED_RECEIPT,
    )

    shortfall = _shortfall_amount(r)
    suggested = None
    if shortfall is not None:
        exact = [c for c in credit_memos if round(c.amount, 2) == shortfall]
        if len(exact) == 1:
            situation = "exact_match"
            suggested = exact[0].document_number
        elif len(exact) > 1:
            situation = "ambiguous_match"
        elif credit_memos:
            situation = "credits_available"
        else:
            situation = "none"
    else:
        situation = "credits_available" if credit_memos else "none"

    return {
        "credit_memos": [_serialize_credit(c) for c in credit_memos],
        # Shown so the SPOC can see them, never suggested and never counted
        # towards a match: per Finance, nobody knows when a customer will
        # come back to an unapplied receipt, so it must not drive a decision.
        "unapplied_receipts": [_serialize_credit(c) for c in unapplied],
        "credit_context": {
            "situation": situation,
            "shortfall_amount": shortfall,
            "currency": currency,
            "credit_memo_total": round(sum(c.amount for c in credit_memos), 2),
            "credit_memo_count": len(credit_memos),
            "unapplied_receipt_count": len(unapplied),
            "suggested_document_number": suggested,
            # The aging export is fully replaced daily with no history, so
            # everything above is a snapshot of one file, not a standing
            # fact. Surfaced so the card can say which file it came from
            # rather than implying it is live.
            "aging_filename": aging_store.get_status().get("filename"),
            "aging_loaded_at": aging_store.get_status().get("loaded_at"),
        },
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
            # Negative aging rows for the same customer/OU/currency. Purely
            # additive to this payload — a client that ignores these keys
            # behaves exactly as before.
            **_credit_context(aging_map, r, customer_name),
        }

    return {
        "customer_identified": False,
        "customer_name": None,
        "customers": aging_map.customers_for_ou(r.ou_number, limit=500) if r.ou_number else [],
        "invoices": [],
        # No customer yet, so nothing to scope a credit lookup to. Emitted
        # empty rather than omitted so the client has one payload shape.
        **_credit_context(aging_map, r, None),
    }


def get_invoices_for_customer(db: Session, line_item_id: int, customer_name: str) -> dict:
    """Step 2 of the picker when no customer was auto-identified — SPOC
    searched/picked a customer, now fetch THEIR open invoices."""
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}
    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {"error": "No aging report is currently loaded — load one before mapping invoices."}
    return {
        "customer_name": customer_name,
        "invoices": [_serialize_invoice(v) for v in aging_map.invoices_for_customer(customer_name)],
        # Same credit context as get_mapping_options, now that a customer is
        # known. Without this the SPOC would see credit memos on the
        # auto-identified path but not after picking a customer by hand.
        **_credit_context(aging_map, r, customer_name),
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

    # Mirrors evaluator.py's R12 guard. `0.0 if target_total == 0` used to fall
    # through to the R9a branch below, so a selection carrying no payable
    # balance was reported to the SPOC as an EXACT MATCH. The aging picker now
    # only ever offers payable invoices (aging/aging_map.py's is_payable), so
    # this is the backstop, not the everyday path.
    if target_total <= 0:
        return {
            "target_total": target_total, "received_total": received_total, "shortfall_pct": 0.0,
            "fx_info": fx_info,
            "qualifies": False, "tag": None, "rule_id": "R12", "reason_code": "NO_PAYABLE_BALANCE",
            "message": (
                f"The selected document(s) carry a combined payable balance of {target_total} — "
                f"there is nothing outstanding to apply {received_total} against."
            ),
        }

    shortfall_pct = round((target_total - received_total) / target_total * 100, 2)

    base = {
        "target_total": target_total, "received_total": received_total, "shortfall_pct": shortfall_pct,
        "fx_info": fx_info,
    }

    if shortfall_pct < 0:
        # PATCH (business decision, confirmed): an overpayment used to be a dead
        # end here exactly as a beyond-tolerance shortfall once was — see the
        # R9d note further down, which loosened the OTHER side of this same
        # boundary and explicitly left this one closed.
        #
        # It is now allowed, because refusing it settled nothing: the invoices
        # the customer genuinely DID pay stayed open in the aging while their
        # cash sat in Oracle, and the only action that could actually close the
        # row was Reject — which records something that did not happen.
        #
        # Over-applying is impossible here, and not because of a check that
        # could be bypassed. confirm_manual_mapping() stamps every selected
        # invoice with `stated_amount = outstanding_amount`, and
        # oracle/fusion_client.py's build_remittance_reference_payloads() sends
        # exactly that as each ReferenceAmount — so each reference is capped at
        # its own invoice's balance by construction. The difference simply stays
        # unapplied on the receipt, which is the state every conflict row's
        # receipt is already in today.
        #
        # This is the MANUAL path only. The automatic matcher must keep landing
        # on R11: evaluator.py's _resolve_matched_invoices() assigns a single
        # invoice the whole effective_received rather than its own outstanding,
        # so the same move there would over-apply.
        excess = round(received_total - target_total, 2)
        ccy = fx_info["selected_currency"] or ""
        return {
            **base, "qualifies": True, "tag": "overpayment_capped",
            "rule_id": "R9e", "reason_code": "OVERPAYMENT_CAPPED",
            "excess_amount": excess,
            # The front end must collect a disposition before confirming — the
            # SPOC knows what the unapplied amount is at the moment they pick the
            # invoices, and making them come back for a second action loses it.
            "requires_disposition": True,
            "message": (
                f"Received {received_total} {ccy} against selected invoice(s) totalling "
                f"{target_total} {ccy}. Each invoice is applied at its own outstanding amount, "
                f"so {target_total} {ccy} will post and {excess} {ccy} will stay unapplied on "
                f"the receipt. Record why it stays unapplied to continue."
            ),
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

    # PATCH (business decision, confirmed): a shortfall beyond the auto
    # tolerance used to be a dead end here — qualifies=False, and
    # confirm_manual_mapping() refused to persist ANYTHING, so a SPOC could
    # never actually record "yes, this payment is against these invoices,
    # and yes, the rest is a genuine open shortage." That's now allowed:
    # ANY non-negative shortfall qualifies (posting goes through as a
    # partial receipt; the remaining balance stays open on the invoice for
    # collections, same as a partial payment would in Oracle itself).
    # shortfall_pct < 0 (overpayment) is UNCHANGED and still never
    # qualifies here — see the overpayment branch above; "until it is not
    # over paid" is exactly that boundary.
    return {
        **base, "qualifies": True, "tag": "short_payment_recorded", "rule_id": "R9d",
        "reason_code": "SHORT_PAYMENT_RECORDED",
        "message": f"Shortage of {shortfall_pct}% exceeds the {DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT}% auto-tolerance, "
                   f"but is recorded as a genuine short payment against the selected invoice(s) — this mapping "
                   f"qualifies for Ready for Oracle. The remaining balance stays open for collections.",
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
    return preview_selection(db, r, invoice_numbers)


def preview_selection(db: Session, r: LineItem, invoice_numbers: list[str]) -> dict:
    """
    The whole of preview_manual_mapping() EXCEPT its row-lookup and
    hitl_status guard — extracted verbatim, no logic changed.

    Split out so hitl/reopen_with_edits.py can reuse this validation on a row
    that DOES still carry hitl_status == "rejected". That guard is correct for
    the manual-mapping card (never re-map underneath a recorded decision) but
    wrong for the reopen flow, whose entire purpose is to edit the mapping of a
    rejected row and clear that decision in the same action. The alternative
    was a second copy of the aging lookup, same-customer, same-currency and
    duplicate-claim checks, which would drift.

    Still read-only: persists nothing, and callers must not treat it as
    authoritative — confirm re-validates from scratch.
    """
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

    # PATCH: real duplicate-mapping protection -- see rule_engine/
    # invoice_ledger.py's module docstring for exactly what this replaces
    # (a permanent False stub). Checked per invoice BEFORE shortfall
    # classification, since it doesn't matter whether the selection would
    # otherwise be an exact match or a short payment -- if another row has
    # already claimed this invoice's outstanding amount, this selection is
    # blocked outright, not silently allowed to double up on it.
    for v in selected:
        dup = check_duplicate(
            db, v["invoice_number"], v["ou_number"],
            outstanding_amount=v["outstanding_amount"],
            new_amount=v["outstanding_amount"],
            exclude_line_item_id=r.id,
        )
        if dup["blocked"]:
            return {"error": dup["message"], "qualifies": False, "duplicate": dup}

    result = _classify(r, selected)
    result["matched_invoices_preview"] = selected
    return result


def confirm_manual_mapping(
    db: Session,
    line_item_id: int,
    invoice_numbers: list[str],
    user: User | None,
    overpayment_disposition: str | None = None,
    overpayment_comment: str | None = None,
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
    return apply_selection(
        db, r, invoice_numbers, user,
        overpayment_disposition=overpayment_disposition,
        overpayment_comment=overpayment_comment,
    )


def apply_selection(
    db: Session,
    r: LineItem,
    invoice_numbers: list[str],
    user: User | None,
    overpayment_disposition: str | None = None,
    overpayment_comment: str | None = None,
    commit: bool = True,
) -> dict:
    """
    The whole of confirm_manual_mapping() EXCEPT its row-lookup and
    hitl_status guard — extracted verbatim apart from the `commit` flag below.

    Split out for the same reason as preview_selection(): the reopen flow
    (hitl/reopen_with_edits.py) legitimately needs to write a new mapping onto a
    row that still carries hitl_status == "rejected", because it clears that
    rejection in the same transaction. Everything that makes this safe is
    unchanged — it still re-validates from scratch rather than trusting a
    client preview, still stamps stated_amount = outstanding_amount so Oracle
    references stay capped at each invoice's own balance, and still refuses an
    overpaid selection without a recorded disposition.

    `commit=False` lets the reopen flow stage this alongside its own field
    changes and commit once, so a failure part-way cannot leave a row mapped
    but still rejected.
    """
    line_item_id = r.id

    preview = preview_selection(db, r, invoice_numbers)
    if preview.get("error"):
        return preview
    if not preview["qualifies"]:
        return {"error": preview["message"], "qualifies": False}

    # An overpaid mapping (R9e) is only allowed to proceed WITH a recorded
    # reason for the excess -- see _classify()'s R9e branch. Enforced here and
    # not only in the UI, because this is the point where money becomes
    # postable: without it the row would go Processed carrying an unexplained
    # unapplied balance, which is exactly the silent-residual problem this
    # whole flow exists to avoid.
    is_overpaid = preview.get("rule_id") == "R9e"
    if is_overpaid and not (overpayment_disposition or "").strip():
        return {
            "error": (
                "This selection overpays the chosen invoice(s). Record why the remainder "
                "stays unapplied (duplicate payment, another entity, advance payment, or "
                "other) before confirming."
            ),
            "qualifies": False,
            "requires_disposition": True,
            "excess_amount": preview.get("excess_amount"),
        }

    selected = preview["matched_invoices_preview"]
    fx_info = preview.get("fx_info") or {}
    from_state = r.current_state

    # stated_amount = outstanding_amount is what caps each Oracle reference at
    # its own invoice's balance -- see fusion_client.build_remittance_reference_
    # payloads(), which sends `stated_amount or outstanding_amount` verbatim.
    # On an overpaid row this is the entire safety mechanism, so it must stay
    # exactly as it is: never the received amount, never a SPOC-typed figure.
    r.matched_invoices = [{**v, "stated_amount": v["outstanding_amount"]} for v in selected]
    r.target_total   = preview["target_total"]
    r.shortfall_pct  = preview["shortfall_pct"]
    r.rule_id        = preview["rule_id"]
    r.reason_code    = preview["reason_code"]
    r.is_matched     = True
    r.current_state  = "review_approve"  # same state automatic ready_for_oracle rows sit in, awaiting Approve
    r.status         = "Ready for Oracle (Manual Mapping)"

    if is_overpaid:
        # Recorded now, at the moment the SPOC actually knows what the excess
        # is. approve_row() computes the final unapplied_amount when the
        # references are posted; this is the WHY, not the HOW MUCH.
        r.overpayment_disposition    = overpayment_disposition.strip()
        r.overpayment_disposition_at = dt.datetime.utcnow()
        r.overpayment_disposition_by = user.email if user else None
        r.status = "Overpayment — Ready to Post"

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

    # FIX: Leg 2 (invoice -> functional, i.e. Oracle's ConversionRate) was
    # never revisited here -- it was frozen at analysis time against
    # whatever invoice_currency was known THEN (often just defaulted to
    # credited_currency when no invoice had matched yet, e.g. a
    # CUSTOMER_ONLY_NO_REMIT row). r.invoice_currency above can change to
    # the REAL selected invoice's currency, but is_cross_ledger /
    # fx_invoice_to_functional were left stale -- meaning a row could sit
    # in the DB claiming is_cross_ledger=False with a NULL rate even
    # though invoice_currency now genuinely differs from
    # functional_currency. build_receipt_creation_payload() recomputes
    # is_cross_ledger fresh from these two fields at payload-build time,
    # so a stale fx_invoice_to_functional there means posting fails with
    # "conversion rate is not resolved" even when the rate exists in
    # gl_daily_rates -- it just never got looked up. Recompute both here,
    # every time a mapping is (re)confirmed, so they always reflect the
    # invoice actually selected.
    functional_currency = (r.functional_currency or "").upper().strip()
    resolved_invoice_currency = (r.invoice_currency or "").upper().strip()
    r.is_cross_ledger = (
        bool(resolved_invoice_currency)
        and bool(functional_currency)
        and resolved_invoice_currency != functional_currency
    )
    if r.is_cross_ledger:
        fx_leg2 = FxService()
        leg2_rate, leg2_source = fx_leg2.get_rate_with_source(
            from_ccy=resolved_invoice_currency,
            to_ccy=functional_currency,
            rate_date=r.statement_date,
        )
        r.fx_invoice_to_functional = leg2_rate
        r.fx_invoice_to_functional_source = leg2_source
    else:
        r.fx_invoice_to_functional = None
        r.fx_invoice_to_functional_source = None

    # PATCH: this used to be a persistent record that THIS row's current
    # mapping came from a SPOC, not automatic extraction — see the
    # LineItem.manually_mapped field comment in db/models.py for why this
    # matters (previously the only trace was a RowStatusHistory log entry,
    # unreadable by the row-detail page, which is why the Manual Mapping
    # card couldn't tell "already mapped" apart from "needs mapping").
    r.manually_mapped    = True
    r.manually_mapped_at = dt.datetime.utcnow()
    r.manually_mapped_by = user.email if user else None

    # FIX: r.remittance_extraction_id was never revisited here — it was
    # frozen at analysis time against whatever customer the ORIGINAL
    # (possibly wrong) automatic match guessed. Once a SPOC manually
    # remaps this row to a different customer/invoice, the row-detail
    # remittance panel (bff/row_detail.py) kept showing that OLD, stale
    # email — including its sender address — because nothing here ever
    # re-ran the lookup for the customer actually selected. Recompute it
    # the same way orchestrator.py does on the automatic path: look up
    # remittance emails matching this row's amount/currency/date, and
    # prefer the one whose extracted payer name agrees with the NEWLY
    # selected customer (preview_selection() already guarantees every
    # selected invoice belongs to exactly one customer — see its
    # "different customers" guard above). Ends up None (no email) just as
    # correctly as it ends up with a real match, if this customer simply
    # has no matching email in the inbox.
    new_customer_name = selected[0].get("customer_name") if selected else None
    # aging_map re-fetched here rather than threaded through from
    # preview_selection() (which only returns its plain dict result, not
    # the AgingMap object it used internally) -- aging_store.get_aging_map()
    # is a cheap in-memory cache read, not a re-parse.
    remittance_view = build_remittance_view(db, r, new_customer_name, aging_map=aging_store.get_aging_map())
    r.remittance_extraction_id = remittance_view.get("extraction_id")

    history_comment = f"Manually mapped to invoice(s): {', '.join(invoice_numbers)}"
    if is_overpaid:
        history_comment += (
            f" | Overpayment — {preview.get('excess_amount')} left unapplied; "
            f"disposition={r.overpayment_disposition}"
        )
        if (overpayment_comment or "").strip():
            history_comment += f" | {overpayment_comment.strip()}"

    db.add(RowStatusHistory(
        line_item_id=r.id, from_state=from_state, to_state=r.current_state,
        trigger="manual_mapping", rule_id=r.rule_id,
        triggered_by=user.email if user else None,
        comment=history_comment,
    ))
    # PATCH: register this mapping in the ledger the moment it's confirmed
    # (status="pending", not yet "confirmed" -- that upgrade happens at
    # Approve, see hitl/service.py) -- so a SECOND row trying to map the
    # SAME invoice sees it as already claimed immediately, not only after
    # this row is later approved.
    record_application(db, r, status="pending")
    # commit=False when the reopen flow is staging this alongside its own
    # changes -- it commits once, so a mid-way failure can't leave a row
    # mapped but still rejected. Every other caller keeps committing here.
    if commit:
        db.commit()

    return {
        "success": True,
        "rule_id": r.rule_id,
        "reason_code": r.reason_code,
        "excess_amount": preview.get("excess_amount"),
        "overpayment_disposition": r.overpayment_disposition if is_overpaid else None,
        "message": preview["message"] + (
            " Row moved to Overpayment — Ready to Post; use Approve to post it."
            if is_overpaid else
            " Row moved to Ready for Oracle — use Approve to post it."
        ),
    }