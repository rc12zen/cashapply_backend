"""
app.ingestion.file_hash
=========================
Exact-duplicate-file detection. See design doc §2.1.

Hash is computed BEFORE the file touches blob storage — reject on the hot
path, don't upload-then-check.
"""
from __future__ import annotations

import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import AnalysisRun, SourceFile, StatementFileHash, User


def compute_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_duplicate_file(db: Session, file_hash: str) -> dict | None:
    """
    Returns a duplicate-info dict if this exact file was already uploaded,
    else None. Does not mutate anything — safe to call speculatively.

    PATCH: now includes `existing_file_archived` — the original version
    never checked this, so re-uploading the exact same bytes as a file
    that had been archived (removed via ✕) was permanently blocked as a
    "duplicate" forever, with no way back — GET /api/run/files filters
    archived=False, so that file could never appear in the Account
    Statements list again, yet the duplicate banner claimed it was
    "still sitting in your list." Callers should treat archived=True as
    "restore it," not "block it" — see handle_statement_upload_v2().
    """
    existing = db.query(StatementFileHash).filter_by(file_hash=file_hash).first()
    if existing is None:
        return None

    uploader = db.query(User).get(existing.uploaded_by) if existing.uploaded_by else None

    # Best-effort: find a run that included this source file, for the "view
    # existing run" link. selected_files stores filenames (JSON list), so we
    # match on the SourceFile's filename rather than a direct FK — matches
    # the existing AnalysisRun.selected_files shape (see db/models.py).
    source = db.query(SourceFile).get(existing.source_file_id)
    prior_run = None
    if source:
        candidates = (
            db.query(AnalysisRun)
            .order_by(AnalysisRun.started_at.desc())
            .all()
        )
        for run in candidates:
            if source.filename in (run.selected_files or []):
                prior_run = run
                break

    return {
        "duplicate": True,
        "uploaded_by": uploader.display_name or uploader.email if uploader else "unknown",
        "uploaded_at": existing.uploaded_at.isoformat() if existing.uploaded_at else None,
        "existing_source_file_id": existing.source_file_id,
        "existing_file_archived": bool(source.archived) if source else False,
        "existing_run_id": prior_run.run_id if prior_run else None,
        "history_link": (
            f"/analysis-history/row/{prior_run.run_id}" if prior_run else "/analysis-history"
        ),
    }


def record_file_hash(db: Session, file_hash: str, source_file_id: int, uploaded_by_user_id: int | None) -> None:
    """
    Insert the hash row. Relies on the UNIQUE(file_hash) constraint as the
    real race guard (see design doc §4a) — the check_duplicate_file() call
    above is a UX optimization, not the correctness boundary.
    """
    try:
        db.add(StatementFileHash(
            file_hash=file_hash,
            source_file_id=source_file_id,
            uploaded_by=uploaded_by_user_id,
        ))
        db.flush()
    except IntegrityError:
        db.rollback()
        raise