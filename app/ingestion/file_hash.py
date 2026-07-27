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


def check_duplicate_file(db: Session, file_hash: str, current_user_id: int | None = None) -> dict | None:
    """
    Returns a duplicate-info dict if this exact file was already uploaded,
    else None. Does not mutate anything — safe to call speculatively.

    `current_user_id` (optional) is the person doing THIS upload. It's used
    only to set `owned_by_current_user` in the result, so the frontend banner
    can tell "you already uploaded this" (it's in YOUR list → "select it and
    Start") apart from "someone else already uploaded this" (per-user gating
    means it is NOT in your list → purely informational).

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

    source = db.query(SourceFile).get(existing.source_file_id)

    # Whose Account Statements list is this file actually in RIGHT NOW? That's
    # SourceFile.uploaded_by_user_id (what GET /files filters on) — the current
    # owner, which can differ from StatementFileHash.uploaded_by (the ORIGINAL
    # uploader of these bytes) after a restore/retry transfers ownership. Use
    # the current owner for both the displayed name and the ownership flag so
    # the banner matches what the user can actually see.
    owner_id = (
        source.uploaded_by_user_id
        if source is not None and source.uploaded_by_user_id is not None
        else existing.uploaded_by
    )
    uploader = db.query(User).get(owner_id) if owner_id else None

    # Best-effort: find a run that included this source file, for the "view
    # existing run" link. selected_files stores filenames (JSON list), so we
    # match on the SourceFile's filename rather than a direct FK — matches
    # the existing AnalysisRun.selected_files shape (see db/models.py).
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
        "owned_by_current_user": bool(current_user_id is not None and owner_id == current_user_id),
        "uploaded_at": existing.uploaded_at.isoformat() if existing.uploaded_at else None,
        "existing_source_file_id": existing.source_file_id,
        "existing_file_archived": bool(source.archived) if source else False,
        # PATCH: lets handle_statement_upload_v2() distinguish "this exact
        # file was already successfully ingested" from "this exact file was
        # uploaded before but ingestion never succeeded" (most commonly:
        # no bank config existed yet). Without this, a file that failed
        # only because a config was missing became a permanently-dead
        # hash — re-uploading the identical bytes always hit this duplicate
        # branch and was rejected outright, even after the user went and
        # created the config that would let it succeed this time.
        "existing_ingest_status": source.ingest_status if source else None,
        "existing_ingest_error": source.ingest_error if source else None,
        "existing_run_id": prior_run.run_id if prior_run else None,
        # Open the RUN detail view. The history page restores a run from the
        # ?run_id= query param (see analysis-history/page.tsx); the /row/<id>
        # route expects a LINE-ITEM id, not a run id, so the old link sent the
        # user to the wrong row (run_id reinterpreted as record id).
        "history_link": (
            f"/analysis-history?run_id={prior_run.run_id}" if prior_run else "/analysis-history"
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