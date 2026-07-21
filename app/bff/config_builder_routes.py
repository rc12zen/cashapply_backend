"""
app.bff.config_builder_routes — /api/config/*  (Account-Based Ingestion)
========================================================================
Config Builder + account-config management for the account-based engine.

  GET    /builder/raw-preview/{filename}   — raw cell grid for the wizard
  POST   /builder/locate-account           — live-extract account(s) via a locator draft
  POST   /builder/test                     — test a *draft recipe* against a file
  GET    /builder/available-ous            — OU/BU picklist for the wizard's OU step
  POST   /builder/save                     — save a recipe (create account / add format recipe)
  GET    /builder/accounts                 — list accounts (Manage + Clone-from-existing)
  GET    /builder/account/{account_number} — full account entry (for cloning)
  DELETE /builder/{account_number}          — delete an account (all its recipes)
  DELETE /builder/{account_number}/{format} — delete a single format recipe
  POST   /test-existing                    — test a *saved* recipe against a file
  GET    /detect/{filename}                — re-detect + list matching accounts

DB-BACKED (was JSON only). account_configs.json, bank_ou_mapping.json,
account_ou_map.json and ou_functional_currency.json are no longer read or
written anywhere in this module. OU + Business Unit are captured here at
save time as a real relationship — BankAccount.ou_id -> OrganizationUnit —
via _get_or_create_organization_unit()/_get_or_create_bank_account() below,
and recipes are versioned rows in AccountConfigRecipe. This is now the
single place account configs are created; everything downstream (detector,
parser, ou_resolver, fx_service) reads the same DB rows through
bank_statement/configs/account_loader.py.
Register under /api/config alongside config_routes.
"""
from __future__ import annotations

import os
import logging
import dataclasses
import datetime as dt

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db.models import SourceFile, User, BankAccount, OrganizationUnit, AccountConfigRecipe
from ..deps import get_db
from ..auth import require_permission
from ..audit.service import log_activity
from ..storage.client import get_storage_client
from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..bank_statement.account_locator import extract_accounts, normalize_account, last4, match_key
from ..bank_statement.configs.account_loader import (
    load_account_configs, load_account_ou_map, reload_account_configs,
    active_recipe, format_summaries,
)
from ..aging import aging_store

router = APIRouter()
logger = logging.getLogger("cashapply.config_builder")

_STATEMENT_BUCKET = "bank-statements"
_FMT_ALIASES = {"xlsm": "xlsx", "txt": "csv"}


# ── shared helpers ────────────────────────────────────────────────────────────

def _local_path(db: Session, filename: str) -> tuple[SourceFile, str]:
    record = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement", SourceFile.filename == filename
    ).first()
    if not record:
        raise AppError(ErrorCode.STATEMENT_NOT_FOUND, detail=f"file '{filename}'")
    storage = get_storage_client()
    return record, storage.local_path_for_read(_STATEMENT_BUCKET, record.storage_key)


def _local_path_by_key(db: Session, storage_key: str) -> tuple[SourceFile, str]:
    record = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement", SourceFile.storage_key == storage_key
    ).first()
    if not record:
        raise AppError(ErrorCode.STATEMENT_NOT_FOUND)
    storage = get_storage_client()
    return record, storage.local_path_for_read(_STATEMENT_BUCKET, record.storage_key)


