"""
app.ingestion.ingest_service
==============================
Implements the split-ingestion-from-analysis flow. See design doc §0 and §4.

handle_statement_upload_v2():
    Synchronous hot path — hash check, storage save, SourceFile creation,
    defers the background parse/dedupe job. Returns immediately (~ms).

ingest_and_parse():
    Background job body (deferred via procrastinate — see tasks/ingestion_tasks.py).
    Parses the file, computes row hashes, bulk-inserts new rows into
    StatementTransactionRow with ON CONFLICT DO NOTHING, updates the
    SourceFile's ingest_status so the frontend's poll loop can flip from
    "Processing..." to "You can now start Analysis."
"""
from __future__ import annotations

import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import BankAccount, OrganizationUnit, SourceFile, StatementTransactionRow, User
from ..storage.client import get_storage_client
from ..bank_statement.detector import detect_config, list_matching_configs
from ..bank_statement.account_locator import match_key, last4
from ..bank_statement.parser import parse_credit_rows
from ..audit.service import log_activity
from .file_hash import check_duplicate_file, compute_file_hash, record_file_hash
from .row_hash import compute_row_hash

logger = logging.getLogger("cashapply.ingestion")

STATEMENT_BUCKET = "bank-statements"


class OUNotMappedError(Exception):
    """
    Raised when a bank account was successfully recognized (its statement
    format matched a registered config) but no Organizational Unit mapping
    could be resolved for it. bank_accounts.ou_id is NOT NULL, so this MUST
    be caught before it reaches the DB — every onboarded account needs an
    Organization Unit set via the Config Builder wizard (OU + Business Unit
    are required there) or it will hit this wall the first time a statement
    for it is ingested.
    """
    def __init__(self, account_number: str | None, bank_name: str | None):
        self.account_number = account_number
        self.bank_name = bank_name
        super().__init__(
            f"This account ({account_number}, {bank_name or 'bank name unknown'}) was "
            f"recognized, but has no Organization Unit set up for it yet. "
            f"Go to the Config tab and set its Organization Unit and Business Unit, "
            f"then re-upload or reprocess this statement."
        )


def _incomplete_accounts_message(detection) -> str:
    """User-facing reason a fully-recognised file is still refused.

    Only reachable when the statement's rows span several accounts (a per-row
    account COLUMN) and some of them have no config — those rows would otherwise
    be attributed to whichever account won first-fit in the detector. Names the
    exact accounts so the fix is a Config Builder round-trip, not a guess.
    """
    parts = []
    missing = list(getattr(detection, "unregistered_accounts", None) or [])
    configured = list(getattr(detection, "matched_accounts", None) or [])
    if missing:
        shown = ", ".join(missing[:5])
        more = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
        total = len(missing) + len(configured)
        # Lead with what's ALREADY fine, so "reconfigure this statement" reads as
        # "add the one missing account" rather than "redo the whole thing".
        head = (
            f"{len(configured)} of the {total} accounts in this statement are already configured. "
            if configured else ""
        )
        parts.append(
            f"{head}{len(missing)} still need{'s' if len(missing) == 1 else ''} configuring: "
            f"{shown}{more}. Reconfigure this statement to add "
            f"{'it' if len(missing) == 1 else 'them'} — the existing recipe is reused, you only "
            f"set each new account's Organization Unit."
        )
    return " ".join(parts) or (
        "This statement contains accounts that are not configured yet — add them on the Config tab."
    )


