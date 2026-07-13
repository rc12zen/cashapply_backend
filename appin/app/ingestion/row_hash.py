"""
app.ingestion.row_hash
========================
Row-level duplicate detection across separate uploads. See design doc §2.2.

Hash is computed over a NORMALIZED representation, not the raw row — two
exports of the same transaction rarely byte-match (column order, whitespace,
date format differ). Scoped to bank_account_id: the same amount/date/
reference on two different accounts is not a duplicate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from decimal import Decimal


def compute_row_hash(
    bank_account_id: int | None,
    statement_date: dt.date | dt.datetime | None,
    amount: Decimal | float | None,
    currency: str | None,
    bank_reference: str | None,
    narrative: str | None,
) -> str:
    date_part = ""
    if statement_date is not None:
        date_part = (
            statement_date.date().isoformat()
            if isinstance(statement_date, dt.datetime)
            else statement_date.isoformat()
        )
    amount_part = f"{float(amount or 0):.2f}"
    currency_part = (currency or "").upper().strip()
    reference_part = (bank_reference or "").strip().upper()
    narrative_part = re.sub(r"\s+", " ", (narrative or "").strip().upper())[:200]

    normalized = "|".join([
        str(bank_account_id or ""),
        date_part,
        amount_part,
        currency_part,
        reference_part,
        narrative_part,
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
