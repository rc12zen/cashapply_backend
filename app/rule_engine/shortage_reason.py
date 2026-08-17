"""
app.rule_engine.shortage_reason
==================================
Works out WHY a row came up short, so R9c arrives explained instead of bare.

The mirror image of overpayment_reason.py, and deliberately built to the
same shape: same call site, same advisory-only contract, same two columns
(a reason code plus structured evidence), same "never able to fail a run"
guarantee. Read that module first if this is unfamiliar — everything
structural about it applies here.

Background
----------
evaluator.py's R9c fires when the credited amount falls short of the
invoices it matched. Two roads lead there now:

  1. The shortfall exceeded the tolerance. Always did.
  2. NEW — the shortfall was WITHIN tolerance, but the customer holds open
     credit memos, so it is no longer auto-accepted. Before this existed
     the row was passed silently under the 12% rule, nobody looked at it,
     and the credit memo stayed open in Oracle for the customer to deduct a
     second time next month.

Case 2 is the whole reason this module exists. A row held back for review
needs to arrive saying WHICH credit memos the customer holds and whether
one of them matches the shortfall exactly — otherwise the SPOC is doing
the same manual hunt through a several-thousand-row export that the change
was meant to remove.

Like overpayment_reason.py this runs AFTER the row is persisted, not inside
evaluator.py, because it needs the full AgingMap and evaluator.py is a pure
function over its input dict by design.

Nothing here changes the row's category. Every shortage still goes to a
human — this only decides what the human is told.

The causes, in the order they are tested
----------------------------------------
CREDIT_MEMO_EXACT_MATCH
    Exactly ONE open credit memo equals the shortfall to the cent. The
    strongest signal available without a remittance, and the case Finance
    described directly: the customer knew about the credit memo and
    deducted it before paying.

CREDIT_MEMO_AMBIGUOUS
    Several credit memos equal the shortfall exactly. Reported as ambiguous
    rather than resolved by picking one — arbitrarily choosing between
    identical candidates would be a guess wearing the costume of a fact.

CREDIT_MEMO_AVAILABLE
    The customer holds open credit memos but none matches the shortfall.
    Deliberately does NOT search for a combination that adds up. Assurant
    carries 164 open credit memos in a single OU; some subset of 164
    numbers fits almost any target, so a combination search would
    manufacture a confident wrong explanation that somebody then approves.
    Listing what exists and letting a human judge is the honest output.

DEDUCTION_STATED
    The remittance itself declared a per-line deduction (TDS, withholding,
    bank charges — BRD Scenario 4(a), 10, 11) that accounts for the gap. No
    credit memo needed to explain it.

SHORTAGE_UNEXPLAINED
    None of the above. The honest answer, and the one that means "ask the
    customer for remittance advice" (BRD Scenario 3).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import LineItem

# How close a stated deduction has to come to the shortfall before it counts
# as explaining it. Deliberately tight — this is an amount resemblance, not
# a document, so it should only fire when the numbers genuinely agree.
_DEDUCTION_TOLERANCE_PCT = 2.0

# Credit memos are only ever offered as an explanation when they match the
# shortfall EXACTLY. No tolerance band, because unlike an FX difference
# there is no reason a real deduction would be a few cents out — and a loose
# band across 164 candidates would match something almost every time.


def _shortfall_amount(r: LineItem) -> float | None:
    """
    How much the row is short, in invoice currency.

    From target_total x shortfall_pct rather than
    target_total - credit_amount: credit_amount is in the CREDITED currency
    and target_total is in the INVOICE currency, so subtracting them
    directly would silently mix units on any cross-currency row.
    shortfall_pct is the already-reconciled figure the rule engine produced.
    """
    if r.target_total is None or r.shortfall_pct is None:
        return None
    if float(r.shortfall_pct) <= 0:
        return None
    return round(float(r.target_total) * float(r.shortfall_pct) / 100.0, 2)


def diagnose_shortage(db: Session, r: LineItem, aging_map) -> dict:
    """
    Returns {"reason": str, "evidence": dict}. Pure read — no writes here.
    """
    shortfall = _shortfall_amount(r)
    matched = r.matched_invoices or []
    first = matched[0] if matched else {}

    evidence: dict = {
        "shortfall_amount": shortfall,
        "shortfall_pct": round(float(r.shortfall_pct), 2) if r.shortfall_pct is not None else None,
        "target_total": float(r.target_total) if r.target_total is not None else None,
        "currency": first.get("invoice_currency"),
        # The aging export is fully replaced daily with no history kept, so
        # every credit memo named below is a snapshot of one file rather
        # than a standing fact. Stamped so a stale follow-up is recognisable
        # as stale instead of being trusted weeks later.
        "aging_snapshot": None,
    }
    try:
        from ..aging import aging_store
        status = aging_store.get_status()
        evidence["aging_snapshot"] = {
            "filename": status.get("filename"),
            "loaded_at": status.get("loaded_at"),
        }
    except Exception:  # noqa: BLE001 — provenance is nice to have, never required
        pass

    # ── 1/2/3. Credit memos held by this customer ────────────────────────
    credit_memos = aging_map.credit_memos_for(
        customer_number=first.get("customer_number"),
        customer_name=first.get("customer_name"),
        ou_number=first.get("ou_number") or r.ou_number,
        currency=first.get("invoice_currency"),
    )
    if credit_memos:
        evidence["credit_memo_count"] = len(credit_memos)
        evidence["credit_memo_total"] = round(sum(c.amount for c in credit_memos), 2)
        # Capped: this is persisted as JSON on the row, so it must not grow
        # with the size of a customer's credit book. Largest first, which is
        # both the useful order and a stable one.
        evidence["credit_memos"] = [
            {
                "document_number": c.document_number,
                "amount": c.amount,
                "currency": c.currency,
                "document_date": c.document_date,
                "description": c.description,
            }
            for c in credit_memos[:25]
        ]
        evidence["credit_memos_truncated"] = len(credit_memos) > 25

        if shortfall is not None:
            exact = [c for c in credit_memos if round(c.amount, 2) == shortfall]
            if len(exact) == 1:
                evidence["matched_document_number"] = exact[0].document_number
                evidence["matched_description"] = exact[0].description
                return {"reason": "CREDIT_MEMO_EXACT_MATCH", "evidence": evidence}
            if len(exact) > 1:
                evidence["candidate_document_numbers"] = [c.document_number for c in exact]
                return {"reason": "CREDIT_MEMO_AMBIGUOUS", "evidence": evidence}

        # Credit memos exist but none matches. Say so plainly rather than
        # hunting for a combination — see the module docstring.
        return {"reason": "CREDIT_MEMO_AVAILABLE", "evidence": evidence}

    # ── 4. A deduction the remittance itself declared ────────────────────
    # deduction_amount is carried onto every matched invoice from the
    # remittance's amount_withheld (evaluator.py's _resolve_matched_invoices)
    # but has never been read anywhere. If the customer told us what they
    # withheld and it accounts for the gap, that IS the explanation.
    stated = sum(
        float(m.get("deduction_amount") or 0)
        for m in matched
        if m.get("deduction_amount")
    )
    if stated > 0 and shortfall:
        variance_pct = abs(stated - shortfall) / shortfall * 100
        if variance_pct <= _DEDUCTION_TOLERANCE_PCT:
            evidence["stated_deduction_total"] = round(stated, 2)
            return {"reason": "DEDUCTION_STATED", "evidence": evidence}
        evidence["stated_deduction_total"] = round(stated, 2)
        evidence["stated_deduction_variance_pct"] = round(variance_pct, 2)

    return {"reason": "SHORTAGE_UNEXPLAINED", "evidence": evidence}


def apply_shortage_diagnosis(db: Session, r: LineItem, aging_map) -> None:
    """
    Diagnose and stamp the row in place. Caller commits.

    Swallows everything, for the same reason apply_overpayment_diagnosis()
    does: this is advisory metadata bolted onto a row that is already
    correctly classified and already safely blocked from posting. It must
    never be able to take an analysis run down with it.
    """
    if r.rule_id != "R9c":
        return
    try:
        result = diagnose_shortage(db, r, aging_map)
        r.shortage_reason = result["reason"]
        r.shortage_evidence = result["evidence"]
    except Exception:  # noqa: BLE001
        r.shortage_reason = "SHORTAGE_UNEXPLAINED"
        r.shortage_evidence = None
