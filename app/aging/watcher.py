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
     When a new or changed file appears, it:
       a. Reads the bytes.
       b. Saves to blob storage (aging-reports bucket).
       c. Creates/updates the SourceFile DB record.
       d. Calls refresh_aging_map() to reload the AgingMap in memory.

PATCH — dedupe is now by (filename, mtime), not filename alone:
Previously "new" meant "a filename not seen in this process lifetime" (a
plain set of filenames). That meant a file re-dropped with the SAME
filename but genuinely different/refreshed content was silently ignored
forever, for the rest of that process's lifetime — confirmed as a real,
live bug during the prod/UAT stand-up: the Oracle SFTP puller correctly
re-downloaded a fresher xxzen_aging_report_excel.xls into this folder, but
because a file with that exact name had already been processed once
earlier in the same backend run, the watcher never touched it again and
the UI kept showing the stale snapshot. Tracking (filename -> last-
processed mtime) instead means a re-drop with a newer mtime is correctly
treated as new, while a byte-for-byte re-drop with an unchanged mtime
still isn't reprocessed pointlessly.

Also exposes check_now(), a synchronous "check immediately" entry point
for a manual "Check Now" action on the frontend (see
bff/config_routes.py's /check-aging-watch-folder) — same underlying scan
logic as the background loop, just triggered on demand instead of waiting
for the next AGING_POLL_INTERVAL_SECONDS tick.

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
from .file_sniff import check_extension_mismatch

log = logging.getLogger(__name__)

AGING_BUCKET = "aging-reports"
ELIGIBLE_EXTENSIONS = {".xlsx", ".xls", ".csv"}

# Filenames processed in this server lifetime, mapped to the mtime they were
# processed at — a file reappearing with a NEWER mtime is reprocessed; the
# same mtime is skipped (avoids redundant reloads on every poll tick for a
# file that hasn't actually changed).
_processed: dict[str, float] = {}
_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_watch_folder() -> Path:
    settings = get_settings()
    folder = Path(getattr(settings, "AGING_WATCH_FOLDER", "./aging_watch"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _eligible_files(folder: Path) -> list[Path]:
    """All files in folder with an eligible extension, sorted oldest-first
    (by mtime), so if several are genuinely new/changed in one scan, the
    most recent one ends up processed last and therefore active."""
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
    log.info(f"[aging_watcher] Processing aging file: {filename}")

    try:
        data = filepath.read_bytes()

        # PATCH: reject up front if the file's actual bytes don't match
        # its extension (e.g. a legacy .xls binary dropped in the watch
        # folder with a .xlsx name). Without this, ingestion silently
        # "succeeds" -- pandas can often still parse it for the AgingMap
        # if xlrd happens to be installed -- and the mismatch only
        # surfaces much later when someone downloads the raw file from
        # the app and a real Excel client refuses to open it. Logged and
        # skipped (not raised) to match this function's existing
        # log-and-return-False contract; see file_sniff.py for the exact
        # detection logic. The file itself is left untouched in the watch
        # folder — a corrected drop-in replacement with a NEWER mtime will
        # still be picked up and retried on the next scan.
        mismatch = check_extension_mismatch(filename, data)
        if mismatch:
            log.error(f"[aging_watcher] Skipping '{filename}': {mismatch}")
            return False

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


def _scan_once(folder: Path) -> list[str]:
    """
    Check folder for any file that's new or whose mtime has advanced since
    it was last processed. Returns the list of filenames actually
    (re)loaded this scan, so callers (e.g. the manual check_now() entry
    point) can report what happened rather than just "done, maybe nothing".
    """
    reloaded: list[str] = []
    for filepath in _eligible_files(folder):
        fname = filepath.name
        mtime = filepath.stat().st_mtime

        with _lock:
            last_seen = _processed.get(fname)
            if last_seen is not None and mtime <= last_seen:
                continue
            # Mark as seen immediately (at this mtime) so concurrent ticks
            # don't double-process the same version of this file.
            _processed[fname] = mtime

        success = _process_file(filepath)
        if success:
            reloaded.append(fname)
        else:
            # Roll back the recorded mtime so the next tick retries this
            # exact version instead of treating a failed load as "handled".
            with _lock:
                _processed.pop(fname, None)

    return reloaded


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


def check_now() -> dict:
    """
    Manual, synchronous "check the watch folder right now" entry point —
    backs the frontend's "Check Now" button (see
    bff/config_routes.py POST /check-aging-watch-folder), rather than
    waiting for the next background poll tick.

    Returns a small summary rather than nothing, so the API/UI can tell the
    user whether anything actually changed:
        {"checked": True, "reloaded": [...filenames...]}
    """
    folder = _get_watch_folder()
    reloaded = _scan_once(folder)
    return {"checked": True, "reloaded": reloaded}