"""
app.bank_statement.currency
============================
Currency standardization to ISO-4217 3-letter codes — the form Oracle Fusion
requires on every receipt payload (see oracle/fusion_client.py). Bank statements
routinely spell currencies in non-standard ways ("EURO", "Sterling", "US Dollar",
"€"); left raw, these flow through parse → LineItem.statement_currency → the
Fusion `Currency` field and get rejected by Oracle (or silently miss FX lookup,
since fx_service keys on 3-letter codes).

`normalize_currency(value)` returns the canonical ISO code, or None when the
value can't be mapped confidently. Callers decide what to do with None:
  - config save  → reject (the wizard's ISO dropdown makes this rare)
  - per-row parse → fall back to the config's currency + flag (never blocks ingest)
"""
from __future__ import annotations

import re

# Active ISO-4217 codes (the ones realistically seen across the org's banks,
# plus the common majors). Extend freely — membership here is the allow-list.
ISO_4217: set[str] = {
    "AED", "AUD", "BHD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EGP",
    "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KES", "KRW",
    "KWD", "LKR", "MAD", "MXN", "MYR", "NGN", "NOK", "NZD", "OMR", "PHP",
    "PKR", "PLN", "QAR", "RON", "RUB", "SAR", "SEK", "SGD", "THB", "TRY",
    "TWD", "TZS", "UGX", "USD", "VND", "ZAR",
}

# Non-standard spellings → ISO. Keys are COMPACT (letters/digits only, uppercased);
# normalize_currency() compacts the input the same way before looking up here.
CURRENCY_ALIASES: dict[str, str] = {
    "EURO": "EUR", "EUROS": "EUR", "EUROSED": "EUR",
    "STERLING": "GBP", "POUND": "GBP", "POUNDS": "GBP", "POUNDSTERLING": "GBP",
    "GBPSTERLING": "GBP", "UKP": "GBP", "BRITISHPOUND": "GBP",
    "USDOLLAR": "USD", "USDOLLARS": "USD", "DOLLAR": "USD", "DOLLARS": "USD",
    "USDOLLARUSD": "USD",
    "RUPEE": "INR", "RUPEES": "INR", "RS": "INR", "INRRS": "INR", "INDIANRUPEE": "INR",
    "YEN": "JPY", "JAPANESEYEN": "JPY",
    "FRANC": "CHF", "SWISSFRANC": "CHF",
    "YUAN": "CNY", "RENMINBI": "CNY", "RMB": "CNY",
    "DIRHAM": "AED", "UAEDIRHAM": "AED",
    "RIYAL": "SAR", "SAUDIRIYAL": "SAR",
    "RAND": "ZAR",
    "AUSDOLLAR": "AUD", "AUSSIEDOLLAR": "AUD", "AUSTRALIANDOLLAR": "AUD",
    "CANDOLLAR": "CAD", "CANADIANDOLLAR": "CAD",
    "SGDOLLAR": "SGD", "SINGAPOREDOLLAR": "SGD",
    "HKDOLLAR": "HKD", "HONGKONGDOLLAR": "HKD",
    "NZDOLLAR": "NZD", "NEWZEALANDDOLLAR": "NZD",
}

# Currency symbols. `$` defaults to USD (by far the most common on the org's
# statements); a bank that means CAD/AUD/SGD should configure the ISO code.
CURRENCY_SYMBOLS: dict[str, str] = {
    "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY", "$": "USD",
}


def normalize_currency(value) -> str | None:
    """Return the canonical ISO-4217 code for `value`, or None if it can't be
    mapped. Handles already-ISO codes, spelled-out names, and symbols."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in ("nan", "none", "nat"):
        return None

    up = raw.upper()
    if up in ISO_4217:
        return up

    # Symbol anywhere in the string (e.g. "€", "EUR €").
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in raw:
            return code

    compact = re.sub(r"[^A-Z0-9]", "", up)
    if compact in ISO_4217:
        return compact
    if compact in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[compact]

    return None
