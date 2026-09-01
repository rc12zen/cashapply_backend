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

Mixed cells ("41678876 & 41678884")
-----------------------------------
A single cell can name several accounts — typically a main account and its
sub-account, in a header/metadata cell. split_accounts() surfaces both, and
detection matches a config when ITS registered account is among them, so a
config registered under 41678876 still recognises the file.

Which of them a config is FOR is decided once, in the wizard, by registering the
config under one of them. That registered account is then the single answer for
that config — see parser's step 5f, which posts every row of a cell/fixed-mapped
statement against it rather than re-deriving it from the cell. There is
deliberately no per-cell "primary account" mapping: an earlier `account_aliases`
recipe key did that job and is now redundant (it is ignored if present on an old
recipe, which keeps those recipes valid).

Public API
----------
normalize_account(value)          -> normalized string ("" if empty)
last4(value)                      -> last-4 of the normalized value
match_key(value)                  -> leading-zero-insensitive form for matching
split_accounts(value)             -> list[str]  (one cell → 1+ accounts; splits "A & B")
extract_account_groups(filepath, locator, source) -> list[list[str]]  (grouped per cell)
extract_accounts(filepath, locator, source) -> set[str]  (flat, normalized)
"""
from __future__ import annotations

import logging
import re

from .snapshot import FileSnapshot

logger = logging.getLogger("cashapply.ingestion.locator")


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
#
# '-' IS a separator, same as '&'. An HSBC UK header cell reads
# "401310-41678876" -- sort code, hyphen, account number -- and only the second
# half, 41678876, is the account this file's rows post against. Without the
# hyphen here, normalize_account() stripped it and the two halves fused into
# "40131041678876", which then flowed all the way through to Oracle as
# RemittanceBankAccountNumber: an account number that exists nowhere, on every
# row of the file. Note the `regex` locator never had this bug -- ACCOUNT_PATTERN
# matches alnum-only runs, so it already broke on the hyphen. This brings the
# `cell`/`column` locators into line with it rather than the other way round.
#
# Which half a config is FOR is decided once in the wizard, by registering under
# one of them -- exactly as it already works for '&' cells. parser._row_account()
# then picks the registered account out of the pair on every row.
#
# A grouped account that happens to use hyphens ("4013-1041-6788-76") still
# survives: none of its four fragments clears _looks_like_account()'s 6-char bar,
# so split_accounts() falls through to normalizing the whole cell.
_ACCT_SEP = re.compile(r"\s*(?:&|,|/|\+|-|\band\b)\s*", re.IGNORECASE)

# The ONE pattern the "regex" locator type ever uses: any 6-34 char
# alphanumeric run containing at least one digit. Covers plain numeric
# accounts ("000205024781") and IBAN-style alphanumerics ("GB29NWBK…"),
# including when buried in text like "… (INR) - 000205024781".
#
# SECURITY: this is deliberately a server-side CONSTANT, not something the
# request can set. It used to be `re.compile(locator.get("pattern", ""))`,
# i.e. an attacker-controlled regex reaching a backtracking engine -- a
# ReDoS sink (CWE-1333) flagged by VAPT. A caller-supplied "pattern" key is
# now ignored entirely; the wizard's "Advanced: edit pattern" box that fed
# it has been removed. Mirrors AUTO_ACCOUNT_REGEX in ConfigBuilderWizard.tsx,
# which is only used there to describe the behaviour to the user.
ACCOUNT_PATTERN = re.compile(r"((?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,34})")


def _looks_like_account(n: str) -> bool:
    """A normalized token that plausibly is an account: 6+ chars with a digit."""
    return len(n) >= 6 and any(ch.isdigit() for ch in n)


def split_accounts(value) -> list[str]:
    """Split a cell that may hold several accounts into normalized account tokens.

    Handles main/sub-account headers such as "41678876 & 41678884", and sort-code
    style pairs such as "401310-41678876", by splitting on explicit separators
    (& , / + - 'and') only — never on spaces — then normalizing each piece. If no
    piece looks like an account (e.g. a lone value with no separator, or a grouped
    account like "4013-1041-6788-76" whose fragments are all too short), falls back
    to the whole normalized cell so single-account files are unaffected.
    Order-preserving and de-duplicated.
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
        name = src.get("name")
        try:
            # Read as TEXT: pandas would infer a digit-string column as int64 and
            # strip the leading zeros off "000274178".
            df = ExtractorFactory.extract(filepath, source, text_columns=[name] if name else None)
        except Exception:
            return []
        if not name or name not in df.columns:
            return []
        return [str(v) for v in df[name].tolist() if v is not None]

    # default / "sheet" → scan the whole cached region
    return list(snap.iter_values(src.get("sheet")))


