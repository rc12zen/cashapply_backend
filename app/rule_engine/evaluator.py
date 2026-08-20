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
    credit_memos_lookup: callable(customer_number, ou_number, currency)
                          -> list[CreditMemoView]
        REQUIRED — see _require_credit_memos_lookup() below for why a missing
        key raises instead of defaulting to "no credit memos".
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
    settlement_type: str | None     # "card_narrative" | "cheque_narrative" | "third_party_provider" | None
                                     # set by bank_statement/settlement_identifier.py -- see R16/R17/R18
    settlement_provider: str | None # matched provider_name -- only set when settlement_type == "third_party_provider"
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
    # The actual evidence behind is_cross_ou_currency -- see
    # rule_engine/ou_resolver.py::OUResolverResult.customer_ou_details and
    # db/models.py's LineItem.ou_evidence. None when there was no customer
    # signal to evaluate (ou_mismatch was never computed).
    ou_evidence: Optional[dict] = None
    # Set only by R16/R17/R18 -- see settlement_type in the input contract
    # above. Carried on RuleResult (not just derived from reason_code) so
    # state_machine.py can persist it onto LineItem without string-parsing
    # reason_code, and settlement_provider (third-party rows only) has
    # nowhere else to travel from evaluate_row() back to the DB row.
    settlement_type: Optional[str] = None
    settlement_provider: Optional[str] = None


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
        if total_outstanding <= 0:
            # Nothing to weight by, so there is no meaningful split. Leave every
            # stated_amount as None and let evaluate_row's R12 guard classify
            # the row -- a matched set with no payable balance is an exception,
            # not an allocation problem.
            #
            # This used to be an unguarded division and raised ZeroDivisionError
            # out of the rule engine, which the orchestrator's per-row try/except
            # then swallowed into a generic row error with no reason attached.
            # The way in was a credit document being matched alongside an invoice
            # it cancelled out (+1000 and -1000); aging/aging_map.py's is_payable()
            # now keeps such documents out of the pool, so this is the backstop.
            return resolved
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

        if total_outstanding_unstated <= 0:
            # Same guard as Case 3 above, for the partial-breakup path.
            return resolved

        for i, m in enumerate(unstated):
            if i < len(unstated) - 1:
                weight = m.outstanding_amount / total_outstanding_unstated
                m.stated_amount = round(remainder * weight, 2)
            else:
                already_allocated = sum(r.stated_amount for r in unstated[:i])
                m.stated_amount   = round(remainder - already_allocated, 2)

    return resolved


def _require_credit_memos_lookup(input_: dict) -> Callable:
    """
    Fetch the credit-memo lookup, refusing to run without it.

    WHY THIS IS LOUD. The rule input is assembled independently in three
    places — orchestrator.py (the main run), remittance_recheck.py and
    customer_name_correction.py (both re-evaluate an existing row). All
    three reach the R9b branch below.

    If this were read with .get() and only one site supplied it, the other
    two would silently behave as "customer holds no credit memos". A row
    correctly held back for review on the main path would then be quietly
    auto-accepted the moment a remittance arrived or a name was corrected —
    the row would appear to un-flag itself, with nothing logged and no
    error anywhere. That failure is invisible and it loses money, so a
    missing key is an error, not a default.

    Checked once at the top of evaluate_row rather than at the point of use
    so a fourth call site fails on its very first row, not on the first row
    that happens to be short.
    """
    lookup = input_.get("credit_memos_lookup")
    if not callable(lookup):
        raise KeyError(
            "RuleEngineInput is missing the required 'credit_memos_lookup' callable. "
            "Every site that builds this dict must supply it — see "
            "orchestrator.py, remittance_recheck.py and customer_name_correction.py. "
            "It must NOT be defaulted away: a missing lookup would silently "
            "auto-accept short payments that should be held for review."
        )
    return lookup


