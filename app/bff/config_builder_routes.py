"""
app.bff.config_builder_routes — /api/config/*  (Account-Based Ingestion)
========================================================================
Config Builder + account-config management for the account-based engine.

  GET    /builder/raw-preview/{filename}   — raw cell grid for the wizard
  POST   /builder/locate-account           — live-extract account(s) via a locator draft
  POST   /builder/test                     — test a *draft recipe* against a file
  POST   /builder/save                     — save a recipe (create account / add format recipe)
  GET    /builder/accounts                 — list accounts (Manage + Clone-from-existing)
  GET    /builder/account/{account_number} — full account entry (for cloning)
  DELETE /builder/{account_number}          — delete an account (all its recipes)
  DELETE /builder/{account_number}/{format} — delete a single format recipe
  POST   /test-existing                    — test a *saved* recipe against a file
  GET    /detect/{filename}                — re-detect + list matching accounts

Config store is JSON only: account_configs.json (+ account_ou_map.json).
Register under /api/config alongside config_routes.
"""
from __future__ import annotations

import os
import json
import dataclasses
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db.models import SourceFile, User
from ..deps import get_db
from ..auth import get_optional_current_user
from ..audit.service import log_activity
from ..storage.client import get_storage_client
from ..bank_statement.account_locator import extract_accounts, normalize_account, last4, match_key
from ..bank_statement.configs.account_loader import (
    load_account_configs, load_account_ou_map, reload_account_configs,
    active_recipe, format_summaries,
)

router = APIRouter()

_CFG_DIR = Path(__file__).parent.parent / "bank_statement" / "configs"
_ACCOUNT_CONFIGS_PATH = _CFG_DIR / "account_configs.json"
# BUGFIX: this used to point at account_ou_map.json — a legacy, full-account-
# keyed file that NOTHING in the active OU-resolution path reads.
# bank_statement/ou_resolver.py's resolve_ou() (the function every real
# detection actually calls) reads load_bank_ou_mapping() ->
# bank_ou_mapping.json, keyed by last-4, not account_ou_map.json. Every OU
# mapping saved via this wizard was silently going into a dead file — the
# save succeeded with no error, but the account's OU would never actually
# resolve during a real analysis run. See account_loader.py's own comment:
# "do not replace with account_ou_map.json ... which had wrong OU names."
_OU_MAP_PATH = _CFG_DIR / "bank_ou_mapping.json"
# Separate registry fx_service.py actually uses for functional currency +
# the Oracle "NAME(ou)" BusinessUnit display string — see
# rule_engine/fx_service.py's get_functional_currency()/get_ou_display_name().
# Onboarding an account for a genuinely NEW ou_number needs an entry here
# too, or FX Leg 2 resolution and the Oracle payload's BusinessUnit field
# both silently fail for that OU until someone edits this file by hand
# (its own comment says exactly that: "Add new OUs here when onboarded.").
_OU_FUNCTIONAL_CURRENCY_PATH = Path(__file__).parent.parent / "rule_engine" / "configs" / "ou_functional_currency.json"
_STATEMENT_BUCKET = "bank-statements"
_FMT_ALIASES = {"xlsm": "xlsx", "txt": "csv"}


# ── shared helpers ────────────────────────────────────────────────────────────

def _local_path(db: Session, filename: str) -> tuple[SourceFile, str]:
    record = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement", SourceFile.filename == filename
    ).first()
    if not record:
        raise HTTPException(404, f"File '{filename}' not found")
    storage = get_storage_client()
    return record, storage.local_path_for_read(_STATEMENT_BUCKET, record.storage_key)


def _local_path_by_key(db: Session, storage_key: str) -> tuple[SourceFile, str]:
    record = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement", SourceFile.storage_key == storage_key
    ).first()
    if not record:
        raise HTTPException(404, "File not found")
    storage = get_storage_client()
    return record, storage.local_path_for_read(_STATEMENT_BUCKET, record.storage_key)


