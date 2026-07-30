"""
app.bank_statement.account_validation
======================================
The structural gate on account-number VALUES. This is the fix for the
config-corruption class of bug: the account number is the single join key for
config detection, BankAccount identity, OU resolution, and row-dedup, so a
metadata LABEL cell (e.g. a heading that reads "Account Number") that silently
became the account value would false-positive-match every other file carrying
that same label — corrupting detection across unrelated configs.

`account_reject_reason(value)` returns a human-readable reason string when a
value does NOT look like a real account number, or None when it's acceptable.
Used at three gates (all in bff/config_builder_routes.py):
  - locate-account : flag label-like candidates before they can be chosen
  - test           : reject a draft whose parsed account values are label-like
  - save           : hard-block saving a bad account identity (unless the SPOC
                     explicitly overrides — see override_account_validation)

Rule (generic structural — handles numeric accounts AND IBANs):
  - normalized length 6-34
  - at least one digit  (kills "Account Number" / "HSBC" outright)
  - does not contain a known column-label or statement-furniture word
    ("account", "iban", "sort code", "page", "total", "balance", …)  (kills
    "Account Number 1" and "Page 1 of 1" — values that sneak a digit past the
    check above)
  - at least half the characters are digits, UNLESS the value is IBAN-shaped
    (2 letters + 2 check digits + 11-30 alphanumerics). This is the general
    catch for mostly-alphabetic statement furniture: "PAGE1OF1" is 8 chars with
    only 2 digits (25%), while a real account is either all digits or an IBAN.
"""
from __future__ import annotations

import re

from .account_locator import normalize_account

MIN_LEN = 6
MAX_LEN = 34

# A genuine account is overwhelmingly numeric. Anything below this that isn't
# IBAN-shaped is statement furniture, not an account. The tightest real IBAN
# (Malta, e.g. MT84MALT011000012345MTLCAST001S) sits at ~55%, and IBANs are
# exempted by shape anyway — see _IBAN_RE.
MIN_DIGIT_RATIO = 0.5

# ISO 13616 IBAN shape: 2-letter country + 2 check digits + 11-30 alphanumerics.
# Exempt from the digit-ratio rule so no legitimate IBAN can be rejected by it.
_IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")

# Distinctive label words, compacted to letters/digits only. A genuine account
# value (numeric or IBAN-style) does not contain these letter sequences, so a
# substring hit is a reliable "this is a heading, not an account" signal.
# The second group is statement FURNITURE — page markers, totals and carried
# balances that a COLUMN account-locator scoops up along with the real accounts
# (this is how "PAGE1OF1" got offered as an account to configure).
_LABEL_TOKENS = (
    "account", "acct", "ibannumber", "sortcode",
    "customername", "customer", "bankname", "narrative", "description",
    "currency", "reference",
    "page", "total", "balance", "broughtforward", "carriedforward",
    "statement", "continued", "summary", "opening", "closing", "grand",
)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def account_reject_reason(value) -> str | None:
    """Return why `value` is not a valid account number, or None if it's fine.

    Purely structural — never touches the DB. Safe to call anywhere."""
    raw = str(value if value is not None else "").strip()
    if not raw or raw.lower() in ("nan", "none", "nat"):
        return "No account number was provided."

    norm = normalize_account(raw)
    if len(norm) < MIN_LEN:
        return f"'{raw}' is too short to be an account number (need at least {MIN_LEN} letters/digits)."
    if len(norm) > MAX_LEN:
        return f"'{raw}' is too long to be an account number (over {MAX_LEN} characters)."
    if not any(ch.isdigit() for ch in norm):
        return f"'{raw}' has no digits — this looks like a heading or label, not an account number."

    compact = _compact(raw)
    for token in _LABEL_TOKENS:
        if token in compact:
            return (f"'{raw}' contains the label text \"{token}\" — pick the actual account "
                    f"number, not a heading/metadata cell.")

    # Mostly-alphabetic values are statement furniture, not accounts. IBANs are
    # exempt by shape so this can never reject a legitimate one.
    if not _IBAN_RE.match(norm):
        digits = sum(1 for ch in norm if ch.isdigit())
        if digits / len(norm) < MIN_DIGIT_RATIO:
            return (f"'{raw}' is mostly letters ({digits} digit(s) in {len(norm)} characters) — "
                    f"an account number is either all digits or a valid IBAN.")

    return None


def is_account_shaped(value) -> bool:
    return account_reject_reason(value) is None