def _file_format(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    return _FMT_ALIASES.get(ext, ext)


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
async def builder_upload(file: UploadFile = File(...), db: Session = Depends(get_db),
                          user: User = Depends(require_permission("config:manage"))):
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

def _trim_trailing_empty_cols(rows: list[list[str]]) -> list[list[str]]:
    """Normalize every row to equal width, then drop only the TRAILING columns
    that are empty in every previewed row. This is what makes the wizard show
    exactly the columns that carry data — a column is kept if it has a value in
    the header OR any data row, so nothing with data is missed, while the run of
    blank padding columns after the last real one is removed. Interior empty
    columns are preserved (index position matters for column mapping)."""
    if not rows:
        return rows
    width = max((len(r) for r in rows), default=0)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    last_with_data = -1
    for c in range(width):
        if any((norm[r][c] or "").strip() for r in range(len(norm))):
            last_with_data = c
    keep = last_with_data + 1
    return [r[:keep] for r in norm]


@router.get("/builder/raw-preview/{filename}")
def builder_raw_preview(filename: str, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("config:manage"))):
    """Return raw cell data for all sheets — used by the wizard preview/header/locate steps."""
    record, local_path = _local_path(db, filename)
    ext = os.path.splitext(filename)[1].lower().lstrip(".")

    # MAX_COLS is a safety CEILING against pathologically wide files, not the
    # display width — actual columns returned are trimmed to the last one that
    # holds data (see _trim_trailing_empty_cols), so a normal statement shows
    # all its real columns (e.g. 27) without hitting the ceiling.
    MAX_ROWS, MAX_COLS = 40, 50
    sheets = []
    if ext in ("xlsx", "xlsm"):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(local_path, read_only=True, data_only=True)
            for sh in wb.sheetnames:
                ws = wb[sh]
                rows = [[str(v).strip() if v is not None else "" for v in row]
                        for row in ws.iter_rows(min_row=1, max_row=MAX_ROWS, max_col=MAX_COLS, values_only=True)]
                sheets.append({"name": sh, "rows": _trim_trailing_empty_cols(rows)})
            wb.close()
        except Exception as e:
            logger.exception("raw-preview: failed to read xlsx %s", filename)
            raise AppError(ErrorCode.CONFIG_FILE_UNREADABLE,
                            detail=f"'{filename}' doesn't look like a valid Excel (.xlsx) file ({e.__class__.__name__})")
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
                sheets.append({"name": sh, "rows": _trim_trailing_empty_cols(rows)})
        except Exception as e:
            logger.exception("raw-preview: failed to read xls %s", filename)
            raise AppError(ErrorCode.CONFIG_FILE_UNREADABLE,
                            detail=f"'{filename}' doesn't look like a valid legacy Excel (.xls) file ({e.__class__.__name__})")
    elif ext in ("csv", "txt"):
        try:
            import csv
            with open(local_path, encoding="utf-8", errors="replace") as f:
                rows = [[str(v).strip() for v in row[:MAX_COLS]]
                        for i, row in enumerate(csv.reader(f)) if i < MAX_ROWS]
            sheets.append({"name": "Sheet1", "rows": _trim_trailing_empty_cols(rows)})
        except Exception as e:
            logger.exception("raw-preview: failed to read csv %s", filename)
            raise AppError(ErrorCode.CONFIG_FILE_UNREADABLE,
                            detail=f"'{filename}' could not be parsed as CSV ({e.__class__.__name__})")
    else:
        raise AppError(ErrorCode.CONFIG_FILE_TYPE_UNSUPPORTED, detail=f"'.{ext}' extension")

    return {"filename": filename, "storage_key": record.storage_key, "extension": ext, "sheets": sheets}


# ── locate account (live preview for the wizard's account step) ──────────────────

class LocateAccountRequest(BaseModel):
    storage_key: str
    locator: dict
    source: dict | None = None


@router.post("/builder/locate-account")
def builder_locate_account(body: LocateAccountRequest, db: Session = Depends(get_db),
                           user: User = Depends(require_permission("config:manage"))):
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
def builder_test(body: BuilderTestRequest, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("config:manage"))):
    """Test a draft recipe against the uploaded file. Returns up to 50 normalized rows."""
    _record, local_path = _local_path_by_key(db, body.storage_key)
    return _test_recipe(local_path, body.config_draft, body.config_draft.get("key", "_DRAFT_"), _record.filename)


# ── OU/BU picklist for the wizard's OU step ────────────────────────────────────

@router.get("/builder/available-ous")
def available_ous(db: Session = Depends(get_db),
                  user: User = Depends(require_permission("run:view"))):
    """
    OUs the wizard's OU/Business Unit step can offer, so onboarding an
    account picks from a real, known OU instead of free-typing one that
    might not exist. Two sources, merged:
      1. OUs already onboarded (OrganizationUnit table) — these have a
         known business_unit name + functional_currency already.
      2. OU numbers seen in the currently loaded aging report (the
         authoritative Oracle feed — aging_store's AgingMap.ou_numbers())
         that AREN'T onboarded yet. These show up with business_unit=None
         so the wizard prompts for the name once, on first use.
    """
    known: dict[str, dict] = {}
    for ou in db.query(OrganizationUnit).filter(OrganizationUnit.active.is_(True)).all():
        known[ou.ou_number] = {
            "ou_number": ou.ou_number,
            "business_unit": ou.ou_name,
            "functional_currency": ou.functional_currency,
            "known": True,
        }

    aging_map = aging_store.get_aging_map()
    if aging_map is not None:
        for ou_number in aging_map.ou_numbers:
            if ou_number and ou_number not in known:
                known[ou_number] = {
                    "ou_number": ou_number,
                    "business_unit": None,
                    "functional_currency": None,
                    "known": False,
                }

    return {"ous": sorted(known.values(), key=lambda o: o["ou_number"])}


# ── save a recipe (create account or add a format recipe under an existing one) ──

