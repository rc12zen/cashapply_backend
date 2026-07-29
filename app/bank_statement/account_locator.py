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

Mixed cells and `aliases`
-------------------------
A single cell can name several accounts ("41678876 & 41678884" — a main and its
sub-account). split_accounts() surfaces both, but a statement ROW whose account
cell names two accounts has no single correct receipt target, so the wizard asks
the user once, at config time, which one is the primary. That choice is stored in
the recipe as:

    "account_aliases": {"41678876|41678884": "41678876"}

keyed by alias_key() — the sorted tokens joined by "|", so the mapping survives a
change of separator, spacing or order in a later file. The alias is applied at
EXTRACTION time (not only at parse time) so detection, the
"is every account in this file configured?" check, and the parser all agree on
which accounts a file actually contains.

Public API
----------
normalize_account(value)          -> normalized string ("" if empty)
last4(value)                      -> last-4 of the normalized value
split_accounts(value)             -> list[str]  (one cell → 1+ accounts; splits "A & B")
alias_key(tokens)                 -> stable key for an account_aliases entry
collapse_tokens(tokens, aliases)  -> list[str]  (mixed cell → its mapped primary)
resolve_cell_accounts(value, aliases) -> list[str]  (split + collapse in one step)
extract_account_groups(filepath, locator, source) -> list[list[str]]  (per-cell, pre-alias)
extract_accounts(filepath, locator, source, aliases) -> set[str]  (normalized, post-alias)
mixed_groups(groups)              -> list[list[str]]  (the cells holding 2+ accounts)
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


def alias_key(tokens: list[str]) -> str:
    """Stable key for an `account_aliases` entry.

    Sorted + "|"-joined so the mapping a user made once ("41678876 & 41678884"
    -> 41678876) still applies when a later file writes the same pair with a
    different separator, spacing or order ("41678884 / 41678876").
    """
    return "|".join(sorted(t for t in tokens if t))


def collapse_tokens(tokens: list[str], aliases: dict | None = None) -> list[str]:
    """Collapse a multi-account cell to the primary account the user chose.

    Single-account cells pass through untouched. A multi-account cell with no
    alias entry is returned AS-IS (all tokens) — deliberately, so the caller can
    see it is unresolved and prompt for a choice rather than guessing.
    """
    if len(tokens) <= 1 or not aliases:
        return tokens
    chosen = aliases.get(alias_key(tokens))
    if not chosen:
        return tokens
    n = normalize_account(chosen)
    return [n] if n else tokens


def resolve_cell_accounts(value, aliases: dict | None = None) -> list[str]:
    """split_accounts() + collapse_tokens() — the single entry point callers
    should use when they need "which account(s) does this cell mean?"."""
    return collapse_tokens(split_accounts(value), aliases)


def mixed_groups(groups: list[list[str]]) -> list[list[str]]:
    """The cells that named 2+ accounts, de-duplicated by alias_key.

    Feeds the wizard's "pick the primary for this cell" prompt and detection's
    unresolved-mixed-cell reporting.
    """
    seen: set[str] = set()
    out: list[list[str]] = []
    for g in groups:
        if len(g) <= 1:
            continue
        k = alias_key(g)
        if k not in seen:
            seen.add(k)
            out.append(g)
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
            df = ExtractorFactory.extract(filepath, source)
            name = locator.get("name")
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
                "raw_value_count=%d -> distinct_normalized=%s mixed_cells=%s",
                filepath, name, bool(name and name in df.columns), raw_count,
                sorted({t_ for g in groups for t_ in g}),
                [alias_key(g) for g in mixed_groups(groups)],
            )

        elif t == "regex":
            pattern = re.compile(locator.get("pattern", ""))
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
                filepath, locator.get("pattern", ""), len(texts),
                sorted({t_ for g in groups for t_ in g}),
            )
    except Exception as exc:
        logger.warning(
            "[locator] extraction RAISED for file=%r locator=%r -- returning partial groups. error=%s",
            filepath, locator, exc,
        )
        return groups

    return groups


def extract_accounts(filepath: str, locator: dict, source: dict | None = None,
                     aliases: dict | None = None) -> set[str]:
    """Return the set of normalized account numbers this locator finds in the file.
    One value for a single-account file; many for a multi-account (column) file.

    `aliases` is the recipe's `account_aliases` map — a cell that names several
    accounts collapses to the single primary the user picked at config time, so a
    sub-account never counts as an account this file "contains". Never raises —
    returns an empty set on any failure."""
    groups = extract_account_groups(filepath, locator, source)
    out: set[str] = set()
    for g in groups:
        out.update(collapse_tokens(g, aliases))
    return out