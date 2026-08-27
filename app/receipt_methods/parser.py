"""
app.receipt_methods.parser
=============================
Reads an AR Receipt Methods extract file (.xlsx/.xls/.csv/.txt) and
REGENERATES the receipt-method config oracle/receipt_method_resolver.py
reads to resolve Oracle's ReceiptMethod for a given bank account.

Written to RECEIPT_METHOD_MAP_OUTPUT_PATH (see db/settings.py and
get_output_path() below) -- deliberately OUTSIDE the SVN-tracked
oracle/configs/ directory, where the original hand-curated
receipt_method_map.json (SEED_PATH below) still lives as a read-only
fallback. Never write to SEED_PATH from the watcher -- see SEED_PATH's
own comment for why.

Modeled directly on gl_rates/parser.py (same columns-config-driven
read, same "pure parse -> list of dicts" core function), with one
structural difference: gl_rates writes rows into a DB table that
ACCUMULATES history across files. Receipt methods have no history to
accumulate -- each new extract is simply the current, complete picture of
every account's receipt methods, so this REPLACES the JSON file wholesale
on every run rather than upserting into it. That mirrors how
aging/parser.py treats the aging report as a single "current snapshot",
not gl_rates/parser.py's accumulate-forever model.

CONFIRMED against a real xxzen_ar_receipt_methods_extract.txt on
2026-08-26 (ze42-v-cshuiprd) -- comma-delimited CSV despite the .txt
extension, header row RECEIPT_CLASSES,BANK_ACCOUNT_NAME,BANK_ACCOUNT_NUM,
CURRENCY_CODE,BANK_NAME,LE_NAME,BU_NAME,RECEIPT_METHOD_NAME,BANK_BRANCH_NAME
-- see receipt_methods_columns.json. Two things the first real run caught
that an untested guess couldn't have:

  1. NO separate OU_NUMBER column exists -- it's embedded in BU_NAME (e.g.
     "PUNE(111)") and must be parsed out; see _extract_ou_number() below.
  2. BANK_ACCOUNT_NUM values are NOT always quoted (e.g. 07232560000170),
     so pandas' default type inference reads them as int64 and SILENTLY
     DROPS THE LEADING ZERO (07232560000170 -> 7232560000170) -- producing
     an account number that can never match a real LineItem.account_number.
     Every read call below forces dtype=str for exactly this reason. The
     very first real extract run (146 rows) hit both issues at once: 0
     accounts written, all rows skipped -- see
     receipt_methods/watcher.py's zero-accounts write guard, added the
     same day this was fixed, so a bad column mapping can degrade to "logs
     an error, keeps retrying" instead of "silently wipes out 95 working
     accounts."
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

RECEIPT_METHODS_BUCKET = "receipt-methods"

_COLUMNS_CONFIG = Path(__file__).parent / "receipt_methods_columns.json"

# The ORIGINAL, hand-curated, SVN-tracked file. Read-only from this
# module's point of view -- kept as a fallback SEED for
# oracle/receipt_method_resolver.py to fall back on if the generated file
# below doesn't exist yet, never as a live write target. See
# db/settings.py's RECEIPT_METHOD_MAP_OUTPUT_PATH comment for why: writing
# a daily-regenerated file into the SVN working copy causes exactly the
# `svn status`/`svn revert`/accidental-commit problems that path exists to
# avoid.
SEED_PATH = Path(__file__).parent.parent / "oracle" / "configs" / "receipt_method_map.json"


def get_output_path() -> Path:
    """
    Where write_receipt_method_map() writes by default -- from
    db/settings.py's RECEIPT_METHOD_MAP_OUTPUT_PATH, deliberately OUTSIDE
    the SVN working copy (default "./runtime_data/receipt_method_map.json").
    Read fresh from settings on every call rather than cached at import
    time, so a .env change takes effect on next watcher run without
    needing this module reloaded.
    """
    from ..db.settings import get_settings
    settings = get_settings()
    configured = (getattr(settings, "RECEIPT_METHOD_MAP_OUTPUT_PATH", "") or "").strip()
    return Path(configured).expanduser() if configured else SEED_PATH

# Policy, not data -- this ordering was a business decision (see
# oracle/configs/receipt_method_map.json's original
# "_default_receipt_class_priority_rationale": CashApply only ever
# processes ELECTRONIC bank-statement credit lines, so Direct Deposit/Wire
# classes are preferred over Cash Receipt/Cheque classes, which are
# normally for manually-recorded deposits). NOT derivable from the extract
# file itself, so it is preserved here as a constant across every
# regeneration rather than reset to some default order. If finance
# confirms a different priority, update this list (and the rationale
# string below) by hand -- a fresh extract file cannot tell us this.
_DEFAULT_RECEIPT_CLASS_PRIORITY = [
    "Direct Deposit in bank",
    "New Direct Deposit in Bank",
    "Wire Transfer",
    "New Receipt_EON III",
    "Cash Receipt",
    "New Cash Receipt",
    "Cash Receipt ABSA",
    "Check/D.D. class",
]

_PRIORITY_RATIONALE = (
    "CashApply parses ELECTRONIC bank statement credit lines (NEFT/RTGS/wire/SWIFT), so "
    "'Direct Deposit in bank' / 'Wire Transfer' style classes are the most likely correct "
    "match for statement-driven postings. 'Cash Receipt' and 'Check/D.D. class' are typically "
    "for manually-recorded cash/cheque deposits, not statement lines. CONFIRM this priority "
    "with finance/AR before wiring into the live Oracle posting payload -- do not assume."
)

_COMMENT = (
    "Bank account -> receipt method lookup, built from the AR Receipt Methods extract "
    "(regenerated automatically by app.receipt_methods.watcher / parser.py -- do not hand-edit, "
    "changes will be overwritten on the next extract file). Key = BANK_ACCOUNT_NUM (matches "
    "LineItem.account_number from the parsed bank statement). Value = list of candidate receipt "
    "methods for that account (one account can support several receipt classes e.g. "
    "wire/direct-deposit/cash/cheque). Oracle auto-assigns the ReceiptNumber based on which "
    "ReceiptMethod's document sequence is used -- there is no separate 'receipt number' to build "
    "ourselves; getting ReceiptMethod right IS how the receipt number gets built correctly."
)


def _load_columns_config() -> dict:
    with open(_COLUMNS_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _clean(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip()


# Matches the trailing "(<digits>)" in an Oracle BU display string, e.g.
# "PUNE(111)" -> "111", "PUNE ISD(112)" -> "112" -- same "NAME(ou)" format
# rule_engine/fx_service.py's get_ou_display_name() produces for the Oracle
# payload's BusinessUnit field. There is no separate OU_NUMBER column in
# the real extract (confirmed 2026-08-26) -- BU_NAME is the only place the
# OU number appears, so it has to be parsed out rather than read directly.
_OU_NUMBER_RE = re.compile(r"\((\d+)\)\s*$")


def _extract_ou_number(bu_name: str) -> str:
    m = _OU_NUMBER_RE.search(bu_name or "")
    return m.group(1) if m else ""


def parse_receipt_methods_file(local_path: str) -> list[dict]:
    """
    Pure parsing -- file -> list of row dicts. No file writes, no DB.
    Each dict has: account_number, ou_number, bu_name, receipt_class,
    receipt_method_name, currency, bank_name, bank_account_name,
    bank_branch_name, le_name.

    Rows missing account_number, receipt_class, or receipt_method_name are
    skipped (logged, not raised) -- those three are the fields
    receipt_method_resolver.py actually depends on to resolve/rank a
    method; the rest are descriptive.
    """
    cfg_all = _load_columns_config()
    cfg = cfg_all["DEFAULT"]
    cols = cfg["columns"]

    # dtype=str on EVERY branch, deliberately -- BANK_ACCOUNT_NUM values
    # like 07232560000170 are unquoted in the real extract, so pandas'
    # default type inference reads them as int64 and silently drops the
    # leading zero (07232560000170 -> 7232560000170), producing an account
    # number that can never match a real LineItem.account_number. Confirmed
    # against a real file on 2026-08-26 -- see this module's docstring.
    suffix = Path(local_path).suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(local_path, header=cfg["header_row"], dtype=str)
    elif suffix == ".txt":
        df = pd.read_csv(local_path, header=cfg["header_row"], sep=cfg.get("delimiter", ","), dtype=str)
    else:
        df = pd.read_excel(local_path, sheet_name=cfg["sheet_name"], header=cfg["header_row"], dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    rows: list[dict] = []
    skipped = 0
    for _, row in df.iterrows():
        account_number = _clean(row.get(cols["account_number"]))
        receipt_class = _clean(row.get(cols["receipt_class"]))
        receipt_method_name = _clean(row.get(cols["receipt_method_name"]))

        if not account_number or not receipt_class or not receipt_method_name:
            skipped += 1
            continue

        bu_name = _clean(row.get(cols["bu_name"]))

        rows.append({
            "account_number":      account_number,
            "ou_number":           _extract_ou_number(bu_name),
            "bu_name":             bu_name,
            "receipt_class":       receipt_class,
            "receipt_method_name": receipt_method_name,
            "currency":            _clean(row.get(cols["currency"])),
            "bank_name":           _clean(row.get(cols["bank_name"])),
            "bank_account_name":   _clean(row.get(cols["bank_account_name"])),
            "bank_branch_name":    _clean(row.get(cols["bank_branch_name"])),
            "le_name":             _clean(row.get(cols["le_name"])),
        })

    if skipped:
        logger.warning(
            "[receipt_methods] Skipped %d row(s) missing account_number/receipt_class/"
            "receipt_method_name.", skipped,
        )

    return rows


def _compute_ambiguous_accounts(accounts: dict[str, list[dict]]) -> list[str]:
    """
    An account is 'genuinely ambiguous' the same way
    receipt_method_resolver.py's docstring defines it: two DIFFERENT
    receipt_method_names for the SAME receipt_class (not just multiple
    classes, which resolve cleanly via _default_receipt_class_priority).
    Derived fresh from the extract every run, rather than a hand-maintained
    list -- so this stays correct as accounts are added/removed/renamed
    upstream in Oracle, instead of silently going stale like a hardcoded
    list would.
    """
    ambiguous: list[str] = []
    for account_number, entries in accounts.items():
        by_class: dict[str, set[str]] = {}
        for e in entries:
            by_class.setdefault(e["receipt_class"], set()).add(e["receipt_method_name"])
        if any(len(names) > 1 for names in by_class.values()):
            ambiguous.append(account_number)
    return sorted(ambiguous)


def build_receipt_method_map(local_path: str) -> dict:
    """
    Parses local_path and assembles the FULL receipt_method_map.json
    document -- same top-level shape the resolver already reads
    (_comment, _default_receipt_class_priority,
    _default_receipt_class_priority_rationale,
    _accounts_with_unresolved_ambiguity, accounts).
    """
    rows = parse_receipt_methods_file(local_path)

    accounts: dict[str, list[dict]] = {}
    for r in rows:
        account_number = r.pop("account_number")
        accounts.setdefault(account_number, []).append(r)

    return {
        "_comment": _COMMENT,
        "_default_receipt_class_priority": _DEFAULT_RECEIPT_CLASS_PRIORITY,
        "_default_receipt_class_priority_rationale": _PRIORITY_RATIONALE,
        "_accounts_with_unresolved_ambiguity": _compute_ambiguous_accounts(accounts),
        "_generated_from": Path(local_path).name,
        "_generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accounts": accounts,
    }


def write_receipt_method_map(map_dict: dict, output_path: Path | str | None = None) -> None:
    """
    Writes the regenerated map atomically (temp file + rename) so
    receipt_method_resolver.py's mtime-cached reader
    (app.common.json_cache.load_json_cached) never observes a
    partially-written file mid-write -- same safeguard
    oracle_file_pull/puller.py already uses for the extract files
    themselves.

    output_path defaults to get_output_path() (settings-driven, outside
    the SVN tree) -- NOT SEED_PATH. Passing SEED_PATH explicitly here
    would overwrite the checked-in fallback file, defeating the whole
    point of having one; only pass it if that's genuinely what's needed
    (e.g. a one-off manual re-seed, done deliberately).
    """
    output_path = Path(output_path) if output_path is not None else get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(map_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(output_path)
    logger.info(
        "[receipt_methods] Wrote %d account(s), %d flagged ambiguous, to %s",
        len(map_dict.get("accounts", {})),
        len(map_dict.get("_accounts_with_unresolved_ambiguity", [])),
        output_path,
    )