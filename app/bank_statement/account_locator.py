"""
app.bank_statement.account_locator
==================================
Extract account number(s) from a bank statement, given a recipe's
`account_locator`. This is the core of account-based detection.

Locator types
-------------
cell   — a fixed header cell:            {"type":"cell", "sheet":"…", "row":1, "col":1}
column — a per-row account column:       {"type":"column", "name":"Account Number"}
regex  — a pattern over a cell/column/   {"type":"regex",
         whole-sheet region               "in":{"type":"column","name":"Description"},
                                           "pattern":"(\\d{6,})\\s*$"}

`column` and `regex-in-column` need the recipe's `source` (header location) so the
DataFrame is built correctly; `cell` and `regex-in-cell`/`regex-in-sheet` read raw
cells via FileSnapshot.

Public API
----------
normalize_account(value)          -> normalized string ("" if empty)
last4(value)                      -> last-4 of the normalized value
split_accounts(value)             -> list[str]  (one cell → 1+ accounts; splits "A & B")
extract_accounts(filepath, locator, source) -> set[str]  (normalized full accounts)
"""
from __future__ import annotations

import re

from .snapshot import FileSnapshot


def normalize_account(value) -> str:
    """Uppercase, keep A-Z0-9 (drop spaces/dashes/dots), drop a trailing '.0'.
    Preserves leading zeros. Returns '' for blank/NaN."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return "".join(ch for ch in s.upper() if ch.isalnum())


def last4(value) -> str:
    n = normalize_account(value)
    return n[-4:] if n else ""


def match_key(value) -> str:
    """Leading-zero-insensitive canonical form for account matching.

    Excel/pandas often read a numeric account like '000274178' as 274178, dropping
    leading zeros. Comparing on the zero-stripped form makes matching robust to that.
    (last4() stays leading-zero-safe because it's the trailing digits.)
    """
    n = normalize_account(value)
    return n.lstrip("0") or n


# Explicit separators that join several accounts in one cell (e.g. a main +
# sub-account header like "41678876 & 41678884"). Deliberately does NOT include
# spaces, so a single grouped account like "0002 0502 4781" stays intact.
_ACCT_SEP = re.compile(r"\s*(?:&|,|/|\+|\band\b)\s*", re.IGNORECASE)


def _looks_like_account(n: str) -> bool:
    """A normalized token that plausibly is an account: 6+ chars with a digit."""
    return len(n) >= 6 and any(ch.isdigit() for ch in n)


def split_accounts(value) -> list[str]:
    """Split a cell that may hold several accounts into normalized account tokens.

    Handles main/sub-account headers such as "41678876 & 41678884" by splitting on
    explicit separators (& , / + 'and') only — never on spaces — then normalizing
    each piece. If no piece looks like an account (e.g. a lone value with no
    separator), falls back to the whole normalized cell so single-account files are
    unaffected. Order-preserving and de-duplicated.
    """
    if value is None:
        return []
    raw = str(value).strip()
    if not raw or raw.lower() in ("nan", "none", "nat"):
        return []
    tokens = [normalize_account(p) for p in _ACCT_SEP.split(raw)]
    acct_like = [t for t in tokens if t and _looks_like_account(t)]
    if not acct_like:
        whole = normalize_account(raw)
        return [whole] if whole else []
    seen: set[str] = set()
    out: list[str] = []
    for t in acct_like:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _regex_texts(locator: dict, filepath: str, source: dict, snap: FileSnapshot) -> list[str]:
    """Collect the candidate text strings a regex locator should scan."""
    src = locator.get("in", {}) or {}
    t = src.get("type")

    if t == "cell":
        return [snap.cell(src.get("row", 0), src.get("col", 0), src.get("sheet"))]

    if t == "column":
        from .extractor import ExtractorFactory
        try:
            df = ExtractorFactory.extract(filepath, source)
        except Exception:
            return []
        name = src.get("name")
        if not name or name not in df.columns:
            return []
        return [str(v) for v in df[name].tolist() if v is not None]

    # default / "sheet" → scan the whole cached region
    return list(snap.iter_values(src.get("sheet")))


def extract_accounts(filepath: str, locator: dict, source: dict | None = None) -> set[str]:
    """Return the set of normalized account numbers this locator finds in the file.
    One value for a single-account file; many for a multi-account (column) file.
    Never raises — returns an empty set on any failure."""
    if not locator:
        return set()
    source = source or {}
    t = locator.get("type")
    out: set[str] = set()

    try:
        if t == "cell":
            snap = FileSnapshot.from_path(filepath)
            val = snap.cell(locator.get("row", 0), locator.get("col", 0), locator.get("sheet"))
            # A header cell may list several accounts (main & sub) — split them.
            out.update(split_accounts(val))

        elif t == "column":
            from .extractor import ExtractorFactory
            df = ExtractorFactory.extract(filepath, source)
            name = locator.get("name")
            if name and name in df.columns:
                for v in df[name].tolist():
                    out.update(split_accounts(v))

        elif t == "regex":
            pattern = re.compile(locator.get("pattern", ""))
            snap = FileSnapshot.from_path(filepath)
            for text in _regex_texts(locator, filepath, source, snap):
                if not text:
                    continue
                # finditer (not search) so a cell holding several account-like
                # values (main & sub) yields every one, not just the first.
                for m in pattern.finditer(str(text)):
                    captured = m.group(m.lastindex) if m.lastindex else m.group(0)
                    n = normalize_account(captured)
                    if n:
                        out.add(n)
    except Exception:
        return out

    return out
