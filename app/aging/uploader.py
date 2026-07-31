"""
app.aging.uploader
===================
Receives the aging report file upload, persists to storage, creates
the SourceFile DB record, and triggers a DB reload via the parser.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import SourceFile
from ..storage.client import get_storage_client
from .file_sniff import check_extension_mismatch

AGING_BUCKET = "aging-reports"


def handle_aging_upload(db: Session, filename: str, data: bytes) -> dict:
    """
    Save the aging file to storage and create a SourceFile record.
    Does NOT load into aging_invoices — call parser.load_aging_into_db() separately
    (or via /api/config/refresh-aging) to keep upload + parse decoupled.
    """
    # PATCH: reject up front if the file's actual bytes don't match its
    # extension (e.g. a legacy .xls binary saved with a .xlsx name).
    # Without this, the upload silently "succeeds" — pandas can often
    # still parse it for preview/matching if xlrd happens to be
    # installed — and the mismatch only surfaces much later when someone
    # downloads the raw file and a real Excel client refuses to open it.
    # See aging/file_sniff.py for the exact detection logic.
    mismatch = check_extension_mismatch(filename, data)
    if mismatch:
        raise AppError(ErrorCode.AGING_FORMAT_MISMATCH, detail=mismatch)

    storage = get_storage_client()
    key = filename
    storage.save(AGING_BUCKET, key, data)

    record = SourceFile(
        kind="aging_report",
        filename=filename,
        storage_key=key,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return {"filename": filename, "source_file_id": record.id, "storage_key": key}