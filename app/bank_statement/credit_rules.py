"""
app.bank_statement.credit_rules
================================
Typed credit rule evaluators, driven by the `credit_rule` section of bank_configs.json.

Rule types
----------
amount_positive   : signed amount column — positive value means credit
column_not_blank  : dedicated credit column — non-blank (and positive) means credit
flag_matches      : a flag/code column (raw name) matches a regex pattern
"""
from __future__ import annotations

import pandas as pd

from ..common.regex_safety import safe_search


def _column_for_logical(fields: list, logical_name: str) -> str | None:
    for f in fields:
        if f["name"] == logical_name:
            src = f.get("from", {})
            if src.get("type") == "column":
                return src.get("name")
    return None


def eval_credit_rule(row: pd.Series, rule: dict, fields: list) -> bool:
    """
    Evaluate whether a DataFrame row is a credit row.

    Parameters
    ----------
    row    : one row from the parsed DataFrame
    rule   : the credit_rule dict from the config
    fields : the fields list from the config (for logical→column mapping)
    """
    rule_type = rule["type"]

    if rule_type in ("amount_positive", "column_not_blank"):
        from .parser import parse_amount  # lazy import to avoid import cycle

        col = _column_for_logical(fields, rule["field"]) or rule["field"]
        val = row.get(col)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return False
        if str(val).strip() in ("", "nan", "None"):
            return False
        amount = parse_amount(val)  # handles "1,234.56", "(500)", "100 CR", …
        return amount is not None and amount > 0

    if rule_type == "flag_matches":
        val = row.get(rule["field"])
        if val is None:
            return False
        # The wizard always sends the fixed "(?i)cr" here (there is no UI to
        # type another), but the API accepts a recipe body directly -- so the
        # pattern is validated and length-bounded before it reaches the
        # backtracking engine. See common/regex_safety.py (ReDoS / CWE-1333).
        return bool(safe_search(rule["pattern"], str(val).strip()))

    raise ValueError(f"Unknown credit rule type: '{rule_type}'")