def _credit_memos_for_match(input_: dict, matched_invoices: list[MatchedInvoice]) -> list:
    """
    Open credit memos belonging to the customer this payment matched.

    Scoped to the matched invoice's customer + OU + currency. That scoping
    is not a guess: of the 164 credit memos in the 31-Mar-2026 export that
    name a specific invoice, all 164 agree with it on all three, with no
    counter-example — and BRD Scenario 13 has a payment landing in the
    wrong OU parked in GL 23213 rather than applied across OUs.

    Credit memos ONLY. An unapplied receipt is also money on the customer's
    account, but per Finance nobody knows when a customer will come back to
    one, so it must not influence an automatic decision. That exclusion is
    the default in AgingMap.credit_memos_for().
    """
    lookup = _require_credit_memos_lookup(input_)
    if not matched_invoices:
        return []
    m = matched_invoices[0]
    return lookup(m.customer_number, m.ou_number, m.invoice_currency) or []


def evaluate_row(
    input_: dict,
    short_payment_tolerance_pct: float = DEFAULT_SHORT_PAYMENT_TOLERANCE_PCT,
    customer_fuzzy_min_pct: float = DEFAULT_CUSTOMER_FUZZY_MIN_PCT,
) -> RuleResult:
    _require_credit_memos_lookup(input_)   # fail fast, before any rule runs
    original_row = input_["original_row"]
    extraction = input_.get("extraction") or {}
    remittance = input_.get("remittance") or {}
    credit_amount = float(original_row["credit_amount"])

    customer_found = (extraction.get("customer_match_pct") or 0) >= customer_fuzzy_min_pct
    invoice_found = bool(extraction.get("extracted_invoices")) or bool(remittance.get("invoices"))

    # R16/R17/R18 — settlement identity (credit card / cheque / third-party
    # provider) MUST be checked before everything else. These three types
    # are consolidated bank lines by design (one credit = many customers),
    # so the normal customer/invoice-matching rules below would either
    # misfire (R6 CROSS_CUSTOMER_SPLIT treats this as a problem, when for
    # these three it's expected) or just never find a single matching
    # invoice. A settlement-identity match means "do not run the R0-R15
    # table at all" -- the row needs a human to fill in the Split & Map
    # breakdown before anything else is decided, and per the broker PRD
    # requirement, no receipt gets created automatically for these.
    settlement_type = input_.get("settlement_type")
    if settlement_type == "card_narrative":
        return RuleResult("R16", "CARD_SETTLEMENT_DETECTED", "needs_distribution",
                           settlement_type=settlement_type)
    if settlement_type == "cheque_narrative":
        return RuleResult("R17", "CHEQUE_SETTLEMENT_DETECTED", "needs_distribution",
                           settlement_type=settlement_type)
    if settlement_type == "third_party_provider":
        return RuleResult("R18", "THIRD_PARTY_PROVIDER_DETECTED", "needs_distribution",
                           settlement_type=settlement_type,
                           settlement_provider=input_.get("settlement_provider"))

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
            ou_evidence=input_.get("ou_evidence"),
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

    # ── R12 — the matched set carries no payable balance ─────────────────────
    # This branch used to read `if target_total == 0: shortfall_pct = 0.0`,
    # which then fell straight through to the banding below and returned R9a
    # EXACT_MATCH -> ready_to_post. A row that matched NOTHING PAYABLE was
    # therefore presented as a *perfect match* with a live Approve button, and
    # approving it would have sent Oracle reference amounts summing to zero (or
    # negative) against a receipt holding real cash.
    #
    # The usual way in was a credit document being matched as though it were an
    # invoice: invoice +1000 and credit memo -1000 in the same set nets to 0.
    # aging/aging_map.py's is_payable() now keeps such documents out of the
    # matchable pool entirely, so this should be unreachable from the aging
    # path — it stays as the arithmetic backstop, because "nothing to pay" must
    # never again be spelled the same way as "paid exactly right".
    if target_total <= 0:
        return RuleResult(
            "R12", "NO_PAYABLE_BALANCE", "conflict_exception",
            matched_invoices, target_total, received_total, 0.0,
            notes=(
                f"The {len(matched_invoices)} matched document(s) carry a combined "
                f"payable balance of {target_total} — there is nothing outstanding to "
                f"apply {received_total} against. Re-map this row to the invoice(s) the "
                f"payment actually covers."
            ),
        )

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
            ou_evidence=input_.get("ou_evidence"),
        )

    if shortfall_pct < 0:
        # Overpayment — always conflict_exception per business policy.
        # R10 (OVERPAYMENT_EXPLAINED) is not used; all overpayments require
        # SPOC review regardless of remittance content.
        #
        # This was the ONLY rule in this file that returned no `notes` at all,
        # so the row-detail screen said "Overpayment" and stopped — the SPOC
        # had to reconstruct the arithmetic by hand every time. The WHY (a
        # duplicate claim, invoices open in another OU, unmatched invoices for
        # the same customer, an FX difference) is computed separately by
        # rule_engine/overpayment_reason.py once the row is persisted, because
        # it needs the ledger and the full aging map — neither of which this
        # deliberately pure function has access to.
        excess = round(received_total - target_total, 2)
        return RuleResult(
            "R11", "OVERPAYMENT_UNEXPLAINED", "conflict_exception",
            matched_invoices, target_total, received_total, shortfall_pct,
            notes=(
                f"Received {received_total} against matched invoice(s) totalling "
                f"{target_total} — an excess of {excess}. Either map the invoice(s) this "
                f"payment actually covers, or record why the excess exists."
            ),
        )

    if shortfall_pct == 0:
        result = RuleResult("R9a", "EXACT_MATCH", "ready_to_post",
                            matched_invoices, target_total, received_total, shortfall_pct)

    elif 0 < shortfall_pct <= short_payment_tolerance_pct:
        # Per business rule: 0-12% shortage is acceptable regardless of
        # whether a remittance exists or explains the deduction. This ALWAYS
        # stays R9b/acceptable_short_payment -- the shortfall percentage is
        # what decides the group, full stop.
        #
        # Open credit memos do NOT move the row to a different group. They
        # used to (see git history) -- routing to conflict_exception/R9c
        # whenever the customer held any open credit memo, on the reasoning
        # that letting it through on tolerance risks the credit memo being
        # left open in Oracle and deducted again later. That's a real risk
        # worth surfacing, but overriding the CATEGORY for it was the wrong
        # layer: it took a row that already resolved cleanly on the primary
        # metric (shortfall %) and moved it into a different queue based on
        # a secondary signal. Per business direction, the credit-memo
        # situation is now carried as a note on the SAME R9b result instead
        # -- visible to whoever reviews the row, without rerouting it.
        open_credit_memos = _credit_memos_for_match(input_, matched_invoices)
        notes = ""
        if open_credit_memos:
            shortfall_amount = round(target_total - received_total, 2)
            available = round(sum(c.amount for c in open_credit_memos), 2)
            exact = [c for c in open_credit_memos if round(c.amount, 2) == shortfall_amount]
            # Only a SINGLE exact match is called out. Two credit memos of
            # the same amount is genuinely ambiguous, and summing subsets is
            # never attempted -- Assurant holds 164 open credit memos in one
            # OU, and some subset of 164 numbers fits almost any target, so
            # combination search would manufacture confident wrong answers.
            if len(exact) == 1:
                cause = (
                    f"Credit memo {exact[0].document_number} is for exactly "
                    f"{shortfall_amount} — likely the deduction."
                )
            else:
                cause = (
                    f"No single credit memo matches the shortfall exactly; "
                    f"{len(open_credit_memos)} open totalling {available}."
                )
            notes = (
                f"Short by {shortfall_amount} ({shortfall_pct:.1f}%), within the "
                f"{short_payment_tolerance_pct}% tolerance. This customer holds open "
                f"credit memos -- accepted on tolerance as usual, but flagging so the "
                f"credit memo isn't left open to be claimed twice. {cause}"
            )
        result = RuleResult("R9b", "ACCEPTABLE_SHORT_PAYMENT", "acceptable_short_payment",
                            matched_invoices, target_total, received_total, shortfall_pct,
                            notes=notes)

    else:
        result = RuleResult("R9c", "UNEXPLAINED_SHORTAGE", "conflict_exception",
                            matched_invoices, target_total, received_total, shortfall_pct)

    # Problem 2: tag postable rows that are also cross-OU for front-end badge + audit trail.
    # R14 handles cases needing HITL. This covers the rarer case where amounts match
    # exactly (R9a) or within tolerance (R9b) but the payment still crossed OUs.
    if result.category in ("ready_to_post", "acceptable_short_payment"):
        result.is_cross_ou_currency = bool(input_.get("ou_mismatch"))
        if result.is_cross_ou_currency:
            result.ou_evidence = input_.get("ou_evidence")

    return result