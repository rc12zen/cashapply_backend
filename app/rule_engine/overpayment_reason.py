"""
app.rule_engine.overpayment_reason
=====================================
Works out WHY a row looks overpaid, so R11 arrives explained instead of bare.

Background
----------
evaluator.py's R11 fires whenever the credited amount (converted into invoice
currency) exceeds the combined outstanding of the invoices it matched. Until
now that was the entire output: the row landed in Conflict / Exception carrying
the word "Overpayment" and nothing else, and the SPOC re-derived the cause by
hand against a several-thousand-row aging export, every single time.

This module runs immediately AFTER the row is persisted and classified, and
records a structured cause plus the evidence behind it. It is deliberately NOT
part of evaluator.py: the checks need the invoice-application ledger (a DB
query) and the full AgingMap (cross-OU search), and evaluator.py is a pure
function over its input dict by design.

Nothing here changes the row's category. Every overpayment still goes to a
human — this only decides what the human is told.

The causes, in the order they are tested
----------------------------------------
DUPLICATE_SUSPECT
    One of the matched invoices is already claimed (pending or confirmed) by a
    DIFFERENT bank row. The customer most likely paid the same invoice twice.
    BRD Scenario 9. Tested first because it is the only cause backed by a hard
    fact rather than an amount resemblance.

CROSS_OU_CANDIDATE
    The same customer account has open invoices in a DIFFERENT OU, and they
    come to roughly the excess. The payment landed in one entity's bank account
    but part of it belongs to another entity's books. BRD Scenario 13. The
    aging export spans every OU in one file, so this costs one dict lookup.

UNMATCHED_INVOICES_EXIST
    The customer has other open invoices in the SAME OU that this row did not
    match, and they could absorb the excess. Usually means the extraction
    simply missed one — the SPOC can fix it with Map Invoice and the row
    becomes an exact match.

FX_DIFFERENCE
    Cross-currency row, and the excess is small enough to be explained by our
    conversion rate differing slightly from the customer's. Reported last of
    the "explained" causes because it is the weakest signal — an amount
    resemblance with no corroborating document.

UNEXPLAINED
    None of the above. The honest answer, and the one that means "ask the
    customer for remittance advice" (BRD Scenario 3).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import InvoiceApplication, LineItem

# How close a candidate set has to come to the excess before it is worth
# putting in front of a SPOC as a possible explanation. Both bounds apply --
# the ratio keeps large amounts sensible, the absolute floor keeps small ones
# from being rejected by a ratio that is unforgiving at low values.
_MATCH_RATIO_TOLERANCE = 0.05     # within 5% of the excess
_MATCH_ABSOLUTE_FLOOR  = 1.00     # ...or within 1.00 of it outright

# An FX rate disagreement between us and the customer moves the converted
# amount by a small percentage, not a large one. Above this, a difference is
# too big to blame on the rate and the row deserves the honest UNEXPLAINED.
_FX_EXPLAINABLE_PCT = 3.0


def _close_enough(candidate_total: float, excess: float) -> bool:
    """Is `candidate_total` near enough to `excess` to be worth showing?"""
    if excess <= 0:
        return False
    diff = abs(candidate_total - excess)
    return diff <= _MATCH_ABSOLUTE_FLOOR or diff <= excess * _MATCH_RATIO_TOLERANCE


def _best_subset(invoices: list, excess: float, max_invoices: int = 4) -> tuple[list, float]:
    """
    Greedily pick the invoices that come closest to `excess` without a
    combinatorial search.

    Largest-first, taking an invoice only when it does not overshoot, is
    intentionally simple: this output is a HINT shown to a SPOC, never an
    automatic decision, so the cost of an imperfect subset is that the human
    reads a slightly worse suggestion. A full subset-sum over a customer with
    hundreds of open invoices would not be worth that.
    """
    chosen: list = []
    running = 0.0
    for inv in sorted(invoices, key=lambda v: v.outstanding_amount, reverse=True):
        if len(chosen) >= max_invoices:
            break
        if running + inv.outstanding_amount <= excess + _MATCH_ABSOLUTE_FLOOR:
            chosen.append(inv)
            running += inv.outstanding_amount
    return chosen, round(running, 2)


def _serialize(inv) -> dict:
    return {
        "invoice_number": inv.invoice_number,
        "outstanding_amount": inv.outstanding_amount,
        "invoice_currency": inv.invoice_currency,
        "ou_number": inv.ou_number,
        "customer_name": inv.customer_name,
    }


def diagnose_overpayment(db: Session, r: LineItem, aging_map) -> dict:
    """
    Returns {"reason": <code>, "evidence": {...}} for an R11 row.

    Never raises and never writes -- the caller persists the result. A
    diagnosis failing must never be able to fail the analysis run around it;
    an undiagnosed overpayment is exactly as safe as one today, just less
    helpful.
    """
    matched = r.matched_invoices or []
    target = float(r.target_total or 0)
    # received is not persisted on the row -- it is target plus the excess
    # implied by shortfall_pct, which IS persisted. Deriving it here keeps this
    # module from having to re-run the FX legs.
    shortfall_pct = float(r.shortfall_pct or 0)
    received = round(target * (1 - shortfall_pct / 100), 2) if target else 0.0
    excess = round(received - target, 2)

    evidence: dict = {
        "target_total": target,
        "received_total": received,
        "excess_amount": excess,
        "invoice_currency": r.invoice_currency,
    }

    if excess <= 0:
        # Defensive: only ever called for R11, where excess is positive.
        return {"reason": "UNEXPLAINED", "evidence": evidence}

    # ── 1. DUPLICATE_SUSPECT ─────────────────────────────────────────────────
    numbers = [m.get("invoice_number") for m in matched if m.get("invoice_number")]
    if numbers:
        claimed = (
            db.query(InvoiceApplication)
            .filter(
                InvoiceApplication.invoice_number.in_(numbers),
                InvoiceApplication.status.in_(("pending", "confirmed")),
                InvoiceApplication.line_item_id != r.id,
            )
            .all()
        )
        if claimed:
            evidence["claimed_by"] = [
                {
                    "invoice_number": c.invoice_number,
                    "line_item_id": c.line_item_id,
                    "applied_amount": float(c.applied_amount or 0),
                    "status": c.status,
                }
                for c in claimed
            ]
            return {"reason": "DUPLICATE_SUSPECT", "evidence": evidence}

    customer_number = next(
        (m.get("customer_number") for m in matched if m.get("customer_number")), None
    )
    row_ou = next((m.get("ou_number") for m in matched if m.get("ou_number")), None)
    currency = (r.invoice_currency or "").upper().strip()

    # ── 2. CROSS_OU_CANDIDATE ────────────────────────────────────────────────
    if customer_number and aging_map is not None:
        other_ou = [
            v for v in aging_map.invoices_for_customer_number(customer_number, exclude_ou=row_ou)
            # Same-currency only. Comparing an excess in one currency against an
            # outstanding in another would produce a coincidence, not evidence.
            if not currency or (v.invoice_currency or "").upper().strip() == currency
        ]
        if other_ou:
            subset, subtotal = _best_subset(other_ou, excess)
            if subset and _close_enough(subtotal, excess):
                evidence["cross_ou_candidates"] = [_serialize(v) for v in subset]
                evidence["cross_ou_candidate_total"] = subtotal
                evidence["cross_ou_numbers"] = sorted({v.ou_number for v in subset})
                return {"reason": "CROSS_OU_CANDIDATE", "evidence": evidence}

    # ── 3. UNMATCHED_INVOICES_EXIST ──────────────────────────────────────────
    if customer_number and aging_map is not None:
        already = {str(n).strip().upper() for n in numbers}
        same_ou = [
            v for v in aging_map.invoices_for_customer_number(customer_number)
            if v.invoice_number.strip().upper() not in already
            and (not row_ou or v.ou_number == row_ou)
            and (not currency or (v.invoice_currency or "").upper().strip() == currency)
        ]
        if same_ou:
            subset, subtotal = _best_subset(same_ou, excess)
            if subset and _close_enough(subtotal, excess):
                evidence["unmatched_candidates"] = [_serialize(v) for v in subset]
                evidence["unmatched_candidate_total"] = subtotal
                return {"reason": "UNMATCHED_INVOICES_EXIST", "evidence": evidence}
            # Other invoices exist but none of them explains the excess. Still
            # worth telling the SPOC they exist -- it is the first thing they
            # would otherwise go and check by hand.
            evidence["other_open_invoice_count"] = len(same_ou)
            evidence["other_open_invoice_total"] = round(
                sum(v.outstanding_amount for v in same_ou), 2
            )

    # ── 4. FX_DIFFERENCE ─────────────────────────────────────────────────────
    if r.is_cross_currency and abs(shortfall_pct) <= _FX_EXPLAINABLE_PCT:
        evidence["fx_credit_to_invoice"] = (
            float(r.fx_credit_to_invoice) if r.fx_credit_to_invoice else None
        )
        evidence["fx_credit_to_invoice_source"] = r.fx_credit_to_invoice_source
        evidence["credited_currency"] = r.statement_currency
        return {"reason": "FX_DIFFERENCE", "evidence": evidence}

    return {"reason": "UNEXPLAINED", "evidence": evidence}


def apply_overpayment_diagnosis(db: Session, r: LineItem, aging_map) -> None:
    """
    Diagnose and stamp the row in place. Caller commits.

    Swallows everything: this is advisory metadata bolted onto a row that is
    already correctly classified and already safely blocked from posting. It
    must never be able to take an analysis run down with it.
    """
    if r.rule_id != "R11":
        return
    try:
        result = diagnose_overpayment(db, r, aging_map)
        r.overpayment_reason = result["reason"]
        r.overpayment_evidence = result["evidence"]
    except Exception:
        r.overpayment_reason = "UNEXPLAINED"
        r.overpayment_evidence = None
