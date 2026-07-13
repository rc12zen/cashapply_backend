"""
app.bank_statement.credit_filter
==================================
Explicit credit-only filter applied after parsing.
Drops any row where credit_amount <= 0 or narrative is in the skip list.
Exposed as a standalone function so it can be unit-tested independently
of the full parse pipeline.
"""
from __future__ import annotations

from .parser import NormalizedCreditRow


def filter_credits_only(
    rows: list[NormalizedCreditRow],
    skip_narratives: set[str] | None = None,
) -> list[NormalizedCreditRow]:
    """
    Return only rows with credit_amount > 0 and narrative not in skip list.
    The parser already applies this logic, but this function lets downstream
    code re-apply the filter if rows come from a different source.
    """
    skip = skip_narratives or set()
    return [
        r for r in rows
        if r.credit_amount > 0 and r.narrative not in skip
    ]
