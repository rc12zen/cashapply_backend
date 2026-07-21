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


def resolve_ou_status(
    customer_name: Optional[str],
    bank_ou_number: Optional[str],
    aging_map,                      # AgingMap instance
    fuzzy_min_pct: float = 60.0,
) -> OUResolverResult:
    """
    Core function. Compares the bank's OU against the aging map's OU(s)
    for the confirmed customer.

    Cases:
      A. customer_name is None → no customer signal → not cross-OU (R8 handles it)
      B. customer found in bank_ou aging → same OU → is_cross_ou=False
      C. customer NOT in bank_ou aging but found in other OUs → is_cross_ou=True
      D. customer not found anywhere in aging → is_cross_ou=False
                                                (R8 handles unknown customers)

    The fuzzy match uses the same threshold (60%) as the extraction layer's
    global cross-OU check, so the decision here is consistent with what
    the extraction layer already decided.
    """
    bank_ou = str(bank_ou_number or "").strip()

    # Case A — no customer extracted
    if not customer_name:
        return OUResolverResult(
            is_cross_ou=False,
            customer_in_any=False,
            bank_ou=bank_ou,
            reason="No customer name — cannot determine OU status",
        )

    # Ask aging map: which OUs have invoices for this customer?
    customer_ous = _find_customer_ous(customer_name, aging_map, fuzzy_min_pct)
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

    # Case B — customer has invoices in the bank's OU → same OU
    if bank_ou in customer_ous:
        return OUResolverResult(
            is_cross_ou=False,
            customer_in_any=True,
            bank_ou=bank_ou,
            customer_ous=customer_ous,
            reason=(
                f"Customer '{customer_name}' has open invoices in bank OU {bank_ou} "
                f"— same OU, no mismatch"
            ),
        )

    # Case C — customer exists in aging but NOT in the bank's OU → cross-OU
    return OUResolverResult(
        is_cross_ou=True,
        customer_in_any=True,
        bank_ou=bank_ou,
        customer_ous=customer_ous,
        reason=(
            f"Customer '{customer_name}' has open invoices in OU(s) {customer_ous} "
            f"but payment landed in bank OU {bank_ou} — cross-OU payment"
        ),
    )


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