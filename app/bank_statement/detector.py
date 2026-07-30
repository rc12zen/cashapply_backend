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
     • matched, but the file also holds an
       account with no config ................. INCOMPLETE_ACCOUNTS (must configure)
     • many matches, different layouts ......... AMBIGUOUS (user must choose)
     • zero matches ............................ UNKNOWN (→ wizard)

INCOMPLETE_ACCOUNTS exists because a multi-account file is only safe to ingest
when EVERY account in it is configured. Ingesting a partially-configured file
would silently attribute the unconfigured accounts' rows to whichever account
won first-fit. Junk tokens (a "TOTAL" footer row scanned by a column locator)
are filtered out by account_validation.account_reject_reason() before this
check, so they can never block a file.

`config_key` is the matched account number. `detection.config` is the matched
*recipe* (source/fields/credit_rule/… + display_name), which the parser consumes
unchanged.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from .configs.account_loader import (  # noqa: F401
    load_account_configs, load_bank_ou_mapping, last4_index, active_recipe,
)
from .account_locator import extract_account_groups, normalize_account, match_key
from .account_validation import account_reject_reason
from .ou_resolver import resolve_ou

logger = logging.getLogger("cashapply.ingestion.detector")


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
    reason:               Optional[str]  = None    # "UNKNOWN" | "AMBIGUOUS" | "INCOMPLETE_ACCOUNTS"
    ambiguous_candidates: list           = field(default_factory=list)
    # Accounts present in the file that have NO config for this format (junk
    # tokens already filtered). Non-empty ⇒ reason == "INCOMPLETE_ACCOUNTS".
    unregistered_accounts: list          = field(default_factory=list)
    # Accounts present in the file that ARE already configured. Paired with
    # unregistered_accounts so the UI can say "3 of 4 are configured, 1 isn't"
    # instead of only naming what's missing.
    matched_accounts:      list          = field(default_factory=list)
    # Retired: mixed header cells no longer block. A config's registered account
    # is the single answer for a cell/fixed row mapping (see rows_span_accounts).
    # Kept so any caller still reading it sees an empty list rather than an
    # AttributeError.
    unresolved_mixed_cells: list         = field(default_factory=list)
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


def rows_span_accounts(recipe: dict) -> bool:
    """Do this recipe's ROWS belong to more than one account?

    True only when the per-row `account_number` field is a COLUMN — that's the
    only mapping where each row can name a different account, and therefore the
    only case where every account in the file needs its own config + OU.

    A cell/fixed/concat mapping gives every row the SAME account, so exactly one
    account matters: the one the config is registered under. A header cell naming
    a main and its sub-account ("41678876 & 41678884") is therefore NOT a reason
    to demand a second config — the config already declares which of them it is
    for, and changing that means adding a new config.

    Also requires the locator to be column-based. With a cell locator the wizard
    offers a single "pick the account this config is for", so there would be no
    route to configure the extra accounts a block demanded — a dead end. That
    combination is unsupported for now: the check simply doesn't run.
    """
    field = next((f for f in (recipe.get("fields") or [])
                  if f.get("name") == "account_number"), None)
    if (field or {}).get("from", {}).get("type") != "column":
        return False
    loc = recipe.get("account_locator") or {}
    if loc.get("type") == "column":
        return True
    return loc.get("type") == "regex" and (loc.get("in") or {}).get("type") == "column"


