"""
app.bff.run_routes
===================
/api/run/* — matches lib/api.ts: getFiles, startRun, getStatus, resetRun,
deleteFile, uploadStatement, getRunHistory, getFilePreview.

UPDATED (auth/RBAC/duplicate-detection/audit integration — see
cashapply-platform-hardening-design.md):
  - Every route now requires an authenticated user via require_permission().
  - /upload goes through the new ingestion pipeline (hash-dedup + background
    parse job) instead of the old synchronous full-parse-on-upload path.
  - New GET /files/{source_file_id}/ingest-status for the frontend's
    "Processing..." → "You can now start Analysis" poll loop.
  - /start uses procrastinate (run_analysis_task) instead of a bare thread,
    and checks for an overlapping in-flight run before even attempting the
    advisory lock (see rule_engine/orchestrator.py's _run_analysis_locked).
  - Every mutating action is logged via audit.service.log_activity().
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..db.models import AnalysisRun, LineItem, RunStatus, SourceFile, User
from ..storage.client import get_storage_client
from ..deps import get_db
from ..auth import require_permission
from ..audit.service import log_activity
from ..ingestion.ingest_service import handle_statement_upload_v2
from ..tasks.analysis_tasks import run_analysis_task
from ..bank_statement.preview import preview_bank_file
from .metrics import compute_run_summary_row
from .date_range import parse_date_from, parse_date_to

router = APIRouter()

STATEMENT_BUCKET = "bank-statements"


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/files")
def get_files(db: Session = Depends(get_db), user: User = Depends(require_permission("run:view"))):
    rows = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "bank_statement", SourceFile.archived.is_(False))
        .all()
    )
    storage = get_storage_client()
    out = []
    for r in rows:
        size_mb = 0.0
        try:
            data = storage.read(STATEMENT_BUCKET, r.storage_key)
            size_mb = round(len(data) / (1024 * 1024), 2)
        except Exception:
            pass
        out.append({
            "filename": r.filename,
            "bank_name": r.bank_config_key or "Unknown",
            "size_mb": size_mb,
            "business_unit": r.business_unit or "",
            "ou_number": r.ou_number or "",
            "source_file_id": r.id,
            "ingest_status": r.ingest_status,
            "new_row_count": r.new_row_count,
            "duplicate_row_count": r.duplicate_row_count,
        })
    return {"files": out}


@router.post("/upload")
async def upload_statement(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("statement:upload")),
):
    data = await file.read()
    result = handle_statement_upload_v2(db, file.filename, data, uploaded_by=user)

    if result.get("duplicate"):
        # 200, not an error status — the frontend shows this as an informational
        # banner with the "view existing run" link, not a failed-upload toast.
        return result

    # Defer the background parse/dedupe job (procrastinate — see design doc §5).
    from ..tasks.ingestion_tasks import ingest_statement_task
    ingest_statement_task.defer(source_file_id=result["source_file_id"])

    return result


@router.get("/files/{source_file_id}/ingest-status")
def get_ingest_status(source_file_id: int, db: Session = Depends(get_db),
                       user: User = Depends(require_permission("run:view"))):
    record = db.query(SourceFile).get(source_file_id)
    if not record:
        raise HTTPException(404, "Source file not found")
    return {
        "source_file_id": record.id,
        "filename": record.filename,
        "ingest_status": record.ingest_status,      # "processing" | "ready" | "error"
        "ingest_error": record.ingest_error,
        "new_row_count": record.new_row_count,
        "duplicate_row_count": record.duplicate_row_count,
    }


@router.delete("/files/{filename}")
def delete_file(filename: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("statement:upload"))):
    """
    Removes the file from the 'active' UI list for the next run.
    Sets archived=True on the SourceFile record — the file bytes stay in
    storage so historical runs that referenced this file can still retrieve it.
    Does NOT delete from blob/storage.

    PATCH: archives ALL non-archived rows matching this filename, not just
    the first one found. handle_statement_upload_v2() always inserts a new
    SourceFile row (e.g. re-uploading the same file after the Config
    Builder wizard), so more than one row can share a filename. Using
    .first() left the older/newer duplicate un-archived, which made the
    file reappear in get_files() immediately after being "removed".
    """
    records = db.query(SourceFile).filter(
        SourceFile.kind == "bank_statement",
        SourceFile.filename == filename,
        SourceFile.archived.is_(False),
    ).all()
    for record in records:
        record.archived = True
    log_activity(db, user, action="statement.delete", entity_type="SourceFile",
                 entity_id=filename, ip_address=_client_ip(request),
                 metadata={"rows_archived": len(records)})
    db.commit()
    return {"archived": filename, "rows_archived": len(records)}


@router.post("/start")
def start_run(payload: dict, request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_permission("run:start"))):
    selected_files = payload.get("selected_files", [])
    if not selected_files:
        raise HTTPException(400, "No files selected.")

    # Fast-fail check (UX layer) — the advisory lock in
    # _run_analysis_locked() is the actual correctness guarantee for true
    # concurrent requests; this just avoids the common impatient-double-click
    # case ever reaching it. See design doc §4.
    existing_running = db.query(AnalysisRun).filter(AnalysisRun.status == RunStatus.RUNNING).first()
    if existing_running:
        raise HTTPException(409, "A run is already in progress.")

    run = AnalysisRun(
        status=RunStatus.RUNNING,
        started_at=dt.datetime.utcnow(),
        selected_files=selected_files,
        triggered_by=user.email,
        triggered_by_user_id=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    log_activity(db, user, action="run.start", entity_type="AnalysisRun",
                 entity_id=run.run_id, ip_address=_client_ip(request),
                 metadata={"selected_files": selected_files})
    db.commit()

    run_analysis_task.defer(run_id=run.run_id, selected_files=selected_files)
    return {"run_id": run.run_id, "status": "running"}


@router.get("/status")
def get_status(db: Session = Depends(get_db), user: User = Depends(require_permission("run:view"))):
    run = db.query(AnalysisRun).order_by(desc(AnalysisRun.run_id)).first()
    if not run:
        return {"status": "idle", "message": "", "progress_current": 0}
    return {
        "status": run.status.value if hasattr(run.status, "value") else run.status,
        "message": run.error_message or "",
        "progress_current": 0,
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
    }


@router.post("/reset")
def reset_run(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_permission("run:start"))):
    run = db.query(AnalysisRun).filter(AnalysisRun.status == RunStatus.RUNNING).first()
    if run:
        run.status = RunStatus.IDLE
        log_activity(db, user, action="run.reset", entity_type="AnalysisRun",
                     entity_id=run.run_id, ip_address=_client_ip(request))
        db.commit()
    return {"reset": True}


@router.get("/history")
def get_run_history(
    page: int = 1, page_size: int = 50,
    date_from: str | None = None, date_to: str | None = None,
    bank_name: str | None = None, business_unit: str | None = None,
    triggered_by: str | None = None, status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    q = db.query(AnalysisRun)
    if date_from:
        q = q.filter(AnalysisRun.started_at >= parse_date_from(date_from))
    if date_to:
        q = q.filter(AnalysisRun.started_at <= parse_date_to(date_to))
    if status:
        # page=1&page_size=1&status=completed is also how "most recent
        # completed run" (Home's "Last Analysis" pill, and any future page
        # needing the same lookup) is fetched — no separate endpoint needed.
        q = q.filter(AnalysisRun.status == RunStatus(status))
    if triggered_by:
        # "User" filter for the Analysis History page — who STARTED the
        # run (a run-level concept), as distinct from the Home dashboard's
        # user filter (who approved/rejected individual rows within a run,
        # via RowStatusHistory.triggered_by — a row-level concept). Both
        # happen to be named "triggered_by" in their respective tables but
        # answer different questions.
        q = q.filter(AnalysisRun.triggered_by == triggered_by)
    if bank_name or business_unit:
        # AnalysisRun itself has no bank/BU column (a run can span multiple
        # files/banks) — filter to runs that have AT LEAST ONE line item
        # matching, via the same LineItem table everything else filters on.
        line_item_q = db.query(LineItem.run_id)
        if bank_name:
            line_item_q = line_item_q.filter(LineItem.bank_name == bank_name)
        if business_unit:
            line_item_q = line_item_q.filter(LineItem.business_unit == business_unit)
        q = q.filter(AnalysisRun.run_id.in_(line_item_q.distinct().subquery()))
    total = q.count()
    rows = q.order_by(desc(AnalysisRun.started_at)).offset((page - 1) * page_size).limit(page_size).all()
    data = [compute_run_summary_row(db, r) for r in rows]
    return {"data": data, "total": total, "page": page, "page_size": page_size}


@router.get("/history/filter-options")
def get_run_history_filter_options(db: Session = Depends(get_db),
                                    user: User = Depends(require_permission("run:view"))):
    """Distinct 'Started By' values for the Analysis History page's user pill row."""
    users = sorted({
        v for (v,) in db.query(AnalysisRun.triggered_by).distinct() if v
    })
    return {"users": users}


@router.get("/file-preview/{filename}")
def get_file_preview(filename: str, bucket: str = "active", max_rows: int = 200,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_permission("run:view"))):
    return preview_bank_file(db, filename, max_rows)