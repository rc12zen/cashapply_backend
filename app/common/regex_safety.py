"""
app.common.regex_safety
=========================
THE single place a config-supplied regular expression is turned into a
compiled pattern.

WHY THIS EXISTS
---------------
Several config values are regular expressions that originate outside the
codebase (a saved recipe's credit rule / filename_pattern / transform, a
settlement identifier row). Python's `re` is a backtracking engine, so a
pattern with nested quantifiers -- the classic `(a+)+$` shape -- can take
exponential time on a modest input. Feeding one of those to `re.compile()`
and then running it is a Regular Expression Denial of Service (ReDoS,
CWE-1333): one request pins a CPU core indefinitely.

The damage is not request-scoped. Patterns are PERSISTED in the recipe
JSON, and a saved pattern re-runs on every detection/ingestion -- including
inside the background worker -- so one bad value wedges analysis runs from
then on, not just the call that stored it.

WHAT THIS DOES
--------------
`safe_compile()` rejects a pattern BEFORE compiling it if it is over-long
or contains a nested-quantifier construct, then compiles it with a cache.
Rejection raises AppError(CONFIG_PATTERN_INVALID), so a bad pattern is
refused at save time with a clear message rather than silently persisting
and detonating later during a run.

`safe_search()` additionally caps how much text is scanned. Backtracking
blowup scales with input length, so bounding the subject string bounds the
worst case even for a pattern the checks below did not catch.

WHAT THIS IS NOT
----------------
Not a proof of safety. Detecting every catastrophic pattern is undecidable
in general; the checks here catch the well-known shapes. The real
protection is layered: the UI no longer offers free-text regex entry at
all (the account locator uses a fixed server-side pattern, and row
exclusions dropped their regex option), so these paths should only ever
see the small set of patterns the app itself produces. This module is the
backstop for the API, which still accepts a recipe body directly.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .error_codes import ErrorCode
from .errors import AppError

# A legitimate config pattern in this app is short -- "(?i)cr", an account
# shape, a narrative fragment. Hundreds of characters means either a mistake
# or an attack; either way it should not reach the engine.
MAX_PATTERN_LENGTH = 200

# How much of a subject string safe_search() will scan. Bank narratives and
# filenames are far shorter than this; the cap only bites on pathological
# input, where it is exactly what we want.
MAX_SUBJECT_LENGTH = 4096

# Nested quantifiers -- a quantified group that is itself quantified, e.g.
# "(a+)+", "(a*)*", "(a+){2,}", "(?:x|y)+*". This is the shape behind
# essentially every practical catastrophic-backtracking example, because it
# gives the engine exponentially many ways to split the same input.
_NESTED_QUANTIFIER_RE = re.compile(r"\([^)]*[+*}][^)]*\)\s*[+*{]")

# Two adjacent unbounded quantifiers applied to the same atom, e.g. "a+*"
# or ".*+" -- also an exponential-blowup shape.
_STACKED_QUANTIFIER_RE = re.compile(r"[+*]\s*[+*]")


def _reject(pattern: str, why: str) -> None:
    raise AppError(
        ErrorCode.CONFIG_PATTERN_INVALID,
        detail=f"{why} (pattern: {pattern[:60]!r})",
    )


@lru_cache(maxsize=256)
def _compile_cached(pattern: str, flags: int) -> re.Pattern:
    return re.compile(pattern, flags)


def safe_compile(pattern: str | None, flags: int = 0) -> re.Pattern | None:
    """Compile `pattern`, refusing anything unsafe or malformed.

    Returns None for an empty/None pattern -- every caller here treats "no
    pattern configured" as "this rule does not apply", which is not an
    error. Raises AppError(CONFIG_PATTERN_INVALID) for a pattern that is
    too long, structurally dangerous, or not valid regex syntax.
    """
    if not pattern:
        return None

    if len(pattern) > MAX_PATTERN_LENGTH:
        _reject(pattern, f"pattern is longer than {MAX_PATTERN_LENGTH} characters")

    if _NESTED_QUANTIFIER_RE.search(pattern):
        _reject(pattern, "pattern nests one repetition inside another, which can hang the matcher")

    if _STACKED_QUANTIFIER_RE.search(pattern):
        _reject(pattern, "pattern stacks two repetition operators, which can hang the matcher")

    try:
        return _compile_cached(pattern, flags)
    except re.error as exc:
        _reject(pattern, f"not a valid pattern ({exc})")
    return None  # unreachable -- _reject always raises; keeps type checkers happy


def safe_search(pattern: str | None, subject: str, flags: int = 0):
    """safe_compile() + a bounded search. Returns None when there is no
    pattern or no match, so callers can treat both the same way."""
    compiled = safe_compile(pattern, flags)
    if compiled is None:
        return None
    return compiled.search(str(subject)[:MAX_SUBJECT_LENGTH])


def is_safe_pattern(pattern: str | None) -> bool:
    """Non-raising form, for validating a whole recipe at save time without
    aborting on the first bad entry."""
    try:
        safe_compile(pattern)
        return True
    except AppError:
        return False