class SaveRecipeRequest(BaseModel):
    account_number: str
    display_name: str
    format: str                       # xlsx | xls | csv | pdf
    recipe: dict                      # account_locator + source + fields + credit_rule + …
    bank: str | None = None
    currency: str | None = None
    # OU + Business Unit are now REQUIRED — every account config must be
    # linked to a real OrganizationUnit, never saved "OU unknown". This is
    # what makes OU/BU a relationship (BankAccount.ou_id -> OrganizationUnit)
    # instead of an optional free-text afterthought.
    ou_number: str
    business_unit: str
    # Ledger/functional currency for this OU — required for Oracle FX Leg 2
    # resolution (rule_engine/fx_service.py's get_functional_currency()).
    # Only used if ou_number is genuinely new (see builder_save); falls
    # back to `currency` (the bank account's own statement currency) if
    # left blank.
    functional_currency: str | None = None
    storage_key: str | None = None    # (unused for keying; kept for symmetry)
    # Best-effort author of this version, read from the login_user_email_stub
    # cookie by the wizard (this module's axios has no dev-user interceptor).
    # Displayed as "added by" in the read-only version list; omitted if unknown.
    created_by: str | None = None


def _get_or_create_organization_unit(db: Session, ou_number: str, business_unit: str,
                                       functional_currency: str | None) -> OrganizationUnit:
    ou_number = ou_number.strip()
    business_unit = business_unit.strip()
    ou = db.query(OrganizationUnit).filter(OrganizationUnit.ou_number == ou_number).first()
    if ou is None:
        ou = OrganizationUnit(
            ou_number=ou_number,
            ou_name=business_unit,
            functional_currency=(functional_currency or "USD").upper(),
        )
        db.add(ou)
        db.flush()
    elif business_unit and ou.ou_name != business_unit:
        # Onboarding this account corrected/updated the BU name for an
        # already-known OU — keep it in sync rather than silently ignoring
        # what the person just typed.
        ou.ou_name = business_unit
    return ou


def _get_or_create_bank_account(db: Session, acct: str, body: "SaveRecipeRequest",
                                  ou: OrganizationUnit) -> tuple[BankAccount, bool]:
    """Returns (bank_account, created)."""
    existing = (
        db.query(BankAccount)
        .filter(BankAccount.account_number == acct, BankAccount.bank_name == (body.bank or "UNKNOWN"))
        .first()
    )
    if existing:
        existing.display_name = body.display_name
        existing.account_last4 = last4(acct)
        if body.currency:
            existing.currency = body.currency
        if existing.ou_id != ou.id:
            existing.ou_id = ou.id
        return existing, False

    account = BankAccount(
        ou_id=ou.id,
        account_number=acct,
        account_last4=last4(acct),
        display_name=body.display_name,
        bank_name=body.bank or "UNKNOWN",
        bank_config_key=acct,
        currency=body.currency,
    )
    db.add(account)
    db.flush()
    return account, True


@router.post("/builder/save")
def builder_save(body: SaveRecipeRequest, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("config:manage"))):
    """Save/attach a recipe. If the account exists, a new recipe version is added
    for the format; otherwise a new account is created — always linked to a real
    OrganizationUnit (OU + Business Unit), never saved without one."""
    acct = str(body.account_number).strip()
    if not acct:
        raise AppError(ErrorCode.CONFIG_FIELD_REQUIRED, detail="account number")
    if not body.display_name.strip():
        raise AppError(ErrorCode.CONFIG_FIELD_REQUIRED, detail="display name")
    fmt = _FMT_ALIASES.get(body.format.lower(), body.format.lower())
    if fmt not in ("xlsx", "xls", "csv", "pdf"):
        raise AppError(ErrorCode.CONFIG_FILE_TYPE_UNSUPPORTED, detail=f"'{body.format}' -- use xlsx, xls, csv, or pdf")
    if not body.recipe.get("account_locator"):
        raise AppError(ErrorCode.CONFIG_RECIPE_INVALID, detail="no account locator -- go back to the Account step")
    if not body.ou_number.strip():
        raise AppError(ErrorCode.CONFIG_FIELD_REQUIRED, detail="Organization Unit")
    if not body.business_unit.strip():
        raise AppError(ErrorCode.CONFIG_FIELD_REQUIRED, detail="Business Unit")

    try:
        ou = _get_or_create_organization_unit(db, body.ou_number, body.business_unit, body.functional_currency)
        account, created = _get_or_create_bank_account(db, acct, body, ou)

        existing_versions = (
            db.query(AccountConfigRecipe)
            .filter(AccountConfigRecipe.bank_account_id == account.id, AccountConfigRecipe.format == fmt)
            .all()
        )
        format_created = not existing_versions
        next_version = max((v.version for v in existing_versions), default=0) + 1

        recipe_row = AccountConfigRecipe(
            bank_account_id=account.id,
            format=fmt,
            version=next_version,
            recipe=body.recipe,
            created_by=(body.created_by or "").strip() or None,
        )
        db.add(recipe_row)

        action = "config.create" if (created or format_created) else "config.version_added"
        log_activity(db, user, action=action, entity_type="AccountConfig",
                     entity_id=acct,
                     metadata={"display_name": body.display_name, "format": fmt, "version": next_version,
                               "ou_number": ou.ou_number, "business_unit": ou.ou_name})

        db.commit()
    except AppError:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("builder_save failed for account %s", acct)
        raise AppError(ErrorCode.CONFIG_SAVE_FAILED, detail=f"{e.__class__.__name__} -- nothing was changed")

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
        "ou_number": ou.ou_number,
        "business_unit": ou.ou_name,
        "message": message,
    }