def _get_or_create_bank_account(db: Session, account_number: str | None, bank_name: str | None,
                                  ou_number: str | None, currency: str | None) -> BankAccount | None:
    if not account_number:
        return None
    # IDENTITY IS THE ACCOUNT NUMBER, LEADING ZEROS IGNORED -- match_key, the same
    # rule _account_for() below and bff/config_builder_routes.py's own
    # _get_or_create_bank_account() use. This was an EXACT match on
    # account_number AND bank_name, which made it the SECOND place in the system
    # with a different idea of what "the same account" means, and it silently
    # forked accounts:
    #
    # A Standard Chartered CSV writes its account as "188603500" while the same
    # account is registered as "00188603500" (Oracle's form). Called from the
    # file-level site below, the exact match missed, so a second BankAccount was
    # created and stamped onto SourceFile.bank_account_id -- while _account_for(),
    # which already used match_key, correctly filed every parsed ROW against the
    # original. File pointed at the new empty account, rows sat on the old one,
    # and /run/start refused with "no pending rows" because it counts rows by the
    # FILE's account.
    #
    # bank_name is deliberately not part of the match: it is free text a person
    # types (this database holds "CITI Bank", "CITI BANK" and "CITI7005" for one
    # bank), and the old condition compared it against "" here while the INSERT
    # below wrote "UNKNOWN" -- so an account this function created could never be
    # found again by this function.
    key = match_key(account_number)
    existing = next(
        (a for a in db.query(BankAccount)
                      .filter(BankAccount.account_last4 == last4(account_number)).all()
         if match_key(a.account_number) == key),
        None,
    )
    if existing:
        # PATCH: previously returned as-is, forever — ou_id was frozen at
        # whatever it was the FIRST time this account was ever ingested,
        # even if bank_ou_mapping.json got a proper entry added afterward
        # (e.g. this account originally got the auto-provisioned "stub" OU
        # below because no mapping existed yet). Fixing the JSON only ever
        # helped brand-new accounts — anything already in the DB stayed on
        # the stale/stub OU no matter what you fixed. Self-heal it here:
        # only ever upgrades to a real, resolvable OU; never clears one out
        # or overwrites with something worse.
        if ou_number:
            real_ou = db.query(OrganizationUnit).filter(OrganizationUnit.ou_number == ou_number).first()
            if real_ou and existing.ou_id != real_ou.id:
                existing.ou_id = real_ou.id
                db.flush()
        return existing

    ou = None
    if ou_number:
        ou = db.query(OrganizationUnit).filter(OrganizationUnit.ou_number == ou_number).first()
        if ou is None:
            # Auto-provision a stub OU so ingestion never blocks on the
            # organization_units table being pre-seeded. functional_currency
            # defaults to the statement currency as a best-effort placeholder;
            # correct it via the Config screen — this is a convenience
            # fallback, not the source of truth (rule_engine/fx_service.py
            # still reads ou_functional_currency.json for FX resolution).
            ou = OrganizationUnit(
                ou_number=ou_number, ou_name=ou_number,
                functional_currency=(currency or "USD").upper(),
            )
            db.add(ou)
            db.flush()
    else:
        # PATCH: this used to fall through to `BankAccount(ou_id=None, ...)`
        # below, which then hit a raw NotNullViolation on the DB insert —
        # ou_id is NOT NULL, so this can never actually succeed. Fail
        # clearly and catchably here instead, so the caller can set a
        # meaningful ingest_error rather than the row silently retrying
        # forever against the same unresolvable OU gap.
        raise OUNotMappedError(account_number, bank_name)

    account = BankAccount(
        ou_id=ou.id if ou else None,
        account_number=account_number,
        # account_last4 was never set here, only by the Config Builder's own
        # creator. That mattered: the lookup above and _account_for()'s
        # prefilter both narrow on account_last4, so an account created here was
        # invisible to BOTH of them and got duplicated again on the next
        # statement. NOT NULL isn't enforced on the column, so nothing caught it.
        account_last4=last4(account_number),
        bank_name=bank_name or "UNKNOWN",
        currency=currency,
    )
    db.add(account)
    db.flush()
    return account


