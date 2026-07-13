"""
app.bank_statement.configs.account_loader
==========================================
Loads and validates the account-based config registry (JSON, no DB).

Files (this directory)
  account_configs.json  — full account number -> { display_name, recipes{fmt:…} }
  account_ou_map.json   — full account number -> { ou_number, business_unit }
  account_config_schema.json — JSON Schema for a single account entry

Provides:
  load_account_configs()  — validated dict {account_number: entry}
  load_account_ou_map()   — dict {account_number: {ou_number, business_unit}}
  last4_index()           — {last4: [account_number, …]} for fast matching
  reload_account_configs()— clear caches + rebuild (hot reload after save/delete)

Keys beginning with "_" (e.g. "_comment") are ignored everywhere.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).parent
_CONFIGS_PATH = _HERE / "account_configs.json"
_OU_MAP_PATH  = _HERE / "bank_ou_mapping.json"   # last-4-keyed, authoritative Zensar OU data
_SCHEMA_PATH  = _HERE / "account_config_schema.json"


class AccountConfigValidationError(Exception):
    pass


def _strip_private(raw: dict) -> dict:
    """Drop keys that start with '_' (comments / metadata)."""
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


def _validate(entries: dict) -> None:
    """Validate every account entry against account_config_schema.json.
    Silently skips if jsonschema isn't installed (dev convenience)."""
    try:
        import jsonschema
    except ImportError:
        return

    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    errors: list[str] = []
    for acct, entry in entries.items():
        try:
            jsonschema.validate(entry, schema)
        except jsonschema.ValidationError as e:
            path = " → ".join(str(p) for p in e.path) if e.path else "(root)"
            errors.append(f"account '{acct}': {e.message} at {path}")
        # cross-field: the stored last4 must match the account number's tail
        norm = _normalize_account(str(entry.get("account_number", "")))
        expected = norm[-4:] if norm else ""
        if expected and entry.get("account_last4") and entry["account_last4"] != expected:
            errors.append(
                f"account '{acct}': account_last4 '{entry['account_last4']}' "
                f"does not match account_number tail '{expected}'"
            )

    if errors:
        raise AccountConfigValidationError(
            f"{len(errors)} account config(s) failed validation:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )


def _normalize_account(value: str) -> str:
    """Uppercase, keep A-Z0-9 (drop spaces/dashes/dots), drop trailing '.0'.
    Mirrors the normalization used by the account locator (Phase 2)."""
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return "".join(ch for ch in s.upper() if ch.isalnum())


@lru_cache(maxsize=1)
def load_account_configs() -> dict:
    """Load + validate account_configs.json. Returns {account_number: entry}."""
    with open(_CONFIGS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    entries = _strip_private(raw)
    _validate(entries)
    return entries


@lru_cache(maxsize=1)
def load_bank_ou_mapping() -> dict:
    """
    Load bank_ou_mapping.json — keyed by last-4 account suffix.
    Schema per entry: {ou, ou_number, bank, bank_config}
    where `ou` is the Zensar OU display name (e.g. "PUNE(111)").
    This is the authoritative Zensar OU reference; do not replace with
    account_ou_map.json (full-account keyed) which had wrong OU names.
    """
    if not _OU_MAP_PATH.exists():
        return {}
    with open(_OU_MAP_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return _strip_private(raw)


# Keep load_account_ou_map as an alias so any code that imports it still works.
load_account_ou_map = load_bank_ou_mapping


@lru_cache(maxsize=1)
def ou_index() -> dict:
    """
    Build {last4_suffix: entry} from bank_ou_mapping.json for O(1) rowwise
    OU lookup.  The map is already keyed by last-4 so this is a direct pass-
    through with leading-zero preservation (keys kept as-is, no normalization).
    """
    return dict(load_bank_ou_mapping())


@lru_cache(maxsize=1)
def last4_index() -> dict:
    """Build {last4: [account_number, …]} from the registry for fast matching.
    Multiple accounts can share a last4 — that's the collision case resolved by
    the full account number at detection time."""
    index: dict[str, list[str]] = {}
    for acct, entry in load_account_configs().items():
        last4 = entry.get("account_last4") or _normalize_account(acct)[-4:]
        if last4:
            index.setdefault(last4, []).append(acct)
    return index


def reload_account_configs() -> dict:
    """Clear caches and reload. Returns a summary for a /reload response."""
    load_account_configs.cache_clear()
    load_bank_ou_mapping.cache_clear()
    last4_index.cache_clear()
    ou_index.cache_clear()

    configs = load_account_configs()
    ou_map = load_bank_ou_mapping()
    return {
        "reloaded": True,
        "accounts_loaded": len(configs),
        "ou_mappings_loaded": len(ou_map),
        "last4_buckets": len(last4_index()),
    }