"""
app.oracle.receipt_method_resolver
=====================================
Resolves the correct Oracle AR ReceiptMethod for a bank account, from
receipt_method_map.json (built from the xxzen_ar_receipt_methods_extract_.csv
Oracle AR Receipt Methods extract).

WHY THIS EXISTS
----------------
fusion_client.py previously hardcoded "ReceiptMethod": "Standard" on every
payload -- a placeholder value that does not appear anywhere in the real
Oracle extract (143 rows, ~95 distinct bank accounts, receipt method names
like "Direct Deposit in Bank fo Pune" / "Cash Receipt Kotak Mahindra170").
Every receipt posted was using a made-up method name.

ORACLE OWNS THE RECEIPT NUMBER, NOT US
----------------------------------------
Oracle auto-generates ReceiptNumber from whichever ReceiptMethod's own
document-numbering source gets used -- there is no separate "receipt number"
field for us to compute. Getting ReceiptMethod right IS how the receipt
number gets built correctly. (See fusion_client.py's _build_receipt_reference
for the SEPARATE concept of an internal idempotency reference -- that is
ours, not Oracle's, and exists only to make retries safe.)

DISAMBIGUATION
---------------
One bank account can appear under several receipt classes (Direct Deposit,
Wire Transfer, Cash Receipt, Check/D.D.) because Oracle lets the same
account receive money through different channels. CashApply only ever
processes ELECTRONIC bank statement credit lines, so
_default_receipt_class_priority (from the config) prefers Direct
Deposit/Wire classes first. This preference has NOT been confirmed with
finance/AR -- see the config's `_default_receipt_class_priority_rationale`.
11 accounts have genuine unresolved ambiguity (two different method names
for the SAME class) -- these are listed in
`_accounts_with_unresolved_ambiguity` and always come back flagged
`ambiguous=True` here regardless of which one we pick.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..common.json_cache import load_json_cached

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent

# The original, hand-curated file, still checked into SVN under
# oracle/configs/ -- used as a fallback ONLY (see _load_receipt_method_map()
# below). Never written to by receipt_methods/watcher.py -- see
# receipt_methods/parser.py's SEED_PATH comment for why the live,
# daily-regenerated file deliberately lives elsewhere.
_SEED_PATH = _HERE / "configs" / "receipt_method_map.json"


def _load_receipt_method_map() -> dict:
    """
    Priority:
      1. RECEIPT_METHOD_MAP_OUTPUT_PATH (db/settings.py) -- the file
         receipt_methods/watcher.py regenerates daily from the live Oracle
         extract, deliberately kept OUTSIDE the SVN working copy.
      2. The checked-in seed at oracle/configs/receipt_method_map.json --
         used only if #1 isn't configured, or is configured but the
         watcher/puller pipeline hasn't produced a file there yet.

    PATCH: was @lru_cache(maxsize=1) — same cross-process staleness bug
    as bank_statement/configs/account_loader.py and
    rule_engine/fx_service.py (see either file's PATCH note for the full
    explanation). Now mtime-based via app.common.json_cache -- for
    WHICHEVER path is actually loaded, so both the API and worker
    processes converge on a freshly-written generated file automatically,
    same as before.
    """
    from ..db.settings import get_settings

    settings = get_settings()
    configured = (getattr(settings, "RECEIPT_METHOD_MAP_OUTPUT_PATH", "") or "").strip()

    if configured:
        generated_path = Path(configured).expanduser()
        try:
            return load_json_cached(generated_path)
        except FileNotFoundError:
            logger.warning(
                "[receipt_method_resolver] No generated receipt method map at configured "
                "RECEIPT_METHOD_MAP_OUTPUT_PATH=%s (receipt_methods watcher/puller hasn't produced "
                "one yet) -- falling back to the checked-in seed at %s. This seed can go stale -- "
                "treat this warning as a signal to check the watcher/puller pipeline, not routine.",
                generated_path, _SEED_PATH,
            )

    return load_json_cached(_SEED_PATH)


@dataclass
class ReceiptMethodResult:
    matched: bool
    receipt_method_name: Optional[str] = None
    receipt_class: Optional[str] = None
    ou_number: Optional[str] = None
    ambiguous: bool = False
    candidates: list[dict] = field(default_factory=list)


def resolve_receipt_method(
    account_number: str | None,
    ou_number: str | None = None,
) -> ReceiptMethodResult:
    """
    Look up the ReceiptMethod for a bank account.

    Priority:
      1. Narrow candidates to the row's OU number, if given and if any
         candidate actually matches it (handles the same account number
         being reused across multiple OUs in the source data).
      2. Among remaining candidates, prefer receipt classes in the order
         given by config `_default_receipt_class_priority` (Direct
         Deposit / Wire Transfer first -- see module docstring).
      3. If more than one candidate remains after that, or the account is
         listed in `_accounts_with_unresolved_ambiguity`, the result comes
         back with ambiguous=True so the caller can log/flag it -- the
         first candidate is still returned so posting isn't blocked, but
         this should be reviewed.

    Returns matched=False (no receipt_method_name) if the account isn't in
    the extract at all -- callers must NOT silently default to a guessed
    method name in that case.
    """
    if not account_number:
        return ReceiptMethodResult(matched=False)

    data = _load_receipt_method_map()
    accounts: dict = data.get("accounts", {})
    priority: list[str] = data.get("_default_receipt_class_priority", [])
    known_ambiguous: set = set(data.get("_accounts_with_unresolved_ambiguity", []))

    key = str(account_number).strip()
    candidates = accounts.get(key, [])
    if not candidates:
        return ReceiptMethodResult(matched=False)

    if ou_number:
        ou_key = str(ou_number).strip()
        ou_matches = [c for c in candidates if c.get("ou_number") == ou_key]
        if ou_matches:
            candidates = ou_matches

    def _priority_rank(c: dict) -> int:
        cls = c.get("receipt_class", "")
        try:
            return priority.index(cls)
        except ValueError:
            return len(priority)  # unknown classes sort last, not first

    ranked = sorted(candidates, key=_priority_rank)
    best = ranked[0]

    ambiguous = (key in known_ambiguous) or (len(ranked) > 1)

    return ReceiptMethodResult(
        matched=True,
        receipt_method_name=best.get("receipt_method_name"),
        receipt_class=best.get("receipt_class"),
        ou_number=best.get("ou_number"),
        ambiguous=ambiguous,
        candidates=ranked,
    )