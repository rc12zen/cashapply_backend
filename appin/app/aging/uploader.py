"""
app.aging.uploader
===================
Receives the aging report file upload, persists to storage, creates
the SourceFile DB record, and triggers a DB reload via the parser.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import SourceFile
from ..storage.client import get_storage_client

AGING_BUCKET = "aging-reports"


def handle_aging_upload(db: Session, filename: str, data: bytes) -> dict:
    """
    Save the aging file to storage and create a SourceFile record.
    Does NOT load into aging_invoices — call parser.load_aging_into_db() separately
    (or via /api/config/refresh-aging) to keep upload + parse decoupled.
    """
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