def _file_format(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    return _FMT_ALIASES.get(ext, ext)


def _read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _test_recipe(local_path: str, recipe: dict, key: str, filename: str) -> dict:
    """Parse a file with a recipe and return a row-count + sample preview."""
    from ..bank_statement.detector import DetectionResult
    from ..bank_statement.parser import parse_credit_rows, ColumnValidationError

    detection = DetectionResult(success=True, config_key=key, config=recipe,
                                step_used=1, method_detail=f"Test '{key}'")
    try:
        rows = parse_credit_rows(local_path, detection, filename)

        def _ser(r):
            d = dataclasses.asdict(r)
            if d.get("statement_date") is not None:
                d["statement_date"] = str(d["statement_date"])
            return d

        return {"success": True, "row_count": len(rows), "rows": [_ser(r) for r in rows[:50]]}
    except (ColumnValidationError, Exception) as e:
        return {"success": False, "error": str(e), "row_count": 0, "rows": []}


# ── upload a file for the wizard (NO ingestion) ──────────────────────────────────

@router.post("/builder/upload")
async def builder_upload(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Store an uploaded report so the Config Builder wizard can preview / locate /
    test against it — WITHOUT running the ingestion pipeline.

    Config-building is by definition the "no config exists yet" case, so routing
    this through /api/run/upload (which detect_config()s and then defers
    ingest_statement) always fails detection ("Bank format not auto-detected"),
    marks the file ingest_status="error", and clutters the Home analysis list
    with a failed file. Here we only need the bytes on disk + a SourceFile row so
    raw-preview can resolve the file by name.

    - No ingestion is deferred.
    - No duplicate-hash block: re-uploading the same file (to add a NEW version
      of an existing config) is expected and must be allowed.
    - The row is created archived=True so it never shows in the Home statements
      list; the wizard reads it by (kind, filename) regardless of archived state.
    """
    filename = file.filename or "upload"
    data = await file.read()

    storage = get_storage_client()
    storage.save(_STATEMENT_BUCKET, filename, data)

    record = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement", SourceFile.filename == filename
    ).first()
    if record:
        # Reuse the existing row (e.g. adding a new version of the same file).
        record.storage_key = filename
    else:
        record = SourceFile(
            kind="bank_statement",
            filename=filename,
            storage_key=filename,
            ingest_status="config_only",   # never analysed; excluded from Home list
            archived=True,
        )
        db.add(record)
    db.commit()
    db.refresh(record)

    return {"filename": filename, "source_file_id": record.id}


# ── raw preview ─────────────────────────────────────────────────────────────────

@router.get("/builder/raw-preview/{filename}")
def builder_raw_preview(filename: str, db: Session = Depends(get_db)):
    """Return raw cell data for all sheets — used by the wizard preview/header/locate steps."""
    record, local_path = _local_path(db, filename)
    ext = os.path.splitext(filename)[1].lower().lstrip(".")

    MAX_ROWS, MAX_COLS = 40, 20
    sheets = []
    if ext in ("xlsx", "xlsm"):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(local_path, read_only=True, data_only=True)
            for sh in wb.sheetnames:
                ws = wb[sh]
                rows = [[str(v).strip() if v is not None else "" for v in row]
                        for row in ws.iter_rows(min_row=1, max_row=MAX_ROWS, max_col=MAX_COLS, values_only=True)]
                sheets.append({"name": sh, "rows": rows})
            wb.close()
        except Exception as e:
            raise HTTPException(500, f"Could not read xlsx: {e}")
    elif ext == "xls":
        try:
            import xlrd
            wb = xlrd.open_workbook(local_path)
            for sh in wb.sheet_names():
                ws = wb.sheet_by_name(sh)
                rows = []
                for r in range(min(MAX_ROWS, ws.nrows)):
                    row = [str(ws.cell_value(r, c)).strip() if ws.cell_value(r, c) not in (None, "") else ""
                           for c in range(min(MAX_COLS, ws.ncols))]
                    row += [""] * (MAX_COLS - len(row))
                    rows.append(row)
                sheets.append({"name": sh, "rows": rows})
        except Exception as e:
            raise HTTPException(500, f"Could not read xls: {e}")
    elif ext in ("csv", "txt"):
        try:
            import csv
            with open(local_path, encoding="utf-8", errors="replace") as f:
                rows = [[str(v).strip() for v in row[:MAX_COLS]]
                        for i, row in enumerate(csv.reader(f)) if i < MAX_ROWS]
            sheets.append({"name": "Sheet1", "rows": rows})
        except Exception as e:
            raise HTTPException(500, f"Could not read csv: {e}")
    else:
        raise HTTPException(400, f"Unsupported extension: .{ext}")

    return {"filename": filename, "storage_key": record.storage_key, "extension": ext, "sheets": sheets}


# ── locate account (live preview for the wizard's account step) ──────────────────

class LocateAccountRequest(BaseModel):
    storage_key: str
    locator: dict
    source: dict | None = None


@router.post("/builder/locate-account")
def builder_locate_account(body: LocateAccountRequest, db: Session = Depends(get_db)):
    """Run a locator draft against the file and return the account(s) it finds,
    flagging any that are already registered."""
    _record, local_path = _local_path_by_key(db, body.storage_key)
    found = sorted(extract_accounts(local_path, body.locator, body.source or {}))

    configs = load_account_configs()
    existing = {}
    for acct in found:
        mk = match_key(acct)
        for k, entry in configs.items():
            if match_key(entry.get("account_number", k)) == mk:
                existing[acct] = sorted((entry.get("recipes") or {}).keys())
    return {
        "accounts": found,
        "count": len(found),
        "last4s": sorted({last4(a) for a in found}),
        "existing": existing,   # {account: [formats already configured]}
    }


# ── test a draft recipe ──────────────────────────────────────────────────────────

class BuilderTestRequest(BaseModel):
    storage_key: str
    config_draft: dict          # the draft recipe (source/fields/credit_rule/account_locator/…)


@router.post("/builder/test")
def builder_test(body: BuilderTestRequest, db: Session = Depends(get_db)):
    """Test a draft recipe against the uploaded file. Returns up to 50 normalized rows."""
    _record, local_path = _local_path_by_key(db, body.storage_key)
    return _test_recipe(local_path, body.config_draft, body.config_draft.get("key", "_DRAFT_"), _record.filename)


# ── save a recipe (create account or add a format recipe under an existing one) ──

class SaveRecipeRequest(BaseModel):
    account_number: str
    display_name: str
    format: str                       # xlsx | xls | csv | pdf
    recipe: dict                      # account_locator + source + fields + credit_rule + …
    bank: str | None = None
    currency: str | None = None
    ou_number: str | None = None
    business_unit: str | None = None
    # Ledger/functional currency for this OU — required for Oracle FX Leg 2
    # resolution (rule_engine/fx_service.py's get_functional_currency()).
    # Only used if ou_number is genuinely new (see builder_save); falls
    # back to `currency` (the bank account's own statement currency) if
    # left blank, matching the same best-effort default
    # ingestion/ingest_service.py's auto-provisioning already uses — but
    # exposed here so a human can set it correctly at onboarding time
    # instead of relying on a guess that may not match the OU's real ledger.
    functional_currency: str | None = None
    storage_key: str | None = None    # (unused for keying; kept for symmetry)
    # Best-effort author of this version, read from the login_user_email_stub
    # cookie by the wizard (this module's axios has no dev-user interceptor).
    # Displayed as "added by" in the read-only version list; omitted if unknown.
    created_by: str | None = None
    source_filename: str | None = None  # statement file the config was built from (for the audit log)


@router.post("/builder/save")
def builder_save(body: SaveRecipeRequest, db: Session = Depends(get_db),
                 user: User | None = Depends(get_optional_current_user)):
    """Save/attach a recipe. If the account exists, the recipe is added (or replaced)
    under recipes[format]; otherwise a new account entry is created. Validates the
    whole registry after writing and rolls back on failure."""
    acct = str(body.account_number).strip()
    if not acct:
        raise HTTPException(400, "Account number is required")
    if not body.display_name.strip():
        raise HTTPException(400, "Display name is required")
    fmt = _FMT_ALIASES.get(body.format.lower(), body.format.lower())
    if fmt not in ("xlsx", "xls", "csv", "pdf"):
        raise HTTPException(400, f"Unsupported format '{body.format}'")
    if not body.recipe.get("account_locator"):
        raise HTTPException(400, "Recipe must include an account_locator")

    raw = _read_raw(_ACCOUNT_CONFIGS_PATH)
    before = json.dumps(raw)

    created_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    created_by = (body.created_by or "").strip() or None

    def _version_obj(version: int) -> dict:
        obj = {"version": version, "created_at": created_at, "recipe": body.recipe}
        if created_by:
            obj["created_by"] = created_by
        return obj

    entry = raw.get(acct) if not acct.startswith("_") else None
    created = False          # brand-new account
    format_created = False   # first recipe for this format on an existing account
    if isinstance(entry, dict):
        recipes = entry.setdefault("recipes", {})
        existing = recipes.get(fmt)
        if isinstance(existing, list) and existing:
            # Append a new version — never replace. version = max existing + 1.
            next_version = max((v.get("version", 0) for v in existing), default=0) + 1
            existing.append(_version_obj(next_version))
        else:
            # Format absent (or malformed) → start its version list at 1.
            format_created = True
            recipes[fmt] = [_version_obj(1)]
            next_version = 1
        entry["display_name"] = body.display_name
        entry["account_last4"] = last4(acct)
        if body.bank:     entry["bank"] = body.bank
        if body.currency: entry["currency"] = body.currency
        entry["account_number"] = acct
    else:
        created = True
        format_created = True
        next_version = 1
        raw[acct] = {
            "account_number": acct,
            "account_last4":  last4(acct),
            "bank":           body.bank or "",
            "currency":       body.currency or "",
            "display_name":   body.display_name,
            "recipes":        {fmt: [_version_obj(1)]},
        }

    # write + validate, roll back on failure
    with open(_ACCOUNT_CONFIGS_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    try:
        reload_account_configs()
    except Exception as e:
        with open(_ACCOUNT_CONFIGS_PATH, "w", encoding="utf-8") as f:
            f.write(before)
        reload_account_configs()
        raise HTTPException(400, f"Config invalid, not saved: {e}")

    # Audit. A new account or a brand-new format recipe is a "create"; adding a
    # further version to an existing format is an update. (The old `overwritten`
    # variable no longer exists in the versioned save — appends never replace.)
    if user is not None:
        action = "config.create" if (created or format_created) else "config.version_added"
        log_activity(db, user, action=action, entity_type="AccountConfig",
                     entity_id=acct,
                     metadata={"display_name": body.display_name, "format": fmt, "version": next_version,
                               "source_filename": body.source_filename})
        db.commit()  # log_activity rides caller txn; builder_save has no other DB commit

    # ── OU mapping — bank_ou_mapping.json (last-4 keyed, the file
    #    bank_statement/ou_resolver.py's resolve_ou() actually reads) ────────
    if body.ou_number:
        ou_number = body.ou_number.strip()
        business_unit = (body.business_unit or "").strip()
        ou_display = f"{business_unit}({ou_number})" if business_unit else ou_number

        ou_map = _read_raw(_OU_MAP_PATH)
        ou_map[last4(acct)] = {
            "ou": ou_display,
            "ou_number": ou_number,
            "bank": body.bank or "",
            "bank_config": acct,
        }
        with open(_OU_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(ou_map, f, indent=2)

        # ── ou_functional_currency.json — the SEPARATE, ou_number-keyed
        #    registry rule_engine/fx_service.py actually reads for FX Leg 2
        #    resolution and the Oracle payload's BusinessUnit field. Only
        #    write here if this OU is genuinely new — never silently
        #    overwrite an existing, presumably-correct entry with a guess.
        func_ccy_map = _read_raw(_OU_FUNCTIONAL_CURRENCY_PATH)
        if ou_number not in func_ccy_map:
            func_ccy_map[ou_number] = {
                "ou": ou_display,
                "country": "",
                "functional_currency": (body.functional_currency or body.currency or "USD").upper(),
            }
            with open(_OU_FUNCTIONAL_CURRENCY_PATH, "w", encoding="utf-8") as f:
                json.dump(func_ccy_map, f, indent=2)

        reload_account_configs()

    if created:
        message = f"Created account {acct} with {fmt} recipe (v1)."
    elif format_created:
        message = f"Added {fmt} recipe to account {acct} (v1)."
    else:
        message = f"Added version {next_version} to {fmt} recipe for account {acct}."

    return {
        "success": True,
        "account_number": acct,
        "format": fmt,
        "created": created,
        "format_created": format_created,
        "appended": not created and not format_created,
        "version": next_version,
        "message": message,
    }


# ── list / fetch accounts (Manage + Clone-from-existing) ─────────────────────────

@router.get("/builder/accounts")
def list_accounts():
    """List all account configs (light) for the manage dialog and clone picker."""
    out = []
    for acct, entry in load_account_configs().items():
        out.append({
            "account_number": entry.get("account_number", acct),
            "account_last4":  entry.get("account_last4"),
            "display_name":   entry.get("display_name", acct),
            "bank":           entry.get("bank"),
            "currency":       entry.get("currency"),
            # Per-format version metadata (newest first, active flagged) for the
            # read-only version list on the Config tab. Recipe bodies are omitted
            # here — display is metadata-only.
            "formats":        format_summaries(entry),
        })
    return {"accounts": out}


@router.get("/builder/account/{account_number}")
def get_account(account_number: str):
    """Full account entry (incl. recipes) — used to clone an existing config."""
    entry = load_account_configs().get(account_number)
    if not entry:
        raise HTTPException(404, f"Account '{account_number}' not found")
    return entry


# ── test an existing saved recipe ────────────────────────────────────────────────

class TestExistingRequest(BaseModel):
    filename: str
    account_number: str
    format: str | None = None


@router.post("/test-existing")
def test_existing(body: TestExistingRequest, db: Session = Depends(get_db)):
    """Run a saved account recipe against the uploaded file (preview + row count)."""
    entry = load_account_configs().get(body.account_number)
    if not entry:
        raise HTTPException(404, f"Account '{body.account_number}' not found")
    record, local_path = _local_path(db, body.filename)
    fmt = body.format or _file_format(body.filename)
    recipe = active_recipe(entry, fmt)   # test always runs the active (latest) version
    if not recipe:
        raise HTTPException(404, f"Account '{body.account_number}' has no '{fmt}' recipe")
    return _test_recipe(local_path, recipe, body.account_number, record.filename)


# ── delete account / recipe ──────────────────────────────────────────────────────

@router.delete("/builder/{account_number}")
def delete_account(account_number: str):
    """Delete an entire account config (all its recipes) + its OU mapping."""
    raw = _read_raw(_ACCOUNT_CONFIGS_PATH)
    if account_number not in raw or account_number.startswith("_"):
        raise HTTPException(404, f"Account '{account_number}' not found")
    del raw[account_number]
    with open(_ACCOUNT_CONFIGS_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)

    ou = _read_raw(_OU_MAP_PATH)
    if ou.pop(account_number, None) is not None:
        with open(_OU_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(ou, f, indent=2)

    reload_account_configs()
    return {"success": True, "deleted": account_number}


@router.delete("/builder/{account_number}/{fmt}")
def delete_recipe(account_number: str, fmt: str):
    """Delete a single format recipe. If it was the last recipe, the account is removed."""
    fmt = _FMT_ALIASES.get(fmt.lower(), fmt.lower())
    raw = _read_raw(_ACCOUNT_CONFIGS_PATH)
    entry = raw.get(account_number)
    if not isinstance(entry, dict) or fmt not in (entry.get("recipes") or {}):
        raise HTTPException(404, f"Account '{account_number}' has no '{fmt}' recipe")
    del entry["recipes"][fmt]
    account_removed = False
    if not entry["recipes"]:
        del raw[account_number]
        account_removed = True
    with open(_ACCOUNT_CONFIGS_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    reload_account_configs()
    return {"success": True, "account_number": account_number, "format": fmt,
            "account_removed": account_removed}


# ── detect / candidates ──────────────────────────────────────────────────────────

@router.get("/detect/{filename}")
def detect_for_file(filename: str, db: Session = Depends(get_db)):
    """Re-run account-based detection and list matching accounts (picker/reconfigure)."""
    record, local_path = _local_path(db, filename)
    from ..bank_statement.detector import detect_config, list_matching_configs
    det = detect_config(local_path)
    return {
        "filename": filename,
        "success": det.success,
        "config_key": det.config_key,           # matched account number
        "account_number": det.account_number,
        "reason": det.reason,
        "method_detail": det.method_detail,
        "ambiguous": det.reason == "AMBIGUOUS",
        "candidates": list_matching_configs(local_path),
    }