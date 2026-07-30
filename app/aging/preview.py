"""
app.aging.preview
==================
Returns the first N rows of an aging file for the config screen preview
table, and the raw bytes for download.

PATCH: both entry points now take an optional `source_file_id`. Passing one
reads that exact historical SourceFile row (e.g. "the aging report a past
run actually matched against" — see AnalysisRun.aging_source_file_id).
Omitting it keeps the original behaviour: whichever SourceFile is currently
archived=False (the active one). Neither path ever mutates archived state —
that only happens via config_routes.py's /aging-select, which is a
deliberate user action, not a side-effect of previewing/downloading.
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


def _resolve_aging_file(db: Session, source_file_id: int | None) -> SourceFile | None:
    """Either the specific aging SourceFile requested, or the active one."""
    if source_file_id is None:
        return _latest_aging_file(db)
    return (
        db.query(SourceFile)
        .filter(SourceFile.kind == "aging_report", SourceFile.id == source_file_id)
        .first()
    )


def load_active_aging_file(
    db: Session, source_file_id: int | None = None
) -> tuple[str, bytes] | None:
    """(filename, raw bytes) of the requested aging file, or the active one
    if source_file_id is None. Name kept for backward compatibility with
    existing call sites that always want "active"."""
    target = _resolve_aging_file(db, source_file_id)
    if not target:
        return None
    storage = get_storage_client()
    return target.filename, storage.read(AGING_BUCKET, target.storage_key)


def preview_aging_file(
    db: Session, max_rows: int = 200, source_file_id: int | None = None
) -> dict:
    storage = get_storage_client()
    target = _resolve_aging_file(db, source_file_id)
    if not target:
        return {"filename": None, "total_rows": 0, "columns": [], "rows": []}

    local_path = storage.local_path_for_read(AGING_BUCKET, target.storage_key)
    cfg = _load_columns_config()["DEFAULT"]
    df = pd.read_excel(local_path, sheet_name=cfg["sheet_name"], header=cfg["header_row"])
    df.columns = [str(c).strip() for c in df.columns]
    df = df.head(max_rows).fillna("")

    return {
        "filename": target.filename,
        "total_rows": len(df),
        "columns": list(df.columns),
        "rows": df.values.tolist(),
    }