def handle_statement_upload_v2(db: Session, filename: str, data: bytes, uploaded_by: User | None) -> dict:
    """
    Replaces bank_statement/uploader.py's handle_statement_upload() as the
    route-level entrypoint. The original function is left untouched for any
    other caller; routes should call this one going forward.
    """
    file_hash = compute_file_hash(data)

    dup = check_duplicate_file(db, file_hash, uploaded_by.id if uploaded_by else None)
    if dup is not None:
        if dup["existing_file_archived"]:
            # PATCH: was an unconditional block. If the matched file had
            # been archived (removed via ✕), it could never come back —
            # GET /api/run/files filters archived=False, so re-uploading
            # the identical bytes was a dead end: blocked as a duplicate,
            # yet invisible in the Account Statements list either way.
            # Un-archive it instead and let the caller treat this as a
            # restore, not a rejection.
            source = db.query(SourceFile).get(dup["existing_source_file_id"])
            source.archived = False
            # Per-user gating: the file re-enters the Account Statements list
            # for whoever just uploaded it, so ownership follows the current
            # uploader — otherwise GET /files (scoped by uploaded_by_user_id)
            # would restore it but keep it invisible to the person who acted.
            if uploaded_by is not None:
                source.uploaded_by_user_id = uploaded_by.id
            log_activity(db, uploaded_by, action="statement.restore",
                         entity_type="SourceFile", entity_id=source.id,
                         metadata={"file_hash": file_hash, "filename": filename})
            db.commit()
            db.refresh(source)
            return {
                "duplicate": False,
                "restored": True,
                "source_file_id": source.id,
                "filename": source.filename,
                "ingest_status": source.ingest_status,
                "message": f'"{filename}" was previously removed but is now restored to your Account Statements list.',
            }

        # PATCH: the file is still active (never archived), but its ONLY
        # prior ingestion attempt failed — most commonly because no bank
        # config existed yet for this account/format at the time. The user
        # may have since created one via the Config Builder wizard.
        #
        # Without this branch, re-uploading the identical bytes always fell
        # through to the flat "duplicate, blocked" response below — a dead
        # end, since the same file_hash can never take the normal "new
        # upload" path again, and creating a config alone (builder_save())
        # never touches this SourceFile row or re-triggers ingestion.
        #
        # Fix: reuse the existing row, reset it back to "processing", and
        # return duplicate=False so the route defers ingest_statement_task
        # again. detect_config() runs fresh inside ingest_and_parse(), so if
        # a config now exists, this succeeds and rows are finally parsed —
        # no new SourceFile row or file_hash entry needed, since the bytes
        # (and the storage key they were saved under) haven't changed.
        if dup.get("existing_ingest_status") in ("error", "unrecognized"):
            source = db.query(SourceFile).get(dup["existing_source_file_id"])
            logger.info(
                "Re-upload of previously %s file: source_file_id=%s filename=%r -> retrying",
                dup.get("existing_ingest_status"), source.id, filename,
            )
            source.ingest_status = "processing"
            source.ingest_error = None
            # Per-user gating: ownership follows the current uploader so the
            # retried file shows up for the person who re-uploaded it (see the
            # restore branch above for the same rationale).
            if uploaded_by is not None:
                source.uploaded_by_user_id = uploaded_by.id
            log_activity(db, uploaded_by, action="statement.upload_retry_after_error",
                         entity_type="SourceFile", entity_id=source.id,
                         metadata={"file_hash": file_hash, "filename": filename,
                                   "prior_error": dup.get("existing_ingest_error")})
            db.commit()
            db.refresh(source)
            return {
                "duplicate": False,
                "retried": True,
                "source_file_id": source.id,
                "filename": source.filename,
                "ingest_status": source.ingest_status,
                "message": f'"{filename}" previously failed to process (no config existed yet) — retrying now.',
            }

        log_activity(db, uploaded_by, action="statement.upload_rejected_duplicate",
                     entity_type="SourceFile", entity_id=dup["existing_source_file_id"],
                     status="failure", metadata={"file_hash": file_hash, "filename": filename})
        db.commit()
        return dup

    storage = get_storage_client()
    key = filename
    storage.save(STATEMENT_BUCKET, key, data)

    local_path = storage.local_path_for_read(STATEMENT_BUCKET, key)
    detection = detect_config(local_path)
    is_ambiguous = detection.reason == "AMBIGUOUS" if hasattr(detection, "reason") else False
    candidates = list_matching_configs(local_path) if (is_ambiguous or not detection.success) else []

    # BUGFIX: this used to always set ingest_status="processing" here, even
    # though detect_config() above has ALREADY told us, synchronously,
    # whether a config exists. The background job (ingest_and_parse) then
    # re-ran detect_config(), found the same "no match", and stamped
    # ingest_status="error" — the exact same status used for genuine
    # failures (OU not mapped, unexpected exceptions). That conflation is
    # what made a brand-new/not-yet-configured statement show a red "Error"
    # badge + "Reconfigure" button in the UI, identical to a real failure.
    # "No config exists yet" is an expected, everyday state — not an error —
    # so it gets its own status, set immediately (no need to even defer the
    # background job when we already know it can't proceed).
    ingest_status = "unrecognized" if (is_ambiguous or not detection.success) else "processing"
    logger.info(
        "Upload detection: filename=%r success=%s ambiguous=%s config_key=%r -> ingest_status=%r",
        filename, detection.success, is_ambiguous, detection.config_key, ingest_status,
    )

    record = SourceFile(
        kind="bank_statement",
        filename=filename,
        storage_key=key,
        bank_config_key=detection.config_key,
        ou_number=(detection.ou_info or {}).get("ou_number"),
        business_unit=(detection.ou_info or {}).get("ou"),
        uploaded_by_user_id=uploaded_by.id if uploaded_by else None,
        file_hash=file_hash,
        ingest_status=ingest_status,
    )
    db.add(record)
    db.flush()  # need record.id before inserting the hash row

    try:
        record_file_hash(db, file_hash, record.id, uploaded_by.id if uploaded_by else None)
    except IntegrityError:
        # Race: another concurrent upload of the same bytes committed first
        # between our check and our insert. UNIQUE(file_hash) is the real
        # guard (see design doc §4a) — fold this into the same duplicate
        # response instead of a 500.
        db.rollback()
        dup = check_duplicate_file(db, file_hash, uploaded_by.id if uploaded_by else None)
        return dup or {"duplicate": True, "history_link": "/analysis-history"}

    log_activity(db, uploaded_by, action="statement.upload",
                 entity_type="SourceFile", entity_id=record.id,
                 metadata={"filename": filename, "file_hash": file_hash})
    db.commit()
    db.refresh(record)

    warning = None
    if is_ambiguous:
        warning = "Multiple configs match this file — choose the correct one."
    elif detection.reason == "INCOMPLETE_ACCOUNTS":
        # Recognised, but not every account in it is configured — a distinct
        # (and actionable) state from "we don't know this format at all".
        warning = _incomplete_accounts_message(detection)
    elif not detection.success:
        warning = "Bank format not auto-detected — manual config required."

    return {
        "duplicate": False,
        "filename": filename,
        "source_file_id": record.id,
        "storage_key": key,
        "detected_bank_config": detection.config_key,
        "detection_method": detection.method_detail,
        "detection_step": detection.step_used,
        "ou_info": detection.ou_info,
        "ambiguous": is_ambiguous,
        "candidates": candidates,
        "warning": warning,
        "ingest_status": ingest_status,
    }


