"""
cashapply_shared.rule_engine
===============================
Table-driven rule engine. Implements the rule table agreed in design
discussions (R0..R15). Pure functions — no DB/IO here, so it's testable
in isolation and usable from both the FastAPI request path (App1) and
any offline batch re-evaluation script.

INPUT CONTRACT
--------------
RuleEngineInput = {
    original_row: { credit_amount, currency, narrative, bank_reference, ... },
    extraction:   { extracted_customer, extracted_invoices: [str],
                     customer_match_pct, invoice_match_pct },
    remittance:   { found: bool, invoices: [RemittanceInvoiceLine-like dicts],
                     customer_text: str|None, ambiguous: bool,
                     is_cross_currency: bool, remittance_currency: str|None },
    aging_lookup: callable(invoice_number, ou_number) -> AgingInvoiceView|None
    cross_currency: { is_cross_currency: bool,
                      credited_currency: str, invoice_currency: str|None,
                      functional_currency: str,
                      fx_credit_to_invoice: float|None,        ← Leg 1: credited→invoice
                      is_cross_ledger: bool,
                      fx_invoice_to_functional: float|None }   ← Leg 2: invoice→functional (Oracle only)
    ou_mismatch:   bool   — set by orchestrator via ou_resolver.resolve_ou_status()
                            True  = customer's aging OU != bank account OU
                            False = same OU or customer not in aging at all
    customer_ou_numbers: list[str]   — OUs where customer has open invoices (for audit)
    duplicate_invoice_across_customers: bool
    already_processed_match: bool   # bank_reference/amount/customer hits an existing Processed row
}

OUTPUT
------
RuleResult(rule_id, reason_code, category, matched_invoices, target_total,
           received_total, shortfall_pct, notes)

OVERPAYMENT POLICY
------------------
All overpayments (shortfall_pct < 0) are routed to R11 OVERPAYMENT_UNEXPLAINED
→ conflict_exception regardless of remittance content. R10 is not used.

CHANGES FROM PREVIOUS VERSION
------------------------------
1. DEFAULT_CUSTOMER_FUZZY_MIN_PCT: 40.0 → 35.0
   AI confidence scores in production: 0.35–0.82 (converted to pct by orchestrator).
   Old threshold of 40% was dropping 0.35–0.39 confidence rows into R8 (NO_SIGNAL)
   even when the AI had correctly identified the customer. 35% matches the minimum
   observed AI confidence score in production logs.

2. New R14 check added BEFORE R7 (cross-OU, no invoice path):
   When customer is confirmed by AI but belongs to a different OU than the bank
   account that received the payment, the row should route to conflict_exception
   (HITL action: re-route to correct OU) — NOT needs_remittance (HITL action:
   wait for remittance). Without this check, all cross-OU rows fell to R7 or R8.

   The existing R14 check inside the R9 block is KEPT — it handles the rarer
   case where a cross-OU payment also has an invoice number in the narrative.
   That case still resolves via the R9 family then hits R14 before R11/R9a/R9b.

3. TWO-LEG FX MODEL replacing single functional-currency leg:
   Leg 1  fx_credit_to_invoice   (credited → invoice)
          effective_received = credit_amount * fx_credit_to_invoice
          Basis for comparison AND Oracle Amount field.
   Leg 2  fx_invoice_to_functional  (invoice → functional)
          Passed to Oracle as ConversionRate only — we never apply it.
   R13 fires when Leg 1 is missing. Leg 2 missing alone does NOT trigger R13.

4. is_cross_ou_currency flag on RuleResult (Problem 2):
   Set True on R9a/R9b results where ou_mismatch is also True.
   Surfaces as a front-end badge; no HITL action required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Tolerance defaults — override via Settings / AppConfig table at call time.
DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT = 12.0
# CHANGED: 40.0 → 35.0 to match minimum observed AI confidence score (0.35 × 100 = 35%)
DEFAULT_CUSTOMER_FUZZY_MIN_PCT = 35.0


@dataclass
class MatchedInvoice:
    invoice_number: str
    outstanding_amount: float
    customer_name: str
    ou_number: str
    invoice_currency: str = ""                 # populated from aging row
    customer_number: str = ""                   # populated from aging row — Oracle CustomerAccountNumber
    stated_amount: Optional[float] = None     # from remittance, if available
    deduction_amount: Optional[float] = None


@dataclass
class RuleResult:
    rule_id: str
    reason_code: str
    category: str                              # maps to RowState
    matched_invoices: list[MatchedInvoice] = field(default_factory=list)
    target_total: Optional[float] = None
    received_total: Optional[float] = None
    shortfall_pct: Optional[float] = None
    notes: str = ""
    # Problem 2: True on ready_to_post/acceptable_short_payment rows where ou_mismatch=True.
    # Front-end badge + audit trail. No HITL required.
    is_cross_ou_currency: bool = False


def _resolve_matched_invoices(input_: dict) -> list[MatchedInvoice]:
    """
    Remittance's invoice list overrides extraction's guess when remittance
    is found (structured doc > narrative regex). Falls back to extraction.
    Looks every invoice up against the in-memory Aging map via aging_lookup.

    Split computation (ground rule: reference amounts MUST sum to the
    credited amount expressed in INVOICE currency, not functional currency):

      If cross-currency (credited != invoice) and fx_credit_to_invoice available:
        → effective_received = credit_amount * fx_credit_to_invoice  (invoice currency)
        → split is computed against effective_received

      If same currency (or no FX needed):
        → effective_received = credit_amount as-is

      Case 1 — remittance gave explicit amount_paid for ALL invoices
               → use stated_amount as-is (customer's own split intent)

      Case 2 — remittance gave amount_paid for SOME invoices only
               → use stated_amount where present; distribute the remaining
                  effective_received proportionally by outstanding among the rest

      Case 3 — no amount_paid on any invoice (or extraction path, no remittance)
               → distribute effective_received proportionally by outstanding
                  Last invoice absorbs any rounding remainder so total is exact.

    After this function, stated_amount is NEVER None on any returned
    MatchedInvoice — downstream (state_machine, hitl, oracle payload) can
    always use stated_amount directly as reference_amount.
    """
    aging_lookup: Callable = input_["aging_lookup"]
    ou_number       = input_["original_row"].get("ou_number")
    credit_amount   = float(input_["original_row"]["credit_amount"])

    # ── Resolve effective received amount in invoice currency (Leg 1) ─────────
    cross_currency       = input_.get("cross_currency") or {}
    is_cross_currency    = cross_currency.get("is_cross_currency", False)
    fx_credit_to_invoice = cross_currency.get("fx_credit_to_invoice")   # None if not resolved

    if is_cross_currency and fx_credit_to_invoice:
        # Convert credited amount → invoice currency for split computation
        effective_received = round(credit_amount * fx_credit_to_invoice, 2)
    else:
        # Same currency or FX rate not yet known — use raw credited amount
        effective_received = credit_amount

    remittance = input_.get("remittance") or {}
    extraction = input_.get("extraction") or {}

    if remittance.get("found"):
        candidate_numbers = [inv["invoice_number"] for inv in remittance.get("invoices", [])]
        stated_by_invoice = {inv["invoice_number"]: inv for inv in remittance.get("invoices", [])}
    else:
        candidate_numbers = extraction.get("extracted_invoices") or []
        stated_by_invoice = {}

    # ── Step 1: resolve all invoices against aging ────────────────────────────
    resolved: list[MatchedInvoice] = []
    for inv_no in candidate_numbers:
        aging_row = aging_lookup(inv_no, ou_number)
        if aging_row is None:
            continue  # handled by R4 (INVOICE_NOT_IN_AGING) at the caller level
        stated = stated_by_invoice.get(inv_no, {})
        resolved.append(MatchedInvoice(
            invoice_number=inv_no,
            outstanding_amount=float(aging_row.outstanding_amount),
            customer_name=aging_row.customer_name,
            ou_number=aging_row.ou_number,
            invoice_currency=(getattr(aging_row, "invoice_currency", None) or "").upper().strip(),
            customer_number=(getattr(aging_row, "customer_number", None) or ""),
            stated_amount=stated.get("amount_paid"),       # may still be None here
            deduction_amount=stated.get("amount_withheld"),
        ))

    if not resolved:
        return resolved

    # ── Step 2: compute stated_amount for every invoice so total = effective_received
    #
    # Single invoice — no split needed, reference = effective_received exactly.
    if len(resolved) == 1:
        resolved[0].stated_amount = effective_received
        return resolved

    all_have_stated  = all(m.stated_amount is not None for m in resolved)
    none_have_stated = all(m.stated_amount is None      for m in resolved)

    if all_have_stated:
        # Case 1 — remittance fully specified every invoice amount.
        # Trust the customer's own split; do NOT override with effective_received.
        # Shortfall/overpayment visible via target_total vs received_total in evaluate_row.
        pass

    elif none_have_stated:
        # Case 3 — no breakup info at all.
        # Distribute effective_received proportionally by outstanding weight.
        total_outstanding = sum(m.outstanding_amount for m in resolved)
        for i, m in enumerate(resolved):
            if i < len(resolved) - 1:
                weight = m.outstanding_amount / total_outstanding
                m.stated_amount = round(effective_received * weight, 2)
            else:
                # Last invoice absorbs rounding remainder — sum is exact.
                already_allocated = sum(r.stated_amount for r in resolved[:i])
                m.stated_amount = round(effective_received - already_allocated, 2)

    else:
        # Case 2 — partial: some invoices have stated_amount, some don't.
        # Remainder after stated amounts is distributed among the unstated ones.
        stated_total = sum(m.stated_amount for m in resolved if m.stated_amount is not None)
        remainder    = effective_received - stated_total

        unstated                   = [m for m in resolved if m.stated_amount is None]
        total_outstanding_unstated = sum(m.outstanding_amount for m in unstated)

        for i, m in enumerate(unstated):
            if i < len(unstated) - 1:
                weight = m.outstanding_amount / total_outstanding_unstated
                m.stated_amount = round(remainder * weight, 2)
            else:
                already_allocated = sum(r.stated_amount for r in unstated[:i])
                m.stated_amount   = round(remainder - already_allocated, 2)

    return resolved


def evaluate_row(
    input_: dict,
    short_payment_tolerance_pct: float = DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT,
    customer_fuzzy_min_pct: float = DEFAULT_CUSTOMER_FUZZY_MIN_PCT,
) -> RuleResult:
    original_row = input_["original_row"]
    extraction = input_.get("extraction") or {}
    remittance = input_.get("remittance") or {}
    credit_amount = float(original_row["credit_amount"])

    customer_found = (extraction.get("customer_match_pct") or 0) >= customer_fuzzy_min_pct
    invoice_found = bool(extraction.get("extracted_invoices")) or bool(remittance.get("invoices"))

    # R0 — duplicate invoice number across customers, ambiguous
    if input_.get("duplicate_invoice_across_customers") and input_.get("duplicate_ambiguous", False):
        return RuleResult("R0", "DUPLICATE_INVOICE_NO", "conflict_exception")

    # R1 — remittance customer != aging customer for a matched invoice
    if remittance.get("found") and remittance.get("customer_conflicts_with_aging"):
        return RuleResult("R1", "CUSTOMER_CONFLICT", "conflict_exception")

    # R2 — invoice 100% match, no customer text corroboration, remittance customer mismatches aging
    if (extraction.get("invoice_match_pct") == 100 and not extraction.get("customer_text_match")
            and remittance.get("found") and remittance.get("customer_conflicts_with_aging")):
        return RuleResult("R2", "INVOICE_CUSTOMER_MISMATCH", "conflict_exception")

    # R3 — multiple remittances match the same bank row
    if remittance.get("ambiguous"):
        return RuleResult("R3", "AMBIGUOUS_REMITTANCE", "conflict_exception")

    # R4 — matched invoice not found in Aging at all (closed/typo/nonexistent)
    candidate_numbers = (
        [inv["invoice_number"] for inv in remittance.get("invoices", [])]
        if remittance.get("found") else (extraction.get("extracted_invoices") or [])
    )
    aging_lookup: Callable = input_["aging_lookup"]
    ou_number = original_row.get("ou_number")
    if candidate_numbers and all(aging_lookup(n, ou_number) is None for n in candidate_numbers):
        return RuleResult("R4", "INVOICE_NOT_IN_AGING", "conflict_exception")

    # R5 — possible duplicate payment (matches an already-Processed row)
    if input_.get("already_processed_match"):
        return RuleResult("R5", "POSSIBLE_DUPLICATE_PAYMENT", "conflict_exception")

    # R6 — lump credit, remittance reveals multiple distinct customers
    if remittance.get("found") and remittance.get("multiple_customers"):
        return RuleResult("R6", "CROSS_CUSTOMER_SPLIT", "conflict_exception")

    # ── NEW R14 (pre-R7) — cross-OU, no invoice ──────────────────────────────
    # Fires when:
    #   customer_found = True  — AI confirmed the customer (pct >= 35%)
    #   invoice_found  = False — no invoice number in the payment narrative
    #   ou_mismatch    = True  — ou_resolver found customer's invoices in a
    #                            different OU than the bank account's OU
    #
    # MUST sit before R7 because R7's condition (customer found, no invoice)
    # is a superset — without this check every cross-OU row falls into R7
    # (needs_remittance) which is wrong. Waiting for remittance won't fix
    # a payment that landed in the wrong bank account entirely.
    #
    # HITL action: re-route the receipt to the correct OU for posting.
    # customer_ou_numbers in notes tells the SPOC which OU to send it to.
    if customer_found and not invoice_found and input_.get("ou_mismatch"):
        customer_ous = input_.get("customer_ou_numbers", [])
        return RuleResult(
            "R14",
            "WRONG_OU_PAYMENT",
            "conflict_exception",
            notes=(
                f"Customer has open invoices in OU(s) {customer_ous} but payment "
                f"received into bank account for OU {ou_number}. "
                f"Re-route receipt to OU {customer_ous[0] if customer_ous else 'unknown'}."
            ),
        )

    # R7 — customer confirmed, same OU, no invoice number, no remittance found
    # ou_mismatch is False here (cross-OU case already handled above by new R14)
    # HITL action: contact customer for remittance advice, then re-run matching.
    if customer_found and not invoice_found and not remittance.get("found"):
        return RuleResult("R7", "CUSTOMER_ONLY_NO_REMIT", "needs_remittance")

    # R8 — no customer, no invoice, no remittance — nothing at all extractable
    if not customer_found and not invoice_found and not remittance.get("found"):
        return RuleResult("R8", "NO_SIGNAL", "unidentified")

    # ── R9 family — resolve invoices against Aging, compute shortfall band ────
    matched_invoices = _resolve_matched_invoices(input_)
    if not matched_invoices:
        # Found *something* per above gates, but nothing resolves cleanly in Aging
        return RuleResult("R8", "NO_SIGNAL", "unidentified")

    target_total = sum(m.outstanding_amount for m in matched_invoices)

    # Use effective received amount in INVOICE currency for shortfall comparison.
    # Leg 1 (credited → invoice): if cross-currency and fx_credit_to_invoice
    # resolved → convert; else use raw credit_amount (same-currency path).
    cross_currency       = input_.get("cross_currency") or {}
    fx_credit_to_invoice = cross_currency.get("fx_credit_to_invoice")

    if cross_currency.get("is_cross_currency") and fx_credit_to_invoice:
        received_total = round(credit_amount * fx_credit_to_invoice, 2)
    else:
        received_total = credit_amount

    if target_total == 0:
        shortfall_pct = 0.0
    else:
        shortfall_pct = (target_total - received_total) / target_total * 100

    if cross_currency.get("is_cross_currency"):
        if fx_credit_to_invoice:
            # FX rate resolved — fall through to standard R9/R11 banding below.
            pass
        else:
            # Cross-currency (credited != invoice) but Leg 1 rate unavailable.
            # Cannot compare amounts — must flag for SPOC to provide rate manually.
            # Note: Leg 2 (invoice → functional) missing alone does NOT trigger R13.
            credited_ccy   = cross_currency.get("credited_currency")   or original_row.get("currency") or "unknown"
            invoice_ccy    = cross_currency.get("invoice_currency")    or "unknown"
            functional_ccy = cross_currency.get("functional_currency") or "unknown"
            return RuleResult(
                "R13", "FX_RATE_MISSING", "conflict_exception",
                matched_invoices, target_total, received_total, shortfall_pct,
                notes=(
                    f"Credited in {credited_ccy}, invoice currency is {invoice_ccy} — "
                    f"FX rate ({credited_ccy}→{invoice_ccy}) could not be resolved. "
                    f"SPOC must provide rate to re-evaluate. "
                    f"(OU functional currency is {functional_ccy}; "
                    f"invoice→functional rate is separate and handled by Oracle.)"
                ),
            )

    # R14 (in-R9 position) — cross-OU payment that also has an invoice number
    # This handles the rarer case where a cross-OU payment includes an invoice
    # reference in the narrative. The invoice resolved against aging above but
    # the payment still landed in the wrong OU's bank account.
    # ou_mismatch is set by ou_resolver in the orchestrator.
    if input_.get("ou_mismatch"):
        customer_ous = input_.get("customer_ou_numbers", [])
        return RuleResult(
            "R14",
            "WRONG_OU_SPLIT_REQUIRED",
            "conflict_exception",
            matched_invoices, target_total, received_total, shortfall_pct,
            notes=(
                f"Invoice resolved but customer OU(s) {customer_ous} != "
                f"bank account OU {ou_number}. Re-route to correct OU."
            ),
        )

    if shortfall_pct < 0:
        # Overpayment — always conflict_exception per business policy.
        # R10 (OVERPAYMENT_EXPLAINED) is not used; all overpayments require
        # SPOC review regardless of remittance content.
        return RuleResult("R11", "OVERPAYMENT_UNEXPLAINED", "conflict_exception",
                           matched_invoices, target_total, received_total, shortfall_pct)

    if shortfall_pct == 0:
        result = RuleResult("R9a", "EXACT_MATCH", "ready_to_post",
                            matched_invoices, target_total, received_total, shortfall_pct)

    elif 0 < shortfall_pct <= short_payment_tolerance_pct:
        # Per business rule: 0-12% shortage is acceptable regardless of
        # whether a remittance exists or explains the deduction.
        result = RuleResult("R9b", "ACCEPTABLE_SHORT_PAYMENT", "acceptable_short_payment",
                            matched_invoices, target_total, received_total, shortfall_pct)

    else:
        result = RuleResult("R9c", "UNEXPLAINED_SHORTAGE", "conflict_exception",
                            matched_invoices, target_total, received_total, shortfall_pct)

    # Problem 2: tag postable rows that are also cross-OU for front-end badge + audit trail.
    # R14 handles cases needing HITL. This covers the rarer case where amounts match
    # exactly (R9a) or within tolerance (R9b) but the payment still crossed OUs.
    if result.category in ("ready_to_post", "acceptable_short_payment"):
        result.is_cross_ou_currency = bool(input_.get("ou_mismatch"))

    return result