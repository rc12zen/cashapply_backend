"""
app.common.account_masking
===========================
Single place that decides what a bank account number looks like when it
travels through an API response. VAPT flagged full account numbers being
returned to any authenticated viewer (CWE-200-adjacent info disclosure) --
this masks them at serialization time; the real value is only ever returned
by the dedicated reveal endpoints (bank_accounts_routes.py,
results_routes.py), which re-check the same permission used to view the
record and audit-log the reveal.
"""

MASK_CHAR = "•"


def mask_account_number(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return MASK_CHAR * len(value)
    return f"{MASK_CHAR * 4}{value[-4:]}"