def ingest_and_parse(db: Session, source_file_id: int) -> dict:
    """
    Background job body — parses the file and bulk-dedupes its rows into
    StatementTransactionRow. Idempotent: safe to re-run for the same
    source_file_id (ON CONFLICT DO NOTHING means re-running just finds 0 new
    rows the second time).
    """
    record = db.query(SourceFile).get(source_file_id)
    if record is None:
        return {"error": "source_file_not_found"}

    try:
        storage = get_storage_client()
        local_path = storage.local_path_for_read(STATEMENT_BUCKET, record.storage_key)
        logger.info(
            "[ingest] source_file_id=%s filename=%r -- running detect_config() (see "
            "cashapply.ingestion.detector logs above/below for the full candidate-by-"
            "candidate matching trace)",
            source_file_id, record.filename,
        )
        detection = detect_config(local_path)
        if not detection.success:
            # Same distinction as handle_statement_upload_v2 above: no config
            # matching this file is a normal, expected state (needs
            # configuring), not a processing failure — keep it out of
            # ingest_status="error" so the UI doesn't show it as one.
            logger.info(
                "Background detection still no match: source_file_id=%s filename=%r -> ingest_status='unrecognized'",
                source_file_id, record.filename,
            )
            record.ingest_status = "unrecognized"
            if detection.reason == "INCOMPLETE_ACCOUNTS":
                # The format IS recognised — we're refusing because some account
                # in the file has no config, which would otherwise get its rows
                # silently attributed to the first-fit account.
                record.ingest_error = _incomplete_accounts_message(detection)
                # Persist the matched config so the UI can tell "recognised, but
                # some accounts are missing" (offer Reconfigure + the detail) from
                # "we don't know this format at all" (offer Configure). Without
                # this, a file first uploaded before ANY config existed keeps
                # bank_config_key=None through the retry and looks unknown.
                record.bank_config_key = detection.config_key or record.bank_config_key
            else:
                record.ingest_error = "No matching account configuration found for this statement — configure this account to enable ingestion."
            db.commit()
            return {"error": record.ingest_error}

        # Persist the config this file actually matched. Only ever set at UPLOAD
        # time before, so a statement first uploaded when its accounts weren't
        # configured yet (config_key=None) kept that None through the successful
        # retry — and the UI, which reads bank_config_key, went on showing it as
        # "Unknown" even though it had parsed cleanly.
        if detection.config_key:
            record.bank_config_key = detection.config_key

        raw_rows = parse_credit_rows(local_path, detection, record.filename)

        # PATCH: was `ou_number=record.ou_number` — a snapshot taken once at
        # the ORIGINAL upload attempt and never refreshed. If bank_ou_mapping
        # .json (or the account's config) was fixed/added after that first
        # attempt, every retry kept reusing the stale value (often still
        # None) instead of picking up the fix, because detect_config() above
        # already re-resolves it fresh on every call but nothing was reading
        # that fresh value back out. Prefer it; fall back to the stored
        # value only if this call's own detection didn't produce one.
        #
        # PATCH 2 (F2 — cross-account OU contamination): detection.ou_info
        # describes the FIRST-FIT account only (detector.py picks matches[0]
        # when several accounts share one layout). Pairing it with row 0's
        # account number below meant a multi-account file could hand account
        # B's BankAccount row account A's OU — and _get_or_create_bank_account's
        # self-heal branch then OVERWRITES a correct ou_id with the wrong one,
        # a persistent DB mutation, not just a bad read. The parser already
        # resolves each row's OU from that row's OWN account (parser.py's
        # step 5f) and nothing was reading it back; prefer it. Reliable now
        # that detect_config refuses files containing unconfigured accounts,
        # so row 0's account is always registered and therefore resolvable.
        resolved_ou_number = (
            (raw_rows[0].ou_number if raw_rows else None)
            or (detection.ou_info or {}).get("ou_number")
            or record.ou_number
        )

        bank_account = _get_or_create_bank_account(
            db,
            account_number=(raw_rows[0].account_number if raw_rows else None),
            bank_name=(raw_rows[0].bank_name if raw_rows else None),
            ou_number=resolved_ou_number,
            currency=(raw_rows[0].currency if raw_rows else None),
        )
        if bank_account:
            # The file-level account stays as the PRIMARY (first row's) account
            # for back-compat; per-row attribution below is what's authoritative.
            record.bank_account_id = bank_account.id

        # PER-ROW ACCOUNT ATTRIBUTION (was: every row stamped with row 0's
        # account). A file whose account_locator is a COLUMN legitimately holds
        # several accounts, and stamping them all with the first one meant:
        #   - the run/Confirm dialog showed one account for the whole file
        #   - every row entered the rule engine under that account's OU, so R14
        #     (OU mismatch) judged other accounts' rows against the wrong OU
        #   - row_hash was scoped to one account, so two genuinely different
        #     rows on different accounts collided and one was silently dropped
        # detect_config refuses a file containing unconfigured accounts, so every
        # account here resolves; anything that somehow doesn't falls back to the
        # primary account rather than being dropped.
        account_cache: dict[str, BankAccount | None] = {}

        def _account_for(row) -> BankAccount | None:
            key = match_key(row.account_number)
            if not key:
                return bank_account
            if key not in account_cache:
                found = next(
                    (a for a in db.query(BankAccount).filter(
                        BankAccount.account_last4 == (row.account_number or "")[-4:]).all()
                     if match_key(a.account_number) == key),
                    None,
                )
                if found is None:
                    found = _get_or_create_bank_account(
                        db,
                        account_number=row.account_number,
                        bank_name=row.bank_name,
                        ou_number=row.ou_number or resolved_ou_number,
                        currency=row.currency,
                    )
                account_cache[key] = found or bank_account
            return account_cache[key]

        payload = []
        for r in raw_rows:
            row_account = _account_for(r)
            row_hash = compute_row_hash(
                row_account.id if row_account else None,
                r.statement_date, r.credit_amount, r.currency, r.bank_reference, r.narrative,
            )
            payload.append({
                "source_file_id": source_file_id,
                "bank_account_id": row_account.id if row_account else None,
                "row_hash": row_hash,
                "statement_date": r.statement_date,
                "credit_amount": r.credit_amount,
                "currency": r.currency,
                "narrative": r.narrative,
                "bank_reference": r.bank_reference,
                "raw_row_json": {
                    "bank_name": r.bank_name, "account_number": r.account_number,
                    "business_unit": r.business_unit, "ou_number": r.ou_number,
                    "statement_date": r.statement_date.isoformat() if r.statement_date else None,
                    "narrative": r.narrative, "credit_amount": float(r.credit_amount or 0),
                    "currency": r.currency, "bank_reference": r.bank_reference,
                },
            })

        new_count = 0
        if payload:
            stmt = pg_insert(StatementTransactionRow).values(payload)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["bank_account_id", "row_hash"]
            ).returning(StatementTransactionRow.id)
            result = db.execute(stmt)
            new_count = len(result.fetchall())

        record.new_row_count = new_count
        record.duplicate_row_count = len(payload) - new_count
        record.ingest_status = "ready"
        record.ingest_error = None
        logger.info(
            "Ingestion ready: source_file_id=%s filename=%r total_rows=%d new_rows=%d duplicate_rows=%d",
            source_file_id, record.filename, len(payload), new_count, len(payload) - new_count,
        )

        log_activity(db, None, action="statement.ingest_complete",
                     entity_type="SourceFile", entity_id=record.id,
                     metadata={"total_rows": len(payload), "new_rows": new_count,
                               "duplicate_rows": len(payload) - new_count})
        db.commit()
        return {"total_rows": len(payload), "new_rows": new_count, "duplicate_rows": len(payload) - new_count}

    except OUNotMappedError as exc:
        # Permanent (data-completeness), not transient — retrying achieves
        # nothing until bank_ou_mapping.json is fixed, so don't re-raise
        # (which is what triggers procrastinate's automatic retry). Record
        # the clean, actionable message and stop here, same shape as the
        # "Bank format not auto-detected" branch above.
        db.rollback()
        record = db.query(SourceFile).get(source_file_id)
        if record:
            record.ingest_status = "error"
            record.ingest_error = str(exc)
            db.commit()
        logger.warning("Ingestion stopped (OU not mapped): source_file_id=%s — %s", source_file_id, exc)
        return {"error": str(exc)}

    except Exception as exc:
        # PATCH: this used to store str(exc) directly into ingest_error —
        # for a raw SQLAlchemy/DB error, str(exc) IS the full multi-line
        # dump (the INSERT statement, every bound parameter, and the
        # sqlalche.me troubleshooting link), which then got surfaced
        # straight into the Account Statements card's tooltip. Log the real
        # exception (with traceback) server-side where a developer can
        # actually use it, and keep the user-facing message short and
        # non-technical — this is the one case where we genuinely don't
        # know what went wrong, so we say that plainly rather than dumping
        # a stack trace on someone who can't act on it.
        logger.exception("Ingestion failed unexpectedly: source_file_id=%s", source_file_id)
        db.rollback()
        record = db.query(SourceFile).get(source_file_id)
        if record:
            record.ingest_status = "error"
            record.ingest_error = (
                "Something went wrong while processing this statement (not a recognized "
                "format or OU issue). Check the server logs for source_file_id="
                f"{source_file_id}, or try re-uploading the file."
            )
            db.commit()
        raise