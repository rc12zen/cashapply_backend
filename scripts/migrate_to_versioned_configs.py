"""
scripts/migrate_to_versioned_configs.py
========================================
One-time, idempotent migration that converts account_configs.json from the
old single-recipe-per-format shape to the versioned shape.

  OLD:  recipes[fmt] = { …recipe… }                    (a single dict)
  NEW:  recipes[fmt] = [ { version: 1, created_at: <migration UTC ISO-Z>,
                           created_by: "migration", recipe: { …recipe… } } ]

Every existing recipe becomes version 1. Re-running is safe: any recipes[fmt]
that is already a list is left untouched.

Usage:
    python -m scripts.migrate_to_versioned_configs

A backup of the original file is written to account_configs.json.bak before
anything is changed.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

_CFG_PATH = (
    Path(__file__).parent.parent
    / "app" / "bank_statement" / "configs" / "account_configs.json"
)
_BAK_PATH = _CFG_PATH.with_suffix(".json.bak")


def migrate() -> dict:
    with open(_CFG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    created_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    accounts_scanned = 0
    formats_wrapped = 0
    formats_skipped = 0

    for acct, entry in raw.items():
        if str(acct).startswith("_") or not isinstance(entry, dict):
            continue
        accounts_scanned += 1
        recipes = entry.get("recipes")
        if not isinstance(recipes, dict):
            continue
        for fmt, value in list(recipes.items()):
            if isinstance(value, list):
                formats_skipped += 1          # already versioned
                continue
            recipes[fmt] = [{
                "version": 1,
                "created_at": created_at,
                "created_by": "migration",
                "recipe": value,
            }]
            formats_wrapped += 1

    if formats_wrapped:
        # Back up the pristine original only when we actually change something.
        with open(_CFG_PATH, "r", encoding="utf-8") as src:
            original = src.read()
        with open(_BAK_PATH, "w", encoding="utf-8") as bak:
            bak.write(original)
        with open(_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)

    return {
        "accounts_scanned": accounts_scanned,
        "formats_wrapped": formats_wrapped,
        "formats_skipped": formats_skipped,
        "backup_written": bool(formats_wrapped),
    }


if __name__ == "__main__":
    report = migrate()
    print("migrate_to_versioned_configs:")
    print(f"  accounts scanned : {report['accounts_scanned']}")
    print(f"  formats wrapped  : {report['formats_wrapped']}")
    print(f"  formats skipped  : {report['formats_skipped']} (already versioned)")
    if report["backup_written"]:
        print(f"  backup written   : {_BAK_PATH.name}")
    else:
        print("  no changes — nothing to migrate (idempotent no-op)")
