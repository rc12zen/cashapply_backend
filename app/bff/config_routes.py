"""app.bff.config_routes — /api/config/*  (UPDATED)

CHANGES vs original:
  - upload-aging:   unchanged (still just saves file + SourceFile record).
  - refresh-aging:  load_aging_into_db() -> refresh_aging_map(). No more
                     writing into the aging_invoices table — parses the
                     Excel straight into an in-memory AgingMap via aging_store.
  - remove-aging:   `db.query(AgingInvoice).delete()` removed. Now calls
                     aging_store.clear_aging_map(). SourceFile archiving
                     behavior (file/audit history) is UNCHANGED.
  - aging-status:   no longer queries AgingInvoice at all. Reads
                     aging_store.get_status() directly — zero DB round trip.
  - banks / abbreviations / aging-preview / upload-aging: UNCHANGED.

Response shapes for refresh-aging / aging-status are kept backward-compatible
with the existing frontend contract ({loaded, row_count, filename}) — no
frontend changes required for this swap.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db.models import AppConfig, SourceFile, User
from ..deps import get_db
from ..auth import require_permission
from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..aging.uploader import handle_aging_upload
from ..aging.parser import refresh_aging_map
from ..aging.preview import preview_aging_file
from ..aging import aging_store

router = APIRouter()

# Read-only across this page requires just "run:view" (held by every role
# except Viewer); mutating the active aging report / abbreviations requires
# "config:manage" (Administrator only for now — see scripts/seed_rbac.py).


@router.post("/upload-aging")
async def upload_aging(file: UploadFile = File(...), db: Session = Depends(get_db),
                        user: User = Depends(require_permission("config:manage"))):
    data = await file.read()
    return handle_aging_upload(db, file.filename, data)


@router.post("/refresh-aging")
def refresh_aging(db: Session = Depends(get_db),
                   user: User = Depends(require_permission("config:manage"))):
    latest = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "aging_report", SourceFile.archived.is_(False))
        .order_by(SourceFile.uploaded_at.desc())
        .first()
    )
    if not latest:
        return {"loaded": False, "row_count": 0}

    # NOTE: no `db` write happens inside refresh_aging_map() itself — it only
    # reads the file from storage and builds an in-memory AgingMap.
    result = refresh_aging_map(db, latest)
    return {
        "loaded": True,
        "row_count": result["row_count"],
        "invoice_count": result["invoice_count"],
        "customer_count": result["customer_count"],
        "filename": latest.filename,
    }


@router.delete("/remove-aging")
def remove_aging(db: Session = Depends(get_db),
                  user: User = Depends(require_permission("config:manage"))):
    latest = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "aging_report", SourceFile.archived.is_(False))
        .order_by(SourceFile.uploaded_at.desc())
        .first()
    )
    if latest:
        latest.archived = True
        db.commit()

    # Was: db.query(AgingInvoice).delete(); db.commit()
    # Now: clear the in-memory AgingMap. File stays in storage, SourceFile
    # row stays archived (not deleted) — audit trail preserved either way.
    aging_store.clear_aging_map()

    return {"archived": True}


@router.get("/aging-status")
def aging_status(user: User = Depends(require_permission("run:view"))):
    # Was: db.query(AgingInvoice).count() + a SourceFile query.
    # Now: aging_store already tracks filename/row_count/loaded_at from the
    # last refresh — zero DB round trip needed for this endpoint at all.
    return aging_store.get_status()


@router.get("/ai-status")
def ai_status(force: bool = False, user: User = Depends(require_permission("run:view"))):
    """Is AI extraction (Layer 2B's fallback pass -- see
    extraction/ai_providers.py) actually usable right now, not just
    "is a key present". Shown on Home before a SPOC starts analysis, so
    they know upfront whether the AI second pass will run or unresolved
    rows will only get regex/pattern matching. Cached briefly server-side
    (see ai_providers.get_ai_status) -- pass ?force=true to bypass that
    (wired to a "Recheck" button on the frontend)."""
    from ..extraction.ai_providers import get_ai_status
    return get_ai_status(force_refresh=force)


@router.get("/aging-history")
def aging_history(db: Session = Depends(get_db),
                   user: User = Depends(require_permission("run:view"))):
    """
    Every aging report ever loaded — via manual upload OR the watch-folder
    watcher (app.aging.watcher) — most recent first. Nothing here is ever
    hard-deleted: uploading/selecting a new one just archives the rest
    (see watcher._process_file / remove_aging above), so this list is a
    full, permanent history the UI can offer as a "load a past aging
    snapshot" dropdown even when a different one is currently active.
    Exactly one row has is_active=true at any moment — the one currently
    held in aging_store's in-memory AgingMap.
    """
    rows = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "aging_report")
        .order_by(SourceFile.uploaded_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "filename": r.filename,
                "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
                "is_active": not r.archived,
            }
            for r in rows
        ]
    }


@router.post("/aging-select/{source_file_id}")
def select_aging_source(source_file_id: int, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("config:manage"))):
    """
    Switches the ACTIVE aging report to a past upload chosen from the
    aging-history dropdown, and reloads it into the in-memory AgingMap
    (same refresh_aging_map() used by /refresh-aging). Never touches blob
    storage or deletes anything — this only flips which SourceFile row is
    archived=False, mirroring exactly what the watcher does when a new
    file lands in the watch folder.

    NOTE: if the watch folder later receives a genuinely new filename, the
    watcher will re-assert that newest file as active on its next scan
    (and again on server restart, since it always loads the newest file
    on startup). This selection is a manual override that holds until
    then — not a permanent pin.
    """
    target = (
        db.query(SourceFile)
        .filter(SourceFile.kind == "aging_report", SourceFile.id == source_file_id)
        .first()
    )
    if not target:
        raise AppError(ErrorCode.AGING_SOURCE_NOT_FOUND)

    db.query(SourceFile).filter(
        SourceFile.kind == "aging_report",
        SourceFile.id != source_file_id,
    ).update({"archived": True})
    target.archived = False

    # PATCH: this used to commit here, BEFORE refresh_aging_map() below —
    # same bug as aging/watcher.py's _process_file() (see that file's
    # PATCH note). If refresh_aging_map() failed, FastAPI would return a
    # 500 to the user (visible, at least), but the archived-flag changes
    # were already durable — leaving a file marked "(active)" in the
    # aging-history dropdown whose data never actually loaded into
    # aging_store. Now commits only after a successful reload, and rolls
    # back cleanly on failure so a broken file never silently becomes the
    # "active" one.
    try:
        result = refresh_aging_map(db, target)
    except Exception as exc:
        db.rollback()
        raise AppError(ErrorCode.AGING_REPORT_PARSE_FAILED, detail=str(exc))

    db.commit()
    db.refresh(target)
    return {
        "loaded": True,
        "row_count": result["row_count"],
        "invoice_count": result["invoice_count"],
        "customer_count": result["customer_count"],
        "filename": target.filename,
    }


@router.get("/aging-preview")
def aging_preview(max_rows: int = 200, db: Session = Depends(get_db),
                   user: User = Depends(require_permission("run:view"))):
    # Unchanged — reads the Excel file directly from storage, never touched
    # the AgingInvoice table.
    return preview_aging_file(db, max_rows)


@router.get("/abbreviations")
def get_abbreviations(db: Session = Depends(get_db),
                       user: User = Depends(require_permission("run:view"))):
    rows = db.query(AppConfig).filter(AppConfig.key.like("abbrev:%")).all()
    return {"abbreviations": {r.key.split(":", 1)[1]: r.value for r in rows}}


@router.put("/abbreviations")
def update_abbreviations(payload: dict, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("config:manage"))):
    abbreviations: dict = payload.get("abbreviations", {})
    for alias, canonical in abbreviations.items():
        key = f"abbrev:{alias}"
        row = db.query(AppConfig).filter(AppConfig.key == key).first()
        if row:
            row.value = canonical
        else:
            db.add(AppConfig(key=key, value=canonical))
    db.commit()
    return {"updated": len(abbreviations)}