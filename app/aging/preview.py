"""
app.aging.preview
==================
Returns the first N rows of the current aging file for the config screen preview table.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from ..db.models import SourceFile
from ..storage.client import get_storage_client
from .parser import _load_columns_config

AGING_BUCKET = "aging-reports"


def _latest_aging_file(db: Session) -> SourceFile | None:
    return (
        db.query(SourceFile)
        .filter(SourceFile.kind == "aging_report", SourceFile.archived.is_(False))
        .order_by(SourceFile.uploaded_at.desc())
        .first()
    )


def load_active_aging_file(db: Session) -> tuple[str, bytes] | None:
    """(filename, raw bytes) of the currently active aging file, or None."""
    latest = _latest_aging_file(db)
    if not latest:
        return None
    storage = get_storage_client()
    return latest.filename, storage.read(AGING_BUCKET, latest.storage_key)


def preview_aging_file(db: Session, max_rows: int = 200) -> dict:
    storage = get_storage_client()
    latest = _latest_aging_file(db)
    if not latest:
        return {"filename": None, "total_rows": 0, "columns": [], "rows": []}

    local_path = storage.local_path_for_read(AGING_BUCKET, latest.storage_key)
    cfg = _load_columns_config()["DEFAULT"]
    df = pd.read_excel(local_path, sheet_name=cfg["sheet_name"], header=cfg["header_row"])
    df.columns = [str(c).strip() for c in df.columns]
    df = df.head(max_rows).fillna("")

    return {
        "filename": latest.filename,
        "total_rows": len(df),
        "columns": list(df.columns),
        "rows": df.values.tolist(),
    }