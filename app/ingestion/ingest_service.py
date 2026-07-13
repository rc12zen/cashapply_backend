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

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import BankAccount, OrganizationUnit, SourceFile, StatementTransactionRow, User
from ..storage.client import get_storage_client
from ..bank_statement.detector import detect_config, list_matching_configs
from ..bank_statement.parser import parse_credit_rows
from ..audit.service import log_activity
from .file_hash import check_duplicate_file, compute_file_hash, record_file_hash
from .row_hash import compute_row_hash

STATEMENT_BUCKET = "bank-statements"


def _get_or_create_bank_account(db: Session, account_number: str | None, bank_name: str | None,
                                  ou_number: str | None, currency: str | None) -> BankAccount | None:
    if not account_number:
        return None
    existing = (
        db.query(BankAccount)
        .filter(BankAccount.account_number == account_number, BankAccount.bank_name == (bank_name or ""))
        .first()
    )
    if existing:
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

    account = BankAccount(
        ou_id=ou.id if ou else None,
        account_number=account_number,
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

    dup = check_duplicate_file(db, file_hash)
    if dup is not None:
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

    record = SourceFile(
        kind="bank_statement",
        filename=filename,
        storage_key=key,
        bank_config_key=detection.config_key,
        ou_number=(detection.ou_info or {}).get("ou_number"),
        business_unit=(detection.ou_info or {}).get("ou"),
        uploaded_by_user_id=uploaded_by.id if uploaded_by else None,
        file_hash=file_hash,
        ingest_status="processing",
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
        dup = check_duplicate_file(db, file_hash)
        return dup or {"duplicate": True, "history_link": "/analysis-history"}

    log_activity(db, uploaded_by, action="statement.upload",
                 entity_type="SourceFile", entity_id=record.id,
                 metadata={"filename": filename, "file_hash": file_hash})
    db.commit()
    db.refresh(record)

    warning = None
    if is_ambiguous:
        warning = "Multiple configs match this file — choose the correct one."
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
        "ingest_status": "processing",
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
        detection = detect_config(local_path)
        if not detection.success:
            record.ingest_status = "error"
            record.ingest_error = "Bank format not auto-detected; cannot parse rows for ingestion."
            db.commit()
            return {"error": record.ingest_error}

        raw_rows = parse_credit_rows(local_path, detection, record.filename)

        bank_account = _get_or_create_bank_account(
            db,
            account_number=(raw_rows[0].account_number if raw_rows else None),
            bank_name=(raw_rows[0].bank_name if raw_rows else None),
            ou_number=record.ou_number,
            currency=(raw_rows[0].currency if raw_rows else None),
        )
        if bank_account:
            record.bank_account_id = bank_account.id

        payload = []
        for r in raw_rows:
            row_hash = compute_row_hash(
                bank_account.id if bank_account else None,
                r.statement_date, r.credit_amount, r.currency, r.bank_reference, r.narrative,
            )
            payload.append({
                "source_file_id": source_file_id,
                "bank_account_id": bank_account.id if bank_account else None,
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

        log_activity(db, None, action="statement.ingest_complete",
                     entity_type="SourceFile", entity_id=record.id,
                     metadata={"total_rows": len(payload), "new_rows": new_count,
                               "duplicate_rows": len(payload) - new_count})
        db.commit()
        return {"total_rows": len(payload), "new_rows": new_count, "duplicate_rows": len(payload) - new_count}

    except Exception as exc:
        db.rollback()
        record = db.query(SourceFile).get(source_file_id)
        if record:
            record.ingest_status = "error"
            record.ingest_error = str(exc)
            db.commit()
        raise
