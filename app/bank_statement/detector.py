"""
app.bank_statement.detector
============================
Account-number-based bank statement detection (replaces the old
fingerprint/filename engine).

Algorithm
---------
1. Determine the file FORMAT from its extension (xlsm→xlsx, txt→csv).
2. Gather candidate recipes: every account config that has a recipe for this format.
3. For each distinct account-locator, extract the account number(s) from the file
   once (single value for header-cell files; many for multi-account column files).
4. Match: a config matches when its **full account number** is among the extracted
   accounts. (Last-4 is only the fast index; the full number is authoritative, so
   last-4 collisions resolve deterministically — no prompt needed.)
5. Resolve:
     • exactly one match ....................... use its recipe
     • many matches, all the same layout ....... first fit (multi-account file)
     • many matches, different layouts ......... AMBIGUOUS (user must choose)
     • zero matches ............................ UNKNOWN (→ wizard)

`config_key` is the matched account number. `detection.config` is the matched
*recipe* (source/fields/credit_rule/… + display_name), which the parser consumes
unchanged.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .configs.account_loader import (  # noqa: F401
    load_account_configs, load_bank_ou_mapping, last4_index, active_recipe,
)
from .account_locator import extract_accounts, normalize_account, match_key
from .ou_resolver import resolve_ou


# ---------------------------------------------------------------------------
# DetectionResult (public — consumed by parser / routes / orchestrator)
# ---------------------------------------------------------------------------

@dataclass
class DetectionResult:
    config_key:           Optional[str]  = None   # matched account number
    config:               Optional[dict] = None   # matched recipe (+ display_name/meta)
    ou_info:              Optional[dict] = None
    step_used:            Optional[int]  = None
    method_detail:        str            = ""
    account_number:       Optional[str]  = None
    file_format:          Optional[str]  = None
    success:              bool           = False
    errors:               list           = field(default_factory=list)
    confidence:           int            = 0
    reason:               Optional[str]  = None    # "UNKNOWN" | "AMBIGUOUS"
    ambiguous_candidates: list           = field(default_factory=list)
    candidates:           list           = field(default_factory=list)
    suggestions:          list           = field(default_factory=list)
    trace:                list           = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FMT_ALIASES = {"xlsm": "xlsx", "txt": "csv"}


def file_format(filepath: str) -> str:
    """Format from file extension (trusted). xlsm→xlsx, txt→csv."""
    ext = os.path.splitext(filepath)[1].lower().lstrip(".")
    return _FMT_ALIASES.get(ext, ext)


def _ou_info(account: str) -> Optional[dict]:
    info = resolve_ou(account)
    if not info:
        return None
    return {
        "ou_number":     info.get("ou_number"),
        "ou":            info.get("business_unit"),   # "PUNE(111)" etc.
        "business_unit": info.get("business_unit"),
        "bank":          info.get("bank"),
    }


def _recipe_config(recipe: dict, entry: dict) -> dict:
    """The dict handed to the parser: the recipe plus human/meta fields."""
    return {
        **recipe,
        "display_name":   entry.get("display_name", ""),
        "account_number": entry.get("account_number"),
        "bank":           entry.get("bank"),
        "currency":       entry.get("currency"),
    }


def _layout_sig(recipe: dict) -> str:
    """Signature of the parse-relevant layout — used to tell whether several
    matched per-account configs actually share the same parsing recipe."""
    return json.dumps(
        {"source": recipe.get("source"), "fields": recipe.get("fields"),
         "credit_rule": recipe.get("credit_rule")},
        sort_keys=True,
    )


def _extract_sig(recipe: dict) -> str:
    return json.dumps([recipe.get("account_locator"), recipe.get("source", {})], sort_keys=True)


def _candidate(account: str, entry: dict, fmt: str) -> dict:
    return {
        "config_key":     account,
        "account_number": entry.get("account_number", account),
        "display_name":   entry.get("display_name", account),
        "bank":           entry.get("bank"),
        "currency":       entry.get("currency"),
        "format":         fmt,
    }


def _collect_matches(filepath: str, fmt: str):
    """Return (matches, extracted_accounts).
    matches = list of (account_number, entry, recipe) whose FULL account is in the file."""
    configs = load_account_configs()
    # recipes[fmt] is an append-only list of version objects; detection always uses
    # the ACTIVE (latest) version. A format counts as present only when its version
    # list is a non-empty list.
    candidates = [
        (acct, entry, active_recipe(entry, fmt))
        for acct, entry in configs.items()
        if isinstance((entry.get("recipes") or {}).get(fmt), list) and entry["recipes"][fmt]
    ]
    if not candidates:
        return [], set()

    # Extract once per distinct (locator, source) — many configs share a layout.
    extract_cache: dict[str, set] = {}
    for _acct, _entry, recipe in candidates:
        sig = _extract_sig(recipe)
        if sig not in extract_cache:
            extract_cache[sig] = extract_accounts(
                filepath, recipe.get("account_locator", {}), recipe.get("source", {})
            )

    all_extracted: set = set().union(*extract_cache.values()) if extract_cache else set()

    matches = []
    for acct, entry, recipe in candidates:
        extracted_keys = {match_key(x) for x in extract_cache[_extract_sig(recipe)]}
        ckey = match_key(entry.get("account_number", acct))
        if ckey and ckey in extracted_keys:   # full-account match (leading-zero-safe)
            matches.append((acct, entry, recipe))
    return matches, all_extracted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_config(filepath: str) -> DetectionResult:
    result = DetectionResult()
    fmt = file_format(filepath)
    result.file_format = fmt

    try:
        matches, extracted = _collect_matches(filepath, fmt)
    except Exception as e:
        result.errors.append(str(e))
        result.method_detail = f"Detection error: {e}"
        return result

    # ── No registered account matched ────────────────────────────────────────
    if not matches:
        result.reason = "UNKNOWN"
        result.success = False
        if extracted:
            result.account_number = sorted(extracted)[0]
            result.method_detail = (
                f"Extracted account(s) {sorted(extracted)[:3]} but none are registered "
                f"for format '{fmt}'. Add a config."
            )
        else:
            result.method_detail = (
                f"No account could be extracted (or no '{fmt}' recipe exists). "
                f"Add a config or select manually."
            )
        return result

    layouts = {_layout_sig(m[2]) for m in matches}

    # ── Single match, or several accounts sharing one layout (multi-account) ──
    if len(matches) == 1 or len(layouts) == 1:
        acct, entry, recipe = matches[0]     # first fit
        result.success       = True
        result.config_key    = acct
        result.config        = _recipe_config(recipe, entry)
        result.account_number = normalize_account(entry.get("account_number", acct))
        result.ou_info       = _ou_info(result.account_number)
        result.step_used     = 1
        result.confidence    = len(matches)
        result.candidates    = [_candidate(a, e, fmt) for a, e, _ in matches]
        if len(matches) == 1:
            result.method_detail = f"Account {result.account_number} → '{acct}' ({fmt})"
        else:
            result.method_detail = (
                f"{len(matches)} registered accounts in file share one layout → "
                f"first fit '{acct}' ({fmt})"
            )
        return result

    # ── Multiple matches with DIFFERENT layouts → genuine ambiguity ───────────
    result.reason = "AMBIGUOUS"
    result.success = False
    result.ambiguous_candidates = [m[0] for m in matches]
    result.candidates = [_candidate(a, e, fmt) for a, e, _ in matches]
    result.method_detail = (
        f"{len(matches)} configs matched with different layouts — choose one."
    )
    return result


def list_matching_configs(filepath: str) -> list[dict]:
    """Every account config whose full account appears in the file (for the picker)."""
    fmt = file_format(filepath)
    try:
        matches, _ = _collect_matches(filepath, fmt)
    except Exception:
        return []
    return [_candidate(a, e, fmt) for a, e, _ in matches]