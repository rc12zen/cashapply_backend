"""
app.bank_statement.ou_resolver
==============================
Resolves an account number → OU info using the Zensar bank_ou_mapping.json,
which is keyed by the last-4 digits of the account number.

Kept on the old last-4 suffix format because:
  - bank_ou_mapping.json is the authoritative Zensar OU reference (maintained
    separately from account_configs.json — adding a new account recipe does not
    require touching the OU map as long as the last-4 suffix is already in it).
  - Full-account keying in account_ou_map.json was dropped because it contained
    wrong/incomplete OU names and required manual updating per full account number.

Lookup order (mirrors the old detector step 2):
  1. Strip non-alphanumeric chars from account_number, take last 4 digits/chars.
  2. Try lengths 4 → 3 → 2 (matches original suffix fallback logic).
  3. Return {ou_number, business_unit, bank} or {} if not found.

`business_unit` here is the `ou` field in bank_ou_mapping.json
(e.g. "PUNE(111)") — kept under the `business_unit` key so the rest of the new
engine (parser, orchestrator, line_items) can read it without changes.
"""
from __future__ import annotations

import re
from typing import Optional

from .configs.account_loader import load_bank_ou_mapping


def _last_n_digits(account_number: str, n: int) -> str:
    """Return the last `n` alphanumeric characters of account_number."""
    clean = re.sub(r"[^0-9a-zA-Z]", "", str(account_number))
    return clean[-n:] if len(clean) >= n else clean


def resolve_ou(account_number: Optional[str]) -> dict:
    """
    Return {ou_number, business_unit, bank} for the account via last-4 suffix
    lookup in bank_ou_mapping.json.  Returns {} when not found.
    """
    if not account_number:
        return {}

    ou_map = load_bank_ou_mapping()

    for length in (4, 3, 2):
        suffix = _last_n_digits(str(account_number), length)
        if suffix and suffix in ou_map:
            entry = ou_map[suffix]
            return {
                "ou_number":     entry.get("ou_number"),
                "business_unit": entry.get("ou"),     # "ou" field → business_unit
                "bank":          entry.get("bank"),
            }

    return {}