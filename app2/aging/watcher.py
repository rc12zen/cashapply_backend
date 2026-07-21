"""
app.aging.watcher
==================
Auto-detects new aging report files from a watched folder and loads them
into the in-memory AgingMap + blob storage.

Source:
  AGING_SOURCE = "local_folder" (default) or "sftp" (future)
  AGING_WATCH_FOLDER = path to watch (default ./aging_watch, created on startup)

On startup:
  1. Creates the watch folder if it doesn't exist.
  2. Scans for any existing eligible file → loads it immediately.
  3. Starts a background thread that polls every AGING_POLL_INTERVAL_SECONDS (30).
     When a new file appears (detected by filename), it:
       a. Reads the bytes.
       b. Saves to blob storage (aging-reports bucket).
       c. Creates/updates the SourceFile DB record.
       d. Calls refresh_aging_map() to reload the AgingMap in memory.

"New" means a filename not seen in this process lifetime (we track a set of
processed filenames). If the same filename appears again it is skipped —
to force a reload, rename the file or use a different filename.

SFTP: stub branch is included below; set AGING_SOURCE=sftp and fill in the
SFTP_* env vars when ready. The rest of the pipeline is identical.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..db.session import session_scope
from ..db.models import SourceFile
from ..db.settings import get_settings
from ..storage.client import get_storage_client
from ..aging.parser import refresh_aging_map

log = logging.getLogger(__name__)

AGING_BUCKET = "aging-reports"
ELIGIBLE_EXTENSIONS = {".xlsx", ".xls", ".csv"}

# Filenames processed in this server lifetime — avoids re-processing same file.
_processed: set[str] = set()
_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_watch_folder() -> Path:
    settings = get_settings()
    folder = Path(getattr(settings, "AGING_WATCH_FOLDER", "./aging_watch"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _eligible_files(folder: Path) -> list[Path]:
    """All files in folder with an eligible extension, sorted oldest-first."""
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in ELIGIBLE_EXTENSIONS
    ]
    return sorted(files, key=lambda f: f.stat().st_mtime)


def _process_file(filepath: Path) -> bool:
    """
    Load a single aging file: save to blob → SourceFile record → refresh AgingMap.
    Returns True on success.
    """
    filename = filepath.name
    log.info(f"[aging_watcher] Processing new aging file: {filename}")

    try:
        data = filepath.read_bytes()
        storage = get_storage_client()
        storage.save(AGING_BUCKET, filename, data)

        with session_scope() as db:
            # Upsert SourceFile record — if same filename was uploaded before, reuse it.
            existing = db.query(SourceFile).filter(
                SourceFile.kind == "aging_report",
                SourceFile.filename == filename,
            ).first()

            if existing:
                source_file = existing
                source_file.archived = False
            else:
                source_file = SourceFile(
                    kind="aging_report",
                    filename=filename,
                    storage_key=filename,
                )
                db.add(source_file)
                db.flush()

            # Archive all other aging reports so only this one is "active".
            db.query(SourceFile).filter(
                SourceFile.kind == "aging_report",
                SourceFile.filename != filename,
            ).update({"archived": True})

            # PATCH: this used to call db.commit() + db.refresh(source_file)
            # right here, BEFORE refresh_aging_map() below. session_scope()
            # is built to commit ONCE at the end of the `with` block and
            # roll EVERYTHING back together if any exception occurs inside
            # it — that early commit defeated that guarantee. If
            # refresh_aging_map() throws for any reason (a parse error, a
            # snapshot-write failure, anything), this function's own broad
            # `except Exception` below swallows it and just logs — but the
            # archived-flag flip had ALREADY been made durable by that
            # early commit. Net effect: the aging-history dropdown would
            # correctly show this file as "(active)" (the DB really does
            # say so), while aging_store never actually got the parsed
            # data — "NOT LOADED" forever, with no visible error anywhere
            # except a server log line nobody was watching. Now the whole
            # operation is atomic: if refresh_aging_map() fails, the
            # archived-flag changes above roll back with it, and the
            # previously-active file stays active instead of a broken one
            # silently taking its place.

            # Reload AgingMap in memory.
            result = refresh_aging_map(db, source_file)
            log.info(
                f"[aging_watcher] Loaded '{filename}': "
                f"row_count={result['row_count']}, "
                f"invoice_count={result['invoice_count']}, "
                f"customer_count={result['customer_count']}"
            )

        return True

    except Exception as e:
        log.error(f"[aging_watcher] Failed to process '{filename}': {e}")
        return False


def _scan_once(folder: Path) -> None:
    """Check folder for any file not yet processed this session."""
    for filepath in _eligible_files(folder):
        fname = filepath.name
        with _lock:
            if fname in _processed:
                continue
            # Mark as seen immediately so concurrent ticks don't double-process.
            _processed.add(fname)

        success = _process_file(filepath)
        if not success:
            # Remove from set so next tick can retry.
            with _lock:
                _processed.discard(fname)


def _watch_loop(folder: Path, interval: int) -> None:
    log.info(f"[aging_watcher] Watching '{folder}' every {interval}s")
    while True:
        try:
            _scan_once(folder)
        except Exception as e:
            log.error(f"[aging_watcher] Scan error: {e}")
        time.sleep(interval)


# ── Public API ────────────────────────────────────────────────────────────────

def start_watcher() -> None:
    """
    Called from app.main on_startup.
    1. Creates the watch folder.
    2. Runs an immediate scan (picks up any existing file).
    3. Starts the background polling thread.
    """
    settings = get_settings()
    folder = _get_watch_folder()
    interval = int(getattr(settings, "AGING_POLL_INTERVAL_SECONDS", 30))

    log.info(f"[aging_watcher] Watch folder: {folder.resolve()}")

    # Immediate scan on startup — load whatever is already there.
    _scan_once(folder)

    thread = threading.Thread(
        target=_watch_loop,
        args=(folder, interval),
        daemon=True,
        name="aging-watcher",
    )
    thread.start()
    log.info("[aging_watcher] Background watcher thread started.")