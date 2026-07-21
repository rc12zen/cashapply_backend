"""
app.rule_engine.ou_resolver
==============================
Determines whether a payment is cross-OU by comparing:

  1. bank_ou   — the OU the bank account belongs to
                 (resolved at parse time from bank_ou_mapping.json
                  via detector → stored on CreditRowSchema.ou_number)

  2. customer_ou — the OU(s) where the customer has open invoices
                   in the aging map

Cross-OU = customer exists in the global aging map BUT has NO open
           invoices in the specific OU the bank account belongs to.

This is NOT a string check on extraction_method.
It is a factual lookup against the live aging map.

Usage (called from orchestrator._build_rule_input):
----------------------------------------------------
    from .ou_resolver import resolve_ou_status

    ou_status = resolve_ou_status(
        customer_name=payment.customer_name,
        bank_ou_number=orig.ou_number,        # from CreditRowSchema
        aging_map=aging_map,
        fuzzy_min_pct=60.0,                   # same threshold used by extraction layer
    )

    # Then pass into rule engine:
    "ou_mismatch": ou_status.is_cross_ou,
    "customer_ou_numbers": ou_status.customer_ous,

OUResolverResult fields:
------------------------
  is_cross_ou     : bool   — True = payment came into wrong OU's bank account
  customer_ous    : list   — which OUs the customer actually has invoices in
  customer_in_any : bool   — customer exists SOMEWHERE in aging (global)
  bank_ou         : str    — the OU the bank account belongs to
  reason          : str    — human-readable explanation for audit trail
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz


@dataclass
class OUResolverResult:
    is_cross_ou:     bool
    customer_in_any: bool           # exists in global aging (any OU)
    bank_ou:         str            # OU from bank account
    customer_ous:    list[str] = field(default_factory=list)  # OUs with open invoices
    reason:          str = ""
    # The actual evidence behind the above -- per-OU matched customer name,
    # fuzzy match score, and outstanding amount/count. See
    # _find_customer_ou_details(). Empty when customer_name was None (no
    # customer signal to evaluate at all).
    customer_ou_details: list[dict] = field(default_factory=list)


def resolve_ou_status(
    customer_name: Optional[str],
    bank_ou_number: Optional[str],
    aging_map,                      # AgingMap instance
    fuzzy_min_pct: float = 60.0,
    bank_ou_numbers: Optional[list[str]] = None,
) -> OUResolverResult:
    """
    Core function. Compares the bank's OU(s) against the aging map's OU(s)
    for the confirmed customer.

    MULTI-BU: `bank_ou_numbers`, if given, is the FULL set of Business
    Units the bank account belongs to (primary + additional — see
    db/models.py's BankAccount.all_ou_numbers). Cross-OU now means the
    customer's invoice OU(s) have NO OVERLAP with this whole set, not just
    a mismatch against a single OU — so a multi-BU account isn't wrongly
    flagged as cross-OU just because the customer's invoice happens to be
    in its SECOND linked Business Unit rather than its primary one. Falls
    back to treating `bank_ou_number` as a one-item set when not given
    (single-BU accounts — the overwhelming majority — behave exactly as
    before).

    Cases:
      A. customer_name is None → no customer signal → not cross-OU (R8 handles it)
      B. customer found in one of the bank's OU(s) → same OU → is_cross_ou=False
      C. customer NOT in any of the bank's OU(s) but found in other OUs → is_cross_ou=True
      D. customer not found anywhere in aging → is_cross_ou=False
                                                (R8 handles unknown customers)

    The fuzzy match uses the same threshold (60%) as the extraction layer's
    global cross-OU check, so the decision here is consistent with what
    the extraction layer already decided.
    """
    bank_ous = [str(n).strip() for n in bank_ou_numbers if str(n).strip()] if bank_ou_numbers else (
        [str(bank_ou_number).strip()] if bank_ou_number else []
    )
    # Kept for the return value / existing callers that display a single
    # "the bank OU" — the primary/first one in the set.
    bank_ou = bank_ous[0] if bank_ous else str(bank_ou_number or "").strip()

    # Case A — no customer extracted
    if not customer_name:
        return OUResolverResult(
            is_cross_ou=False,
            customer_in_any=False,
            bank_ou=bank_ou,
            reason="No customer name — cannot determine OU status",
        )

    # Ask aging map: which OUs have invoices for this customer? (plus the
    # actual evidence behind each match — see _find_customer_ou_details.)
    customer_ou_details = _find_customer_ou_details(customer_name, aging_map, fuzzy_min_pct)
    customer_ous = [d["ou_number"] for d in customer_ou_details]
    customer_in_any = len(customer_ous) > 0

    # Case D — customer not in aging at all
    if not customer_in_any:
        return OUResolverResult(
            is_cross_ou=False,
            customer_in_any=False,
            bank_ou=bank_ou,
            customer_ous=[],
            reason=f"Customer '{customer_name}' not found in any OU aging — truly unknown",
        )

    # Case B — customer has invoices in ONE OF the bank account's linked
    # OU(s) (usually just one; more than one for a multi-BU account) →
    # same OU, no mismatch. Report the SPECIFIC one that matched (not
    # necessarily the primary) as `bank_ou`, so the reason string/audit
    # trail is accurate for a multi-BU account.
    matched = [ou for ou in bank_ous if ou in customer_ous]
    if matched:
        matched_ou = matched[0]
        return OUResolverResult(
            is_cross_ou=False,
            customer_in_any=True,
            bank_ou=matched_ou,
            customer_ous=customer_ous,
            customer_ou_details=customer_ou_details,
            reason=(
                f"Customer '{customer_name}' has open invoices in bank OU {matched_ou} "
                f"— same OU, no mismatch"
            ),
        )

    # Case C — customer exists in aging but NOT in any of the bank's
    # linked OU(s) → cross-OU
    bank_ous_display = ", ".join(bank_ous) if len(bank_ous) > 1 else bank_ou
    return OUResolverResult(
        is_cross_ou=True,
        customer_in_any=True,
        bank_ou=bank_ou,
        customer_ous=customer_ous,
        customer_ou_details=customer_ou_details,
        reason=(
            f"Customer '{customer_name}' has open invoices in OU(s) {customer_ous} "
            f"but payment landed in bank OU(s) {bank_ous_display} — cross-OU payment"
        ),
    )


def _find_customer_ou_details(
    customer_name: str,
    aging_map,
    fuzzy_min_pct: float,
) -> list[dict]:
    """
    Like _find_customer_ous, but keeps the EVIDENCE behind each match
    instead of discarding it: which exact customer name in aging matched,
    how close the fuzzy match was, and that customer's open invoices in
    that OU (amount + count). This is what lets the Row Detail page show
    "here's why we concluded that" instead of just the conclusion — see
    bff/row_detail.py / db/models.py's LineItem.ou_evidence.

    Returns one entry per OU where a match was found, sorted by OU number:
        [{"ou_number": "205", "matched_customer_name": "ABC CORP",
          "match_score": 87, "invoice_count": 3, "total_outstanding": 4500.0}]
    """
    details: list[dict] = []
    customer_upper = customer_name.upper().strip()

    for ou in aging_map.ou_numbers:
        ou_customers = aging_map.customers_for_ou(ou, limit=200)
        best_name, best_score = None, 0
        for aging_customer in ou_customers:
            score = fuzz.token_sort_ratio(customer_upper, str(aging_customer).upper().strip())
            if score > best_score:
                best_name, best_score = aging_customer, score
        if best_name is not None and best_score >= fuzzy_min_pct:
            invoices = aging_map.invoices_for_customer(best_name)
            ou_invoices = [inv for inv in invoices if inv.ou_number == ou]
            details.append({
                "ou_number": ou,
                "matched_customer_name": best_name,
                "match_score": best_score,
                "invoice_count": len(ou_invoices),
                "total_outstanding": round(sum(inv.outstanding_amount for inv in ou_invoices), 2),
            })

    return sorted(details, key=lambda d: d["ou_number"])


def _find_customer_ous(
    customer_name: str,
    aging_map,
    fuzzy_min_pct: float,
) -> list[str]:
    """
    Returns all OU numbers where this customer has open invoices.

    Iterates over all OUs in the aging map and checks if the customer
    name matches any customer in that OU using fuzzy matching.
    Same approach the extraction layer uses — consistent thresholds.

    Returns a sorted list of OU number strings e.g. ['111', '291']
    Returns [] if customer not found in any OU.
    """
    matched_ous: list[str] = []
    customer_upper = customer_name.upper().strip()

    for ou in aging_map.ou_numbers:
        ou_customers = aging_map.customers_for_ou(ou)   # list of customer name strings
        for aging_customer in ou_customers:
            score = fuzz.token_sort_ratio(
                customer_upper,
                str(aging_customer).upper().strip(),
            )
            if score >= fuzzy_min_pct:
                matched_ous.append(ou)
                break   # found a match in this OU — no need to check other customers

    return sorted(set(matched_ous))