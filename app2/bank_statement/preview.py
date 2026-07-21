"""
app.bank_statement.preview
===========================
Returns the first N rows of an uploaded bank statement for the UI preview table.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import SourceFile
from ..storage.client import get_storage_client
from .detector import detect_config

STATEMENT_BUCKET = "bank-statements"


def preview_bank_file(db: Session, filename: str, max_rows: int = 200) -> dict:
    record = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement", SourceFile.filename == filename
    ).first()
    if not record:
        return {"filename": filename, "total_rows": 0, "columns": [], "rows": []}

    storage = get_storage_client()
    local_path = storage.local_path_for_read(STATEMENT_BUCKET, record.storage_key)
    detection = detect_config(local_path)
    if not detection.success:
        return {"filename": filename, "total_rows": 0, "columns": [], "rows": [],
                "warning": "Could not detect bank format for preview."}

    cfg = detection.config
    from .extractor import ExtractorFactory
    df = ExtractorFactory.extract(local_path, cfg["source"])
    df = df.head(max_rows).fillna("")

    return {
        "filename": filename,
        "total_rows": len(df),
        "columns": list(df.columns),
        "rows": df.values.tolist(),
    }
