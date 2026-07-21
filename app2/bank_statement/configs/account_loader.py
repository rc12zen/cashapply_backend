"""
app.bank_statement.configs.account_loader  (DB-BACKED -- replaces JSON)
========================================================================
Loads the account-based config registry from the database instead of
account_configs.json / bank_ou_mapping.json / account_ou_map.json.

WHY THIS CHANGED
-----------------
Config used to live in three separate JSON files that could (and did)
drift out of sync with each other. OU and Business Unit are now a real FK
relationship (BankAccount.ou_id -> OrganizationUnit), and recipes are
versioned rows in AccountConfigRecipe -- see app/db/models.py. This module
is now the ONLY place that reads that data; every other module (detector,
parser, ou_resolver, bff routes) still calls the same functions below, so
this was a swap of *implementation*, not of *interface* -- callers did not
need to change.

Public API (unchanged shapes, so callers are unaffected):
  load_account_configs()  -> {account_number: entry}   (entry shaped like the
                              old JSON: display_name, account_last4, bank,
                              currency, recipes:{fmt:[versions...]})
  load_bank_ou_mapping()  -> {last4: {ou, ou_number, bank, bank_config}}
  load_account_ou_map     -> alias, kept for any old imports
  last4_index()           -> {last4: [account_number, ...]}
  ou_index()              -> dict copy of load_bank_ou_mapping()
  active_version() / active_recipe() / list_versions() / format_summaries()
                          -> unchanged, operate on the entry shape above
  reload_account_configs()-> summary dict (no-op cache-buster; DB reads are
                              always live, there is no mtime cache anymore)

No JSON files are read here anymore. account_config_schema.json is no
longer used -- integrity (recipe must be valid JSON, format must be one of
xlsx/xls/csv/pdf) is now guaranteed by the DB schema + the save endpoint.
"""
from __future__ import annotations

from ...db.session import session_scope
from ...db.models import BankAccount, OrganizationUnit, AccountConfigRecipe


# -- entry-shape builders (DB rows -> the same dict shape callers expect) ----

def _entry_from_bank_account(acct: BankAccount) -> dict:
    recipes: dict[str, list[dict]] = {}
    for r in acct.recipes:
        recipes.setdefault(r.format, []).append({
            "version":    r.version,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "created_by": r.created_by,
            "recipe":     r.recipe,
        })
    return {
        "account_number": acct.account_number,
        "account_last4":  acct.account_last4,
        "display_name":   acct.display_name or acct.account_number,
        "bank":            acct.bank_name,
        "currency":        acct.currency,
        "ou_number":       acct.organization_unit.ou_number if acct.organization_unit else None,
        "business_unit":   acct.organization_unit.ou_name if acct.organization_unit else None,
        "recipes":         recipes,
    }


def load_account_configs() -> dict:
    """Load every onboarded account, keyed by account_number. DB is always
    live -- no cache, no staleness to reason about across processes."""
    with session_scope() as db:
        accounts = db.query(BankAccount).filter(BankAccount.active.is_(True)).all()
        return {a.account_number: _entry_from_bank_account(a) for a in accounts}


def load_bank_ou_mapping() -> dict:
    """
    {last4: {ou, ou_number, bank, bank_config}} -- shaped exactly like the old
    bank_ou_mapping.json so bank_statement/ou_resolver.py's resolve_ou()
    needs no changes. Built from BankAccount JOIN OrganizationUnit.
    """
    with session_scope() as db:
        accounts = (
            db.query(BankAccount)
            .filter(BankAccount.active.is_(True), BankAccount.account_last4.isnot(None))
            .all()
        )
        out: dict[str, dict] = {}
        for a in accounts:
            ou = a.organization_unit
            if ou is None:
                continue
            ou_display = f"{ou.ou_name}({ou.ou_number})" if ou.ou_name else ou.ou_number
            out[a.account_last4] = {
                "ou":           ou_display,
                "ou_number":    ou.ou_number,
                "bank":         a.bank_name or "",
                "bank_config":  a.account_number,
            }
        return out


# Keep load_account_ou_map as an alias so any code that imports it still works.
load_account_ou_map = load_bank_ou_mapping


def ou_index() -> dict:
    """{last4_suffix: entry} -- same as before, now DB-backed under the hood."""
    return dict(load_bank_ou_mapping())


def last4_index() -> dict:
    """{last4: [account_number, ...]} built from the registry for fast matching."""
    index: dict[str, list[str]] = {}
    for acct, entry in load_account_configs().items():
        last4 = entry.get("account_last4")
        if last4:
            index.setdefault(last4, []).append(acct)
    return index


def _version_list(entry: dict, fmt: str) -> list:
    versions = (entry.get("recipes") or {}).get(fmt)
    return versions if isinstance(versions, list) else []


def active_version(entry: dict, fmt: str) -> dict | None:
    versions = _version_list(entry, fmt)
    if not versions:
        return None
    return max(versions, key=lambda v: v.get("version", 0))


def active_recipe(entry: dict, fmt: str) -> dict | None:
    av = active_version(entry, fmt)
    return av.get("recipe") if av else None


def list_versions(entry: dict, fmt: str) -> list:
    return sorted(_version_list(entry, fmt), key=lambda v: v.get("version", 0), reverse=True)


def format_summaries(entry: dict) -> list:
    summaries = []
    for fmt in sorted((entry.get("recipes") or {}).keys()):
        versions = list_versions(entry, fmt)
        if not versions:
            continue
        summaries.append({
            "format":         fmt,
            "active_version": versions[0].get("version", 0),
            "versions": [
                {
                    "version":    v.get("version"),
                    "created_at": v.get("created_at"),
                    "created_by": v.get("created_by"),
                }
                for v in versions
            ],
        })
    return summaries


def reload_account_configs() -> dict:
    """Kept as a named entrypoint since bff/config_builder_routes.py calls it
    after every save -- with no cache left to bust, this just reports a fresh
    count for the /reload response."""
    configs = load_account_configs()
    ou_map = load_bank_ou_mapping()
    return {
        "reloaded": True,
        "accounts_loaded": len(configs),
        "ou_mappings_loaded": len(ou_map),
        "last4_buckets": len(last4_index()),
    }