# ── list / fetch accounts (Manage + Clone-from-existing) ─────────────────────────

@router.get("/builder/accounts")
def list_accounts(user: User = Depends(require_permission("run:view"))):
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
def get_account(account_number: str, user: User = Depends(require_permission("run:view"))):
    """Full account entry (incl. recipes) — used to clone an existing config."""
    entry = load_account_configs().get(account_number)
    if not entry:
        raise AppError(ErrorCode.CONFIG_NOT_FOUND, detail=f"account '{account_number}'")
    return entry


# ── test an existing saved recipe ────────────────────────────────────────────────

class TestExistingRequest(BaseModel):
    filename: str
    account_number: str
    format: str | None = None


@router.post("/test-existing")
def test_existing(body: TestExistingRequest, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("config:manage"))):
    """Run a saved account recipe against the uploaded file (preview + row count)."""
    entry = load_account_configs().get(body.account_number)
    if not entry:
        raise AppError(ErrorCode.CONFIG_NOT_FOUND, detail=f"account '{body.account_number}'")
    record, local_path = _local_path(db, body.filename)
    fmt = body.format or _file_format(body.filename)
    recipe = active_recipe(entry, fmt)   # test always runs the active (latest) version
    if not recipe:
        raise AppError(ErrorCode.CONFIG_NOT_FOUND, detail=f"account '{body.account_number}' has no '{fmt}' recipe")
    return _test_recipe(local_path, recipe, body.account_number, record.filename)


# ── delete account / recipe ──────────────────────────────────────────────────────

@router.delete("/builder/{account_number}")
def delete_account(account_number: str, db: Session = Depends(get_db),
                   user: User = Depends(require_permission("config:manage"))):
    """Delete an entire account config (all its recipes). The OU itself is left
    alone — other accounts may still reference it."""
    account = db.query(BankAccount).filter(BankAccount.account_number == account_number).first()
    if not account:
        raise AppError(ErrorCode.CONFIG_NOT_FOUND, detail=f"account '{account_number}'")
    db.query(AccountConfigRecipe).filter(AccountConfigRecipe.bank_account_id == account.id).delete()
    db.delete(account)
    db.commit()
    reload_account_configs()
    return {"success": True, "deleted": account_number}


@router.delete("/builder/{account_number}/{fmt}")
def delete_recipe(account_number: str, fmt: str, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("config:manage"))):
    """Delete every version of a single format recipe. If it was the account's
    last format, the account itself is removed too."""
    fmt = _FMT_ALIASES.get(fmt.lower(), fmt.lower())
    account = db.query(BankAccount).filter(BankAccount.account_number == account_number).first()
    if not account:
        raise AppError(ErrorCode.CONFIG_NOT_FOUND, detail=f"account '{account_number}'")
    versions = (
        db.query(AccountConfigRecipe)
        .filter(AccountConfigRecipe.bank_account_id == account.id, AccountConfigRecipe.format == fmt)
        .all()
    )
    if not versions:
        raise AppError(ErrorCode.CONFIG_NOT_FOUND,
                        detail=f"account '{account_number}' has no '{fmt}' recipe to delete")
    for v in versions:
        db.delete(v)
    db.flush()
    remaining = (
        db.query(AccountConfigRecipe).filter(AccountConfigRecipe.bank_account_id == account.id).count()
    )
    account_removed = remaining == 0
    if account_removed:
        db.delete(account)
    db.commit()
    reload_account_configs()
    return {"success": True, "account_number": account_number, "format": fmt,
            "account_removed": account_removed}


# ── detect / candidates ──────────────────────────────────────────────────────────

@router.get("/detect/{filename}")
def detect_for_file(filename: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("run:view"))):
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