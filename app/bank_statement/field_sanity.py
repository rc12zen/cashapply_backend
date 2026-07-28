"""
app.bank_statement.field_sanity
================================
Value-level sanity checks over the PARSED sample rows a config test produces —
the authoritative "does each field actually contain what it should" layer that
was missing (a config could parse cleanly and still be wired to the wrong
columns, e.g. a metadata label cell as the account number).

check_field_values(rows) inspects real NormalizedCreditRow values (account,
date, currency, narrative) and returns a flat list of warnings:
    {"field", "severity": "error"|"warn", "message", "sample"}

Severity policy (agreed with the business):
  - account_number failures are "error" — the frontend BLOCKS Save/Test on
    these unless the SPOC explicitly overrides (they caused config corruption).
  - everything else is "warn" — surfaced to the user, never blocking.

Runs on the parsed rows, where account_number is already normalized and
statement_date is already parsed (None when unparseable) — so a wrong Date
column shows up as a low valid-date rate, a label account as a reject_reason,
etc. Amount is NOT checked here: parse_credit_rows already drops non-numeric /
non-positive amounts, so a bad Credit Amount field surfaces as a low/zero
row_count, which the wizard already shows.
"""
from __future__ import annotations

import re

from .account_validation import account_reject_reason

_MIN_VALID_FRACTION = 0.5   # below this share of good values → warn
_CCY_RE = re.compile(r"^[A-Za-z]{3}$")
_NUMERIC_LIKE_RE = re.compile(r"^[\d.,\-+\s]+$")


def _get(row, name):
    return getattr(row, name, None)


def check_field_values(rows) -> list[dict]:
    """Return value-level warnings for a parsed sample of rows. Empty list when
    there's nothing to flag. `rows` is a list of NormalizedCreditRow (or any
    object exposing account_number / statement_date / currency / narrative)."""
    warnings: list[dict] = []
    n = len(rows)
    if n == 0:
        return warnings

    # ── account_number — ERROR (the hard gate) ────────────────────────────────
    accounts = [str(_get(r, "account_number") or "").strip() for r in rows]
    distinct = sorted({a for a in accounts if a})
    if not distinct:
        warnings.append({
            "field": "account_number", "severity": "error",
            "message": "No account number was extracted from any row — the Account Number field looks unmapped or points at an empty cell.",
            "sample": None,
        })
    else:
        rejected = [(a, account_reject_reason(a)) for a in distinct]
        rejected = [(a, why) for a, why in rejected if why]
        if rejected:
            a, why = rejected[0]
            warnings.append({
                "field": "account_number", "severity": "error",
                "message": why, "sample": a,
            })

    # ── date — WARN ───────────────────────────────────────────────────────────
    valid_dates = sum(1 for r in rows if _get(r, "statement_date") is not None)
    if valid_dates / n < _MIN_VALID_FRACTION:
        warnings.append({
            "field": "date", "severity": "warn",
            "message": (f"Only {valid_dates} of {n} rows have a readable date — the Date field "
                        f"may be mapped to the wrong column or the date format isn't recognised."),
            "sample": None,
        })

    # ── currency — WARN ───────────────────────────────────────────────────────
    currencies = [str(_get(r, "currency") or "").strip() for r in rows]
    valid_ccy = sum(1 for c in currencies if _CCY_RE.match(c))
    if valid_ccy / n < _MIN_VALID_FRACTION:
        sample = next((c for c in currencies if c), "")
        warnings.append({
            "field": "currency", "severity": "warn",
            "message": (f"Currency values don't look like 3-letter codes"
                        + (f" (e.g. '{sample}')" if sample else "")
                        + " — the Currency field may point at the wrong cell/column."),
            "sample": sample or None,
        })

    # ── narrative — WARN ──────────────────────────────────────────────────────
    narratives = [str(_get(r, "narrative") or "").strip() for r in rows]
    empty = sum(1 for x in narratives if not x)
    numeric_like = sum(1 for x in narratives if x and _NUMERIC_LIKE_RE.match(x))
    if empty / n > _MIN_VALID_FRACTION:
        warnings.append({
            "field": "narrative", "severity": "warn",
            "message": f"{empty} of {n} narrative values are empty — the Narrative field may be mapped to the wrong column.",
            "sample": None,
        })
    elif numeric_like / n > _MIN_VALID_FRACTION:
        sample = next((x for x in narratives if x and _NUMERIC_LIKE_RE.match(x)), "")
        warnings.append({
            "field": "narrative", "severity": "warn",
            "message": f"Narrative values look numeric (e.g. '{sample}') — this may be the wrong column.",
            "sample": sample or None,
        })

    return warnings