def _collect_matches(filepath: str, fmt: str):
    """Return (matches, extracted_accounts, views, registered_keys).

    matches       = list of (account_number, entry, recipe) whose FULL account is in the file
    views[sig]    = {"accounts": set[str]} — what one (locator, source) pair finds
                    in this file. Safe to cache by that signature because
                    extraction depends on nothing else.
    registered_keys = match_key of every account that HAS a config for this format
                    (the denominator for "is this file fully configured?").
    """
    configs = load_account_configs()
    # recipes[fmt] is an append-only list of version objects; detection always uses
    # the ACTIVE (latest) version. A format counts as present only when its version
    # list is a non-empty list.
    candidates = [
        (acct, entry, active_recipe(entry, fmt))
        for acct, entry in configs.items()
        if isinstance((entry.get("recipes") or {}).get(fmt), list) and entry["recipes"][fmt]
    ]
    logger.info(
        "[detect] file=%r format=%r -- %d candidate account(s) have an active '%s' recipe: %s",
        filepath, fmt, len(candidates), fmt, [c[0] for c in candidates],
    )
    if not candidates:
        return [], set(), {}, set()

    # Extract once per distinct (locator, source) — many configs share a layout,
    # and extraction depends on nothing else, so this cache is sound.
    views: dict[str, dict] = {}
    for _acct, _entry, recipe in candidates:
        sig = _extract_sig(recipe)
        if sig in views:
            continue
        groups = extract_account_groups(
            filepath, recipe.get("account_locator", {}), recipe.get("source", {})
        )
        views[sig] = {"accounts": {a for g in groups for a in g}}

    all_extracted: set = set().union(*(v["accounts"] for v in views.values())) if views else set()
    registered_keys = {match_key(e.get("account_number", a)) for a, e, _ in candidates}
    registered_keys.discard("")

    matches = []
    for acct, entry, recipe in candidates:
        extracted_keys = {match_key(x) for x in views[_extract_sig(recipe)]["accounts"]}
        registered_value = entry.get("account_number", acct)
        ckey = match_key(registered_value)
        is_match = bool(ckey and ckey in extracted_keys)
        # LOGGING: this is THE line that answers "why did/didn't this
        # account match" -- for every candidate, shows exactly what its
        # OWN locator extracted from THIS file, what that candidate's
        # OWN registered account_number normalizes to, and whether they
        # lined up. This is what would have shown, in plain sight, that
        # the "HSBC" entry's locator target cell literally contained the
        # text "HSBC" in the PLN file too -- a false-positive match on a
        # non-numeric placeholder "account number", not a real account
        # number collision.
        logger.info(
            "[detect]   candidate acct=%r registered_account_number=%r -> match_key=%r | "
            "this_recipe's_locator_extracted=%s (match_keys=%s) | MATCHED=%s",
            acct, registered_value, ckey, sorted(views[_extract_sig(recipe)]["accounts"]),
            sorted(extracted_keys), is_match,
        )
        if is_match:
            matches.append((acct, entry, recipe))

    logger.info(
        "[detect] file=%r format=%r -- RESULT: %d match(es) out of %d candidate(s): %s",
        filepath, fmt, len(matches), len(candidates), [m[0] for m in matches],
    )
    return matches, all_extracted, views, registered_keys


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_config(filepath: str) -> DetectionResult:
    result = DetectionResult()
    fmt = file_format(filepath)
    result.file_format = fmt

    try:
        matches, extracted, views, registered_keys = _collect_matches(filepath, fmt)
    except Exception as e:
        logger.exception("[detect] detect_config RAISED for file=%r", filepath)
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
            logger.warning(
                "[detect] file=%r -> UNKNOWN: extracted account(s) %s but none are "
                "registered for format=%r. If you believe this account IS registered, "
                "check whether its recipe's account_locator is pointing at the wrong "
                "cell/column for this specific file.",
                filepath, sorted(extracted), fmt,
            )
        else:
            result.method_detail = (
                f"No account could be extracted (or no '{fmt}' recipe exists). "
                f"Add a config or select manually."
            )
            logger.warning(
                "[detect] file=%r -> UNKNOWN: NO account number could be extracted at all "
                "by ANY candidate's locator for format=%r. Either no recipe exists for this "
                "format yet, or every candidate's locator failed against this file's layout.",
                filepath, fmt,
            )
        return result

    layouts = {_layout_sig(m[2]) for m in matches}

    # ── Single match, or several accounts sharing one layout (multi-account) ──
    if len(matches) == 1 or len(layouts) == 1:
        acct, entry, recipe = matches[0]     # first fit
        view = views.get(_extract_sig(recipe), {"accounts": set()})

        # A file whose ROWS span several accounts is only safe to ingest when every
        # one of them is configured — otherwise the unconfigured accounts' rows get
        # attributed to whichever account won first-fit. When rows all share one
        # account (a cell/fixed mapping) there is nothing to check: the config's
        # registered account is the answer, and a header cell that happens to name
        # a sub-account too is not a second account to configure.
        #
        # Junk tokens (a "TOTAL"/"PAGE 1 OF 1" row picked up by a column locator)
        # are filtered here — without that this check would be a permanent dead end.
        unregistered: list[str] = []
        already_configured: list[str] = []
        if rows_span_accounts(recipe):
            unregistered = sorted(
                a for a in view["accounts"]
                if match_key(a) not in registered_keys and not account_reject_reason(a)
            )
            already_configured = sorted(
                a for a in view["accounts"] if match_key(a) in registered_keys
            )
        if unregistered:
            result.reason  = "INCOMPLETE_ACCOUNTS"
            result.success = False
            result.unregistered_accounts  = unregistered
            result.matched_accounts       = already_configured
            # Keep the matched recipe/account on the result: the UI needs it to
            # offer "add these accounts to this existing config".
            result.config_key     = acct
            result.config         = _recipe_config(recipe, entry)
            result.account_number = normalize_account(entry.get("account_number", acct))
            result.candidates     = [_candidate(a, e, fmt) for a, e, _ in matches]
            result.method_detail = (
                f"{len(unregistered)} account(s) with no '{fmt}' config: "
                f"{', '.join(unregistered[:5])}"
            )
            logger.warning(
                "[detect] file=%r -> INCOMPLETE_ACCOUNTS: matched config_key=%r but its "
                "per-row account COLUMN also holds unconfigured account(s) %s -- ingestion "
                "blocked until every account whose rows this file carries is configured.",
                filepath, acct, unregistered,
            )
            return result

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
            logger.info("[detect] file=%r -> MATCHED account=%r config_key=%r (single match)",
                        filepath, result.account_number, acct)
        else:
            result.method_detail = (
                f"{len(matches)} registered accounts in file share one layout → "
                f"first fit '{acct}' ({fmt})"
            )
            logger.info(
                "[detect] file=%r -> MATCHED %d accounts sharing one layout, first-fit "
                "config_key=%r chosen out of: %s",
                filepath, len(matches), acct, [m[0] for m in matches],
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
    logger.warning(
        "[detect] file=%r -> AMBIGUOUS: %d configs matched with DIFFERENT layouts: %s",
        filepath, len(matches), [m[0] for m in matches],
    )
    return result


def list_matching_configs(filepath: str) -> list[dict]:
    """Every account config whose full account appears in the file (for the picker)."""
    fmt = file_format(filepath)
    try:
        matches, _extracted, _views, _registered = _collect_matches(filepath, fmt)
    except Exception:
        return []
    return [_candidate(a, e, fmt) for a, e, _ in matches]