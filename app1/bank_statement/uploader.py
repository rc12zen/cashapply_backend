"""
app.bank_statement.uploader
============================
Receives a bank statement file upload, persists to storage, runs bank
detection to pre-populate metadata, and creates the SourceFile DB record.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import SourceFile
from ..storage.client import get_storage_client
from .detector import detect_config, list_matching_configs

STATEMENT_BUCKET = "bank-statements"


def handle_statement_upload(db: Session, filename: str, data: bytes) -> dict:
    """
    Save the statement file and create a SourceFile record with detected metadata.
    Returns detection summary for the API response.
    """
    storage = get_storage_client()
    key = filename
    storage.save(STATEMENT_BUCKET, key, data)

    local_path = storage.local_path_for_read(STATEMENT_BUCKET, key)
    detection = detect_config(local_path)

    is_ambiguous = detection.reason == "AMBIGUOUS"
    candidates = list_matching_configs(local_path) if (is_ambiguous or not detection.success) else []

    record = SourceFile(
        kind="bank_statement",
        filename=filename,
        storage_key=key,
        bank_config_key=detection.config_key,
        ou_number=(detection.ou_info or {}).get("ou_number"),
        business_unit=(detection.ou_info or {}).get("ou"),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    if is_ambiguous:
        warning = "Multiple configs match this file — choose the correct one."
    elif not detection.success:
        warning = "Bank format not auto-detected — manual config required."
    else:
        warning = None

    return {
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
    }


def delete_statement(db: Session, filename: str) -> dict:
    storage = get_storage_client()
    record = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement", SourceFile.filename == filename
    ).first()
    if record:
        storage.delete(STATEMENT_BUCKET, record.storage_key)
        db.delete(record)
        db.commit()
    return {"deleted": filename}
