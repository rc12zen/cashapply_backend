"""
app.bff.run_routes
===================
/api/run/* — matches lib/api.ts: getFiles, startRun, getStatus, resetRun,
deleteFile, uploadStatement, getRunHistory, getFilePreview.

UPDATED (auth/RBAC/duplicate-detection/audit integration — see
cashapply-platform-hardening-design.md):
  - Every route now requires an authenticated user via require_permission().
  - /upload goes through the new ingestion pipeline (hash-dedup + background
    parse job) instead of the old synchronous full-parse-on-upload path.
  - New GET /files/{source_file_id}/ingest-status for the frontend's
    "Processing..." → "You can now start Analysis" poll loop.
  - /start uses procrastinate (run_analysis_task) instead of a bare thread,
    and checks for an overlapping in-flight run before even attempting the
    advisory lock (see rule_engine/orchestrator.py's _run_analysis_locked).
  - Every mutating action is logged via audit.service.log_activity().
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..common.upload_validation import validate_statement_upload, validate_statement_size
from .date_range import parse_date_from, parse_date_to
from ..db.models import AnalysisRun, BankAccount, LineItem, RunStatus, SourceFile, StatementTransactionRow, User
from ..storage.client import get_storage_client
from ..deps import get_db
from ..auth import require_permission
from ..audit.service import log_activity
from ..ingestion.ingest_service import handle_statement_upload_v2
from ..tasks.analysis_tasks import run_analysis_task
from ..bank_statement.preview import preview_bank_file
from .metrics import compute_run_summary_row
from .date_range import parse_date_from, parse_date_to

router = APIRouter()

STATEMENT_BUCKET = "bank-statements"


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _visible_to(user: User):
    """Per-user gating for the Home statement lists.

    A statement is visible to the current user if THEY uploaded it, or if it
    has no recorded owner (legacy/shared rows created before per-user gating —
    uploaded_by_user_id IS NULL). Every new upload records the uploader (see
    ingest_service.handle_statement_upload_v2), so NULL only ever means "old
    row", never a new leak. Scopes the file/account LISTS only — runs, results
    and history remain global by design (a run consumes rows account-wide,
    regardless of who uploaded them)."""
    return or_(
        SourceFile.uploaded_by_user_id == user.id,
        SourceFile.uploaded_by_user_id.is_(None),
    )


@router.get("/files")
def get_files(db: Session = Depends(get_db), user: User = Depends(require_permission("run:view"))):
    rows = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "bank_statement", SourceFile.archived.is_(False))
        .filter(_visible_to(user))
        .all()
    )
    storage = get_storage_client()
    out = []
    for r in rows:
        size_mb = 0.0
        try:
            data = storage.read(STATEMENT_BUCKET, r.storage_key)
            size_mb = round(len(data) / (1024 * 1024), 2)
        except Exception:
            pass
        out.append({
            "filename": r.filename,
            "bank_name": r.bank_config_key or "Unknown",
            # Raw matched-config key. Set even when ingest_status="unrecognized"
            # if the FORMAT was recognised and we refused only because some
            # account in the file has no config (detector's INCOMPLETE_ACCOUNTS)
            # — that's how the UI tells "add the missing account" apart from
            # "this format is unknown".
            "bank_config_key": r.bank_config_key,
            "size_mb": size_mb,
            "bank_account_id": r.bank_account_id,
            "business_unit": r.business_unit or "",
            "ou_number": r.ou_number or "",
            "source_file_id": r.id,
            "ingest_status": r.ingest_status,
            "ingest_error": r.ingest_error,
            "new_row_count": r.new_row_count,
            "duplicate_row_count": r.duplicate_row_count,
        })
    return {"files": out}


@router.get("/pending-by-account")
def get_pending_by_account(db: Session = Depends(get_db), user: User = Depends(require_permission("run:view"))):
    """
    Groups every currently-listed (non-archived) bank statement file by
    the bank account it belongs to, with a LIVE count of unconsumed rows
    for that account (queried fresh from StatementTransactionRow, not the
    per-file new_row_count snapshot taken at upload time — a prior run
    may have already consumed some of an account's rows since then,
    across possibly-different files, so the file-level number alone can
    be stale/misleading).

    Backs the dashboard's account-level "include in next run" checkboxes:
    the orchestrator already resolves and consumes rows by
    bank_account_id, not by file (see rule_engine/orchestrator.py) — this
    endpoint exposes that same grouping so the UI's selection unit matches
    what actually happens when a run executes, instead of offering
    file-level checkboxes that would silently not match real behavior.
    """
    files = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "bank_statement", SourceFile.archived.is_(False))
        .filter(_visible_to(user))
        .all()
    )

    # Accounts each file's ROWS were actually attributed to. A file whose
    # account_locator is a COLUMN holds several accounts, so grouping by
    # SourceFile.bank_account_id (the primary only) showed a multi-account
    # statement as if it were a single-account one — hiding the other accounts and
    # their Business Units from the Confirm Run dialog, which exists specifically
    # so a wrong BU is caught before Oracle rejects every receipt for it.
    accounts_by_file: dict[int, set[int]] = {}
    if files:
        for fid, aid in (
            db.query(StatementTransactionRow.source_file_id, StatementTransactionRow.bank_account_id)
            .filter(StatementTransactionRow.source_file_id.in_([f.id for f in files]))
            .distinct().all()
        ):
            if aid is not None:
                accounts_by_file.setdefault(fid, set()).add(aid)

    groups: dict[int | None, dict] = {}
    for f in files:
        # One entry per account in the file; falls back to the file-level account
        # for files with no rows yet (still ingesting / unrecognised).
        for key in sorted(accounts_by_file.get(f.id) or {f.bank_account_id},
                          key=lambda k: (k is None, k)):
            _add_file_to_group(db, groups, key, f)

    return _finalize_pending_groups(db, groups)


def _add_file_to_group(db: Session, groups: dict, key: int | None, f: SourceFile) -> None:
    if key not in groups:
        account = db.query(BankAccount).get(key) if key is not None else None
        # Prefer the account's CURRENT OU mapping (BankAccount.ou_id ->
        # OrganizationUnit) — the authoritative source the run itself
        # resolves against. f.business_unit / f.ou_number are only a snapshot
        # taken at UPLOAD time (ingest_service), so a file ingested before
        # its account's OU was set up (or before it was fixed on the
        # Accounts & OU's page) would otherwise show a stale "No Business
        # Unit" here even though the account is now mapped correctly.
        ou = account.organization_unit if account else None
        groups[key] = {
            "bank_account_id": key,
            "account_number": account.account_number if account else None,
            "bank_name": (account.bank_name if account else None) or f.bank_config_key or "Unknown",
            "business_unit": (ou.ou_name if ou else None) or f.business_unit or "",
            "ou_number": (ou.ou_number if ou else None) or f.ou_number or "",
            "files": [],
            "pending_row_count": 0,
        }
    groups[key]["files"].append({
        "filename": f.filename,
        "source_file_id": f.id,
        "ingest_status": f.ingest_status,
        "new_row_count": f.new_row_count,
    })


def _finalize_pending_groups(db: Session, groups: dict) -> dict:
    for key, group in groups.items():
        if key is not None:
            group["pending_row_count"] = (
                db.query(StatementTransactionRow)
                .filter(
                    StatementTransactionRow.bank_account_id == key,
                    StatementTransactionRow.consumed_by_run_id.is_(None),
                )
                .count()
            )
            # PATCH: distinguish "genuinely unrecognised" from "recognised,
            # but every row here has already been through a run" — the
            # latter is the re-upload-of-an-already-processed-statement
            # case (rows survive row_hash dedup and land back on this same
            # account with consumed_by_run_id already set, even though the
            # newly-uploaded file itself is a different byte-for-byte file
            # than whatever was uploaded originally). Only computed when
            # there's nothing left pending, since that's the only time the
            # frontend needs it — avoids an extra query per runnable account.
            group["last_consumed_run_id"] = None
            if group["pending_row_count"] == 0:
                group["last_consumed_run_id"] = (
                    db.query(func.max(StatementTransactionRow.consumed_by_run_id))
                    .filter(
                        StatementTransactionRow.bank_account_id == key,
                        StatementTransactionRow.consumed_by_run_id.isnot(None),
                    )
                    .scalar()
                )
        else:
            # No resolved account (e.g. account number missing at ingest) —
            # fall back to summing the per-file snapshot, same fallback
            # rule the orchestrator itself uses for these files.
            group["pending_row_count"] = sum(fi["new_row_count"] or 0 for fi in group["files"])
            group["last_consumed_run_id"] = None

    return {"accounts": list(groups.values())}


# ═══════════════════════════════════════════════════════════════════════════
# Run preflight — the data behind the Confirm Analysis Run dialog
# ═══════════════════════════════════════════════════════════════════════════
# An analysis run is IRREVERSIBLE: the orchestrator stamps
# consumed_by_run_id on every row it processes, and /start refuses to run
# against an account with no unconsumed rows left (see the guard in
# start_run below). There is deliberately no "undo" and no "re-run" — a
# mistake has to be corrected row-by-row through HITL instead. That makes
# the confirmation dialog the last, and only, place a wrong run can be
# caught, so it gets the FULL picture rather than just accounts + Business
# Units: the settings that will actually shape the results (functional
# currency, credit rule, aging report, AI availability, settlement
# identifiers, tolerances) are all resolved live here and returned
# together, alongside a computed blockers/warnings split.
#
# Blockers vs warnings
# --------------------
# blockers  = the run cannot or must not proceed.
#             NO_FILES_SELECTED / NO_ANALYZABLE_ROWS / ALREADY_ANALYSED /
#             RUN_IN_PROGRESS each mirror a guard start_run() itself
#             enforces, so those are a preview of a real rejection rather
#             than a second opinion that could disagree with it.
#             NO_FUNCTIONAL_CURRENCY is the one exception — it is advisory
#             only, with no counterpart in start_run(), so a direct API
#             call can still bypass it. It's near-unreachable in practice
#             (OrganizationUnit.functional_currency is NOT NULL and
#             BankAccount.ou_id is NOT NULL, so it takes a blank string to
#             trigger); add the matching guard to start_run() if that ever
#             needs to be a hard server-side stop.
# warnings  = the run WILL proceed but produce degraded results (e.g. no
#             aging report loaded -> nothing to match invoices against).
#             Surfaced loudly, but the person decides.
#
# Read-only: nothing here mutates, and it never touches aging_store's
# in-memory map beyond reading its status.


def _describe_credit_rule(recipe: dict) -> dict | None:
    """Human-readable summary of a recipe's `credit_rule`.

    The credit rule decides which statement rows are even TREATED as
    incoming money — get it wrong and a run either silently skips real
    receipts or ingests debits as credits, and neither is undoable once
    the rows are consumed. See bank_statement/credit_rules.py for the
    evaluator these three types map to.
    """
    from ..bank_statement.credit_rules import _column_for_logical

    rule = (recipe or {}).get("credit_rule")
    if not isinstance(rule, dict) or not rule.get("type"):
        return None

    rule_type = rule.get("type")
    field = rule.get("field") or ""
    fields = (recipe or {}).get("fields") or []
    # amount_positive / column_not_blank name a LOGICAL field, which maps to
    # a physical spreadsheet column; flag_matches names the raw column
    # directly (same split as eval_credit_rule).
    column = field
    if rule_type in ("amount_positive", "column_not_blank"):
        column = _column_for_logical(fields, field) or field

    if rule_type == "amount_positive":
        description = f'A positive value in "{column}" counts as a credit'
    elif rule_type == "column_not_blank":
        description = f'A non-blank positive value in "{column}" counts as a credit'
    elif rule_type == "flag_matches":
        description = f'"{column}" matching {rule.get("pattern")!r} counts as a credit'
    else:
        description = f'Unknown credit rule type "{rule_type}"'

    return {
        "type": rule_type,
        "column": column,
        "pattern": rule.get("pattern"),
        "description": description,
    }


def _credit_rules_for_account(db: Session, bank_account_id: int, filenames: list[str]) -> list[dict]:
    """Latest recipe per file FORMAT the selected statements actually use.

    Recipes are versioned per (bank_account_id, format) and append-only
    (see AccountConfigRecipe) — only the highest version of each format is
    ever applied, so that's the only one worth showing. Scoped to the
    formats present in this run's files: an account may also carry, say, a
    pdf recipe that this run will never touch.
    """
    from ..db.models import AccountConfigRecipe

    formats = {f.rsplit(".", 1)[-1].lower() for f in filenames if "." in f}
    recipes = (
        db.query(AccountConfigRecipe)
        .filter(AccountConfigRecipe.bank_account_id == bank_account_id)
        .order_by(AccountConfigRecipe.format, AccountConfigRecipe.version.desc())
        .all()
    )
    latest_by_format: dict[str, AccountConfigRecipe] = {}
    for r in recipes:
        fmt = (r.format or "").lower()
        if formats and fmt not in formats:
            continue
        if fmt not in latest_by_format:      # ordered version DESC -> first wins
            latest_by_format[fmt] = r

    out = []
    for fmt, rec in sorted(latest_by_format.items()):
        out.append({
            "format": fmt,
            "recipe_version": rec.version,
            "credit_rule": _describe_credit_rule(rec.recipe or {}),
        })
    return out


def _settlement_identifier_context(db: Session) -> dict:
    """The ACTIVE settlement identifiers a run will classify against.

    Global, not per-account (the table has no bank_account_id) — so these
    apply to every row in the run. Inactive rows are filtered out here:
    settlement_identifier.load_identifiers() only ever matches active
    ones, and listing dormant patterns as if they were in force would be
    worse than not showing them.
    """
    from ..db.models import SettlementIdentifier, SettlementIdentifierType

    rows = (
        db.query(SettlementIdentifier)
        .filter(SettlementIdentifier.active.is_(True))
        .all()
    )
    by_type: dict[str, list[dict]] = {t.value: [] for t in SettlementIdentifierType}
    for r in rows:
        key = r.identifier_type.value if hasattr(r.identifier_type, "value") else str(r.identifier_type)
        by_type.setdefault(key, []).append({
            "id": r.id,
            "pattern": r.pattern,
            "provider_name": r.provider_name,
            "sub_customer_count": len(r.sub_customers or []),
        })
    return by_type


@router.get("/preflight")
def get_run_preflight(
    selected_files: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    """Everything a person needs to review before starting an irreversible run.

    Backs ConfirmRunDialog. See the module-level block above this function
    for the blockers-vs-warnings contract.
    """
    from ..aging import aging_store
    from ..db.settings import get_settings
    from ..extraction.ai_providers import get_ai_status

    blockers: list[dict] = []
    warnings: list[dict] = []

    # ── Accounts in scope ────────────────────────────────────────────────
    # Same grouping as /pending-by-account (a statement whose account
    # locator is a COLUMN spans several accounts, and a run can't take a
    # subset of one file), narrowed to the selected filenames.
    files = (
        db.query(SourceFile)
        .filter(
            SourceFile.kind == "bank_statement",
            SourceFile.archived.is_(False),
            SourceFile.filename.in_(selected_files),
        )
        .filter(_visible_to(user))
        .all()
        if selected_files else []
    )

    accounts_by_file: dict[int, set[int]] = {}
    if files:
        for fid, aid in (
            db.query(StatementTransactionRow.source_file_id, StatementTransactionRow.bank_account_id)
            .filter(StatementTransactionRow.source_file_id.in_([f.id for f in files]))
            .distinct().all()
        ):
            if aid is not None:
                accounts_by_file.setdefault(fid, set()).add(aid)

    groups: dict[int | None, dict] = {}
    for f in files:
        for key in sorted(accounts_by_file.get(f.id) or {f.bank_account_id},
                          key=lambda k: (k is None, k)):
            _add_file_to_group(db, groups, key, f)
    accounts = _finalize_pending_groups(db, groups)["accounts"]

    # Scale + period of the rows THIS run will actually consume, per account:
    # the total incoming money and the statement-date span of the unconsumed
    # rows. One grouped pass over the accounts in scope rather than a query
    # per account. Purely descriptive — it doesn't gate anything, it just
    # lets the person see "$X across dates A–B" before committing to an
    # irreversible run. credit_amount is in the ACCOUNT'S OWN statement
    # currency (group["account_currency"]), so these are never summed across
    # accounts — a cross-currency total would be meaningless.
    account_keys = [g["bank_account_id"] for g in accounts if g["bank_account_id"] is not None]
    pending_agg: dict[int, dict] = {}
    if account_keys:
        for aid, total, dfrom, dto in (
            db.query(
                StatementTransactionRow.bank_account_id,
                func.sum(StatementTransactionRow.credit_amount),
                func.min(StatementTransactionRow.statement_date),
                func.max(StatementTransactionRow.statement_date),
            )
            .filter(
                StatementTransactionRow.bank_account_id.in_(account_keys),
                StatementTransactionRow.consumed_by_run_id.is_(None),
            )
            .group_by(StatementTransactionRow.bank_account_id)
            .all()
        ):
            pending_agg[aid] = {
                "pending_credit_total": float(total) if total is not None else None,
                "pending_date_from": dfrom.isoformat() if dfrom else None,
                "pending_date_to": dto.isoformat() if dto else None,
            }

    # Most recent run that consumed ANY row of each account (over all rows,
    # not just the unconsumed ones) — context for whether this is a fresh
    # account or a follow-up statement on one already analysed before. Two
    # small grouped queries rather than one per account.
    last_run_by_acct: dict[int, dict] = {}
    if account_keys:
        max_runs = (
            db.query(
                StatementTransactionRow.bank_account_id,
                func.max(StatementTransactionRow.consumed_by_run_id),
            )
            .filter(
                StatementTransactionRow.bank_account_id.in_(account_keys),
                StatementTransactionRow.consumed_by_run_id.isnot(None),
            )
            .group_by(StatementTransactionRow.bank_account_id)
            .all()
        )
        run_ids = {rid for _, rid in max_runs if rid is not None}
        run_when: dict[int, object] = {}
        if run_ids:
            for rid, started, completed in (
                db.query(AnalysisRun.run_id, AnalysisRun.started_at, AnalysisRun.completed_at)
                .filter(AnalysisRun.run_id.in_(run_ids))
                .all()
            ):
                # completed_at is the meaningful "when it finished"; fall back
                # to started_at for a run still in flight or missing a stamp.
                run_when[rid] = completed or started
        for aid, rid in max_runs:
            when = run_when.get(rid)
            last_run_by_acct[aid] = {
                "last_run_id": rid,
                "last_run_at": when.isoformat() if when else None,
            }

    # ── Per-account enrichment: functional currency + credit rule ───────
    for group in accounts:
        key = group["bank_account_id"]
        # Same key the frontend derives for selection (see page.tsx's
        # accountGroups) — returned here so the dialog doesn't recompute it.
        group["key"] = str(key) if key is not None else "unresolved"
        group["additional_business_units"] = []
        group["functional_currency"] = None
        group["account_currency"] = None
        group["credit_rules"] = []
        group.update(pending_agg.get(key) or {
            "pending_credit_total": None, "pending_date_from": None, "pending_date_to": None,
        })
        group.update(last_run_by_acct.get(key) or {"last_run_id": None, "last_run_at": None})
        group["runnable"] = key is not None and group["pending_row_count"] > 0
        if key is None:
            continue
        account = db.query(BankAccount).get(key)
        if account is None:
            continue
        group["account_currency"] = account.currency
        ou = account.organization_unit
        # The currency every amount on this account is converted INTO (see
        # rule_engine/fx_service.get_functional_currency) and snapshotted
        # onto each row. Column is NOT NULL, so a missing one means the OU
        # link itself is broken — hence a blocker, not a warning.
        group["functional_currency"] = (ou.functional_currency or None) if ou else None
        # Additional Business Units for a multi-BU account — the run
        # resolves against the FULL set (rule_engine/ou_resolver.py), so
        # showing only the primary would understate the run's reach.
        group["additional_business_units"] = [
            {"ou_name": o.ou_name, "ou_number": o.ou_number, "functional_currency": o.functional_currency}
            for o in (account.all_organization_units or [])[1:]
        ]
        group["credit_rules"] = _credit_rules_for_account(
            db, key, [fi["filename"] for fi in group["files"]]
        )

    runnable = [g for g in accounts if g["runnable"]]
    total_pending_rows = sum(g["pending_row_count"] for g in runnable)

    # Run-level rollups (over runnable accounts only):
    #  - credit grouped BY currency, never a single cross-currency sum, so a
    #    mixed-currency run reads as "USD 4.0M · INR 5.1M", not one wrong
    #    number;
    #  - the widest statement-date span across all accounts.
    credit_by_currency: dict[str, float] = {}
    for g in runnable:
        if g["pending_credit_total"] is None:
            continue
        cur = g["account_currency"] or "—"
        credit_by_currency[cur] = credit_by_currency.get(cur, 0.0) + g["pending_credit_total"]
    date_froms = [g["pending_date_from"] for g in runnable if g["pending_date_from"]]
    date_tos = [g["pending_date_to"] for g in runnable if g["pending_date_to"]]

    # Rows skipped as already-ingested duplicates on THIS upload — deduped by
    # source file (a multi-account file appears under several accounts, but
    # its duplicate_row_count is a single per-file snapshot), so it's read
    # straight off the SourceFile rows rather than summed from the groups.
    total_duplicates = sum((f.duplicate_row_count or 0) for f in files)

    # ── Blockers (mirror what /start actually enforces) ──────────────────
    if not selected_files:
        blockers.append({
            "code": "NO_FILES_SELECTED",
            "message": "No statements are selected for this run.",
        })
    elif not runnable:
        # Same condition as start_run's RUN_NO_ANALYZABLE_FILES guard.
        already_consumed = [g for g in accounts if g.get("last_consumed_run_id")]
        if already_consumed:
            runs = sorted({g["last_consumed_run_id"] for g in already_consumed})
            blockers.append({
                "code": "ALREADY_ANALYSED",
                "message": (
                    "Every row in the selected statement(s) has already been analysed by "
                    + ("run " if len(runs) == 1 else "runs ")
                    + ", ".join(f"#{r}" for r in runs)
                    + ". An analysis cannot be re-run — open that run to review or correct its rows."
                ),
            })
        else:
            blockers.append({
                "code": "NO_ANALYZABLE_ROWS",
                "message": (
                    "None of the selected statements have rows left to analyse. A statement is "
                    "analysable only when its account is recognised and it still has unprocessed rows."
                ),
            })

    running = db.query(AnalysisRun).filter(AnalysisRun.status == RunStatus.RUNNING).first()
    if running:
        blockers.append({
            "code": "RUN_IN_PROGRESS",
            "message": f"Analysis run #{running.run_id} is still running. Only one run can be in progress at a time.",
        })

    missing_currency = [g for g in runnable if not g["functional_currency"]]
    if missing_currency:
        blockers.append({
            "code": "NO_FUNCTIONAL_CURRENCY",
            "message": (
                f"{len(missing_currency)} account(s) have no functional currency on their Organization "
                "Unit, so their amounts cannot be converted. Fix this on the Accounts & OU's page first."
            ),
            "accounts": [g["key"] for g in missing_currency],
        })

    # ── Global run context ───────────────────────────────────────────────
    aging = aging_store.get_status()
    if not aging.get("loaded"):
        warnings.append({
            "code": "NO_AGING_REPORT",
            "message": (
                "No aging report is loaded. Invoice matching has nothing to match against, so most "
                "rows will finish as Unidentified — and this run cannot be repeated once it completes."
            ),
        })

    ai = get_ai_status()
    # `enabled` is the AI_EXTRACTION_ENABLED master switch. It only exists on
    # branches that carry that feature — absent here means "no master switch,
    # so AI is not switched off", NOT "off". Normalising to True keeps the
    # payload's contract stable either way, so the dialog never reports AI as
    # off just because the field is missing.
    ai_enabled = True if ai.get("enabled") is None else bool(ai.get("enabled"))
    if not ai_enabled:
        warnings.append({
            "code": "AI_DISABLED",
            "message": (
                "AI extraction is turned off. Rows will be matched with pattern/regex rules only — "
                "anything they can't resolve stays Unidentified instead of getting the AI second pass."
            ),
        })
    elif not ai.get("active"):
        warnings.append({
            "code": "AI_UNAVAILABLE",
            "message": ai.get("message") or "The AI provider is not reachable — Layer 2B will be skipped.",
        })

    identifiers = _settlement_identifier_context(db)
    if not identifiers.get("third_party_provider"):
        warnings.append({
            "code": "NO_THIRD_PARTY_PROVIDERS",
            "message": (
                "No third-party providers are configured. Payments received via a broker/distributor "
                "will not be flagged for distribution and will be treated as direct customer payments."
            ),
        })

    missing_bu = [g for g in runnable if not g["business_unit"] or not g["ou_number"]]
    if missing_bu:
        warnings.append({
            "code": "NO_BUSINESS_UNIT",
            "message": (
                f"{len(missing_bu)} account(s) have no Business Unit resolved. Their rows will still be "
                "analysed, but won't be postable to Oracle until that's fixed on the Accounts & OU's page."
            ),
        })

    # Statements whose rows span several accounts — a run can't take a
    # subset of one file, so every account in such a statement goes
    # together whether or not that was intended.
    file_account_count: dict[str, int] = {}
    for g in accounts:
        for fi in g["files"]:
            file_account_count[fi["filename"]] = file_account_count.get(fi["filename"], 0) + 1
    multi_account_files = sorted(fn for fn, n in file_account_count.items() if n > 1)

    s = get_settings()
    return {
        "accounts": accounts,
        "totals": {
            "accounts": len(accounts),
            "runnable_accounts": len(runnable),
            "statements": len(file_account_count),
            "pending_rows": total_pending_rows,
            "duplicate_rows_ignored": total_duplicates,
            "credit_by_currency": credit_by_currency,
            "date_from": min(date_froms) if date_froms else None,
            "date_to": max(date_tos) if date_tos else None,
        },
        "context": {
            "aging": aging,
            "ai": {
                "provider": ai.get("provider"),
                "model": ai.get("model"),
                "enabled": ai_enabled,
                "configured": ai.get("configured"),
                "active": ai.get("active"),
                "message": ai.get("message"),
            },
            "settlement_identifiers": identifiers,
            # Read straight from settings — env-driven, with no DB override
            # and no other endpoint serving them (see db/settings.py). Only
            # the short-payment tolerance is surfaced: it directly explains a
            # run outcome the person will see (R9b acceptable vs R9c conflict).
            # The fuzzy-match minimum was dropped from this summary — it's an
            # internal matcher knob, not a decision the reviewer acts on.
            "tolerances": {
                "short_payment_tolerance_pct": s.SHORT_PAYMENT_TOLERANCE_PCT,
            },
        },
        "multi_account_files": multi_account_files,
        "blockers": blockers,
        "warnings": warnings,
        "can_start": not blockers,
    }


@router.post("/upload")
async def upload_statement(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("statement:upload")),
):
    # Reject the wrong file type / oversized files up front, with a clear
    # message, before reading the bytes or touching storage. file.size is the
    # declared Content-Length (early reject); len(data) is the authoritative
    # re-check after reading.
    validate_statement_upload(file.filename)
    validate_statement_size(file.size)
    data = await file.read()
    validate_statement_size(len(data))
    result = handle_statement_upload_v2(db, file.filename, data, uploaded_by=user)

    if result.get("duplicate"):
        # 200, not an error status — the frontend shows this as an informational
        # banner with the "view existing run" link, not a failed-upload toast.
        return result

    # Defer the background parse/dedupe job (procrastinate — see design doc §5).
    from ..tasks.ingestion_tasks import ingest_statement_task
    ingest_statement_task.defer(source_file_id=result["source_file_id"])

    return result


@router.post("/files/{source_file_id}/reingest")
def reingest_statement(source_file_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_permission("statement:upload"))):
    """
    Re-run ingestion for an already-uploaded statement, in place. Used after a
    config is created for a previously-UNKNOWN file via the Home "Configure"
    flow: the file's bytes are still in storage, but its original ingest failed
    ("Bank format not auto-detected") and left it error/unresolved/0-rows. A
    plain re-upload would hit the duplicate-hash guard and do nothing, so this
    re-defers the ingest job — detect_config now matches, rows parse, the bank
    account links, and status flips to ready. ingest_and_parse is idempotent.
    """
    record = db.query(SourceFile).get(source_file_id)
    if not record or record.kind != "bank_statement":
        raise AppError(ErrorCode.STATEMENT_NOT_FOUND, detail="not a bank statement")
    record.ingest_status = "processing"
    record.ingest_error = None
    db.commit()
    from ..tasks.ingestion_tasks import ingest_statement_task
    ingest_statement_task.defer(source_file_id=source_file_id)
    return {"source_file_id": source_file_id, "ingest_status": "processing"}


@router.get("/files/{source_file_id}/ingest-status")
def get_ingest_status(source_file_id: int, db: Session = Depends(get_db),
                       user: User = Depends(require_permission("run:monitor"))):
    record = db.query(SourceFile).get(source_file_id)
    if not record:
        raise AppError(ErrorCode.STATEMENT_NOT_FOUND)
    # Rows still awaiting a run for this file's account (consumed_by_run_id IS
    # NULL). This is the "can it actually be analyzed?" signal — distinct from
    # new_row_count (an INGESTION-dedup number). A re-uploaded file can ingest
    # 0 new rows yet still have pending rows to run (they were ingested by an
    # earlier upload but never consumed by a completed run), so the UI must
    # key "ready to analyze" off this, not off new_row_count.
    pending_row_count = 0
    if record.bank_account_id is not None:
        pending_row_count = (
            db.query(StatementTransactionRow)
            .filter(
                StatementTransactionRow.bank_account_id == record.bank_account_id,
                StatementTransactionRow.consumed_by_run_id.is_(None),
            )
            .count()
        )
    return {
        "source_file_id": record.id,
        "filename": record.filename,
        "ingest_status": record.ingest_status,      # "processing" | "ready" | "error"
        "ingest_error": record.ingest_error,
        "new_row_count": record.new_row_count,
        "duplicate_row_count": record.duplicate_row_count,
        "pending_row_count": pending_row_count,
    }


@router.delete("/files/{filename}")
def delete_file(filename: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("statement:upload"))):
    """
    Removes the file from the 'active' UI list for the next run.
    Sets archived=True on the SourceFile record — the file bytes stay in
    storage so historical runs that referenced this file can still retrieve it.
    Does NOT delete from blob/storage.

    PATCH: archives ALL non-archived rows matching this filename, not just
    the first one found. handle_statement_upload_v2() always inserts a new
    SourceFile row (e.g. re-uploading the same file after the Config
    Builder wizard), so more than one row can share a filename. Using
    .first() left the older/newer duplicate un-archived, which made the
    file reappear in get_files() immediately after being "removed".
    """
    records = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement",
        SourceFile.filename == filename,
        SourceFile.archived.is_(False),
    ).all()
    for record in records:
        record.archived = True
    log_activity(db, user, action="statement.delete", entity_type="SourceFile",
                 entity_id=filename, ip_address=_client_ip(request),
                 metadata={"rows_archived": len(records)})
    db.commit()
    return {"archived": filename, "rows_archived": len(records)}


@router.post("/start")
def start_run(payload: dict, request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_permission("run:start"))):
    selected_files = payload.get("selected_files", [])
    if not selected_files:
        raise AppError(ErrorCode.RUN_NO_FILES_SELECTED)

    # Guard: at least one selected statement must be analyzable — i.e. resolve
    # to a bank account that still has unconsumed rows. The orchestrator
    # consumes rows by bank_account_id, so a run against only Unknown
    # (unrecognised, no bank_account_id) or already-consumed statements would
    # do nothing. Reject it here instead of creating a no-op run — this is the
    # server-side counterpart to the Home tab's "runnable account" gate, so a
    # direct API call can't bypass it.
    sources = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement",
        SourceFile.filename.in_(selected_files),
    ).all()
    account_ids = {s.bank_account_id for s in sources if s.bank_account_id is not None}
    pending_rows = (
        db.query(StatementTransactionRow)
        .filter(
            StatementTransactionRow.bank_account_id.in_(account_ids),
            StatementTransactionRow.consumed_by_run_id.is_(None),
        )
        .count()
        if account_ids else 0
    )
    if pending_rows == 0:
        raise AppError(ErrorCode.RUN_NO_ANALYZABLE_FILES)

    # Fast-fail check (UX layer) — the advisory lock in
    # _run_analysis_locked() is the actual correctness guarantee for true
    # concurrent requests; this just avoids the common impatient-double-click
    # case ever reaching it. See design doc §4.
    existing_running = db.query(AnalysisRun).filter(AnalysisRun.status == RunStatus.RUNNING).first()
    if existing_running:
        raise AppError(ErrorCode.RUN_ALREADY_IN_PROGRESS)

    # PATCH: snapshot which aging report is active RIGHT NOW, so Analysis
    # History can later show "the aging report this run actually matched
    # against" even after a newer one has since replaced it as active (see
    # AnalysisRun.aging_source_file_id's comment in db/models.py). Read-only
    # lookup — does not touch aging_store or the in-memory AgingMap.
    active_aging = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "aging_report", SourceFile.archived.is_(False))
        .order_by(SourceFile.uploaded_at.desc())
        .first()
    )

    run = AnalysisRun(
        status=RunStatus.RUNNING,
        started_at=dt.datetime.utcnow(),
        selected_files=selected_files,
        triggered_by=user.email,
        triggered_by_user_id=user.id,
        aging_source_file_id=active_aging.id if active_aging else None,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    log_activity(db, user, action="run.start", entity_type="AnalysisRun",
                 entity_id=run.run_id, ip_address=_client_ip(request),
                 metadata={"selected_files": selected_files})
    db.commit()

    run_analysis_task.defer(run_id=run.run_id, selected_files=selected_files)
    return {"run_id": run.run_id, "status": "running"}


@router.get("/status")
def get_status(db: Session = Depends(get_db), user: User = Depends(require_permission("run:monitor"))):
    run = db.query(AnalysisRun).order_by(desc(AnalysisRun.run_id)).first()
    if not run:
        return {"status": "idle", "message": "", "progress_current": 0}
    return {
        "status": run.status.value if hasattr(run.status, "value") else run.status,
        "message": run.error_message or "",
        "progress_current": 0,
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
    }


@router.post("/reset")
def reset_run(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_permission("run:start"))):
    run = db.query(AnalysisRun).filter(AnalysisRun.status == RunStatus.RUNNING).first()
    if run:
        run.status = RunStatus.IDLE
        log_activity(db, user, action="run.reset", entity_type="AnalysisRun",
                     entity_id=run.run_id, ip_address=_client_ip(request))
        db.commit()
    return {"reset": True}


@router.get("/history")
def get_run_history(
    page: int = 1, page_size: int = 50,
    date_from: str | None = None, date_to: str | None = None,
    bank_name: str | None = None, business_unit: str | None = None,
    triggered_by: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    q = db.query(AnalysisRun)
    if date_from:
        # Parse to a real datetime (Postgres rejects timestamp >= varchar) and,
        # for date_to, push to end-of-day so a bare "<= YYYY-MM-DD" doesn't drop
        # every run after midnight — same helper every other bff route uses.
        q = q.filter(AnalysisRun.started_at >= parse_date_from(date_from))
    if date_to:
        q = q.filter(AnalysisRun.started_at <= parse_date_to(date_to))
    if status:
        # Validate against the real enum rather than filtering on a raw
        # string -- a typo'd/stale status value (e.g. "complete" instead
        # of "completed") would otherwise just silently match zero rows,
        # which is a confusing, hard-to-diagnose "no data" bug for the
        # caller rather than a clear error.
        try:
            status_enum = RunStatus(status)
        except ValueError:
            raise AppError(ErrorCode.VALIDATION_FAILED, detail=f"unknown run status '{status}'")
        q = q.filter(AnalysisRun.status == status_enum)
    if triggered_by:
        # "User" filter for the Analysis History page — who STARTED the
        # run (a run-level concept), as distinct from the Home dashboard's
        # user filter (who approved/rejected individual rows within a run,
        # via RowStatusHistory.triggered_by — a row-level concept). Both
        # happen to be named "triggered_by" in their respective tables but
        # answer different questions.
        q = q.filter(AnalysisRun.triggered_by == triggered_by)
    if bank_name or business_unit:
        # AnalysisRun itself has no bank/BU column (a run can span multiple
        # files/banks) — filter to runs that have AT LEAST ONE line item
        # matching, via the same LineItem table everything else filters on.
        line_item_q = db.query(LineItem.run_id)
        if bank_name:
            line_item_q = line_item_q.filter(LineItem.bank_name == bank_name)
        if business_unit:
            line_item_q = line_item_q.filter(LineItem.business_unit == business_unit)
        q = q.filter(AnalysisRun.run_id.in_(line_item_q.distinct().subquery()))
    total = q.count()
    rows = q.order_by(desc(AnalysisRun.run_id)).offset((page - 1) * page_size).limit(page_size).all()
    data = [compute_run_summary_row(db, r) for r in rows]
    return {"data": data, "total": total, "page": page, "page_size": page_size}


@router.get("/history/filter-options")
def get_run_history_filter_options(db: Session = Depends(get_db),
                                    user: User = Depends(require_permission("run:view"))):
    """Distinct 'Started By' values for the Analysis History page's user pill row."""
    users = sorted({
        v for (v,) in db.query(AnalysisRun.triggered_by).distinct() if v
    })
    return {"users": users}


@router.get("/file-preview/{filename}")
def get_file_preview(filename: str, bucket: str = "active", max_rows: int = 200,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_permission("run:monitor"))):
    return preview_bank_file(db, filename, max_rows)