def extract_account_groups(filepath: str, locator: dict, source: dict | None = None) -> list[list[str]]:
    """Account tokens this locator finds, grouped BY SOURCE CELL, pre-alias.

    One inner list per raw value the locator targets, so a cell naming a main and
    its sub-account comes back as a single group of two tokens — which is what
    lets callers tell "this file has two accounts" apart from "this one cell names
    two accounts". extract_accounts() flattens this with the alias map applied.
    Never raises — returns whatever was collected before the failure.
    """
    if not locator:
        logger.debug("[locator] no account_locator configured -- returning no groups. file=%r", filepath)
        return []
    source = source or {}
    t = locator.get("type")
    groups: list[list[str]] = []

    try:
        if t == "cell":
            snap = FileSnapshot.from_path(filepath)
            row, col, sheet = locator.get("row", 0), locator.get("col", 0), locator.get("sheet")
            val = snap.cell(row, col, sheet)
            split = split_accounts(val)
            if split:
                groups.append(split)
            # LOGGING: this is the exact spot that explains "how did the
            # system decide this was/wasn't the account number" -- shows
            # the RAW cell text before any cleanup, and what it became
            # after split_accounts()/normalize_account(). If `val` is
            # blank here, the locator's row/col/sheet is pointing at the
            # wrong spot in this file (see the BofA/account-8 case).
            logger.info(
                "[locator] type=cell file=%r sheet=%r row=%s col=%s -> raw=%r normalized=%s",
                filepath, sheet, row, col, val, split,
            )

        elif t == "column":
            from .extractor import ExtractorFactory
            name = locator.get("name")
            # Read the account column as TEXT — otherwise pandas infers int64 and
            # "000274178" arrives as 274178, so the config gets keyed to the wrong
            # account identity (and a 16+ digit account loses precision entirely).
            df = ExtractorFactory.extract(
                filepath, source, text_columns=[name] if name else None)
            raw_count = 0
            if name and name in df.columns:
                for v in df[name].tolist():
                    raw_count += 1
                    split = split_accounts(v)
                    if split:
                        groups.append(split)
            # LOGGING: for column locators, log how many raw values were
            # scanned and the distinct normalized set produced -- if
            # `name` isn't in df.columns at all (e.g. header row
            # misconfigured, so the real column names never got read),
            # raw_count stays 0 and this makes that obvious.
            logger.info(
                "[locator] type=column file=%r column_name=%r column_found=%s "
                "raw_value_count=%d -> distinct_normalized=%s",
                filepath, name, bool(name and name in df.columns), raw_count,
                sorted({t_ for g in groups for t_ in g}),
            )

        elif t == "regex":
            # Fixed server-side pattern -- locator["pattern"] is intentionally
            # NOT read here any more; see ACCOUNT_PATTERN above (ReDoS fix).
            pattern = ACCOUNT_PATTERN
            snap = FileSnapshot.from_path(filepath)
            texts = _regex_texts(locator, filepath, source, snap)
            for text in texts:
                if not text:
                    continue
                # finditer (not search) so a cell holding several account-like
                # values (main & sub) yields every one, not just the first. Every
                # match from ONE text is one group, so a main & sub captured out
                # of the same cell stays recognisable as a mixed cell.
                found = []
                for m in pattern.finditer(str(text)):
                    captured = m.group(m.lastindex) if m.lastindex else m.group(0)
                    n = normalize_account(captured)
                    if n and n not in found:
                        found.append(n)
                if found:
                    groups.append(found)
            logger.info(
                "[locator] type=regex file=%r pattern=%r texts_scanned=%d -> distinct_normalized=%s",
                filepath, ACCOUNT_PATTERN.pattern, len(texts),
                sorted({t_ for g in groups for t_ in g}),
            )
    except Exception as exc:
        logger.warning(
            "[locator] extraction RAISED for file=%r locator=%r -- returning partial groups. error=%s",
            filepath, locator, exc,
        )
        return groups

    return groups


def extract_accounts(filepath: str, locator: dict, source: dict | None = None) -> set[str]:
    """Return the set of normalized account numbers this locator finds in the file.
    One value for a single-account file; several for a multi-account (column) file,
    or for a header cell naming a main and its sub-account.

    Depends only on (locator, source), which is what makes it safe for callers to
    cache by that pair — see detector._collect_matches. (It briefly took an
    `aliases` argument, which broke that assumption: two configs sharing a layout
    but holding different alias maps got one another's collapsed result.)
    Never raises — returns an empty set on any failure."""
    return {a for g in extract_account_groups(filepath, locator, source) for a in g}