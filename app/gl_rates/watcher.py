"""
app.gl_rates.watcher
======================
Auto-detects new GL Daily Rates extract files from a watched folder and
UPSERTS their rows into the gl_daily_rates table.

Deliberately modeled after aging/watcher.py -- same watch-folder /
poll-interval / "new filename = new file" mechanics. The one real
difference: aging's _process_file() calls refresh_aging_map() (in-memory
only); this module's _process_file() calls load_gl_rates_into_db(), which
actually writes rows to the DB and does NOT archive/replace anything --
every file that shows up here just ADDS (or updates, on the natural key)
rows, since GL rate history is meant to accumulate (e.g. a new day's rates
dropped each morning), unlike the aging report which represents a single
"current" snapshot.

Source:
  GL_RATES_SOURCE = "local_folder" (default) or "sftp" (future)
  GL_RATES_WATCH_FOLDER = path to watch (default ./gl_rates_watch, created on startup)

On startup:
  1. Creates the watch folder if it doesn't exist.
  2. Scans for any existing eligible file(s) -> loads all of them.
  3. Starts a background thread that polls every
     GL_RATES_POLL_INTERVAL_SECONDS (30). When a new file appears
     (detected by filename), it:
       a. Reads the bytes.
       b. Saves to blob storage (gl-rates bucket).
       c. Creates/updates the SourceFile DB record (kind="gl_daily_rates").
       d. Parses the file and upserts its rows into gl_daily_rates.

"New" means a filename not seen in this process lifetime (we track a set
of processed filenames) -- same convention as aging/watcher.py. To force a
re-load of a file whose rates changed, use a different filename (or restart
the process, which re-scans everything currently in the folder).

SFTP: not implemented here yet -- see aging/watcher.py's SFTP stub comment
for the intended shape when that's needed; the rest of the pipeline
(save -> SourceFile -> parse/upsert) is identical either way.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ..db.session import session_scope
from ..db.models import SourceFile
from ..db.settings import get_settings
from ..storage.client import get_storage_client
from .parser import load_gl_rates_into_db, GL_RATES_BUCKET

log = logging.getLogger(__name__)

ELIGIBLE_EXTENSIONS = {".xlsx", ".xls", ".csv"}

# Filenames processed in this server lifetime -- avoids re-processing same file.
_processed: set[str] = set()
_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_watch_folder() -> Path:
    settings = get_settings()
    folder = Path(getattr(settings, "GL_RATES_WATCH_FOLDER", "./gl_rates_watch"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _eligible_files(folder: Path) -> list[Path]:
    """All files in folder with an eligible extension, sorted oldest-first
    (so if today's and yesterday's files both landed while the watcher was
    down, older rates are upserted first and newer ones win on conflict)."""
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in ELIGIBLE_EXTENSIONS
    ]
    return sorted(files, key=lambda f: f.stat().st_mtime)


def _process_file(filepath: Path) -> bool:
    """
    Load a single GL rates file: save to blob -> SourceFile record ->
    parse -> upsert rows into gl_daily_rates. Returns True on success.
    """
    filename = filepath.name
    log.info(f"[gl_rates_watcher] Processing new GL rates file: {filename}")

    try:
        data = filepath.read_bytes()
        storage = get_storage_client()
        storage.save(GL_RATES_BUCKET, filename, data)

        with session_scope() as db:
            existing = db.query(SourceFile).filter(
                SourceFile.kind == "gl_daily_rates",
                SourceFile.filename == filename,
            ).first()

            if existing:
                source_file = existing
                source_file.archived = False
            else:
                source_file = SourceFile(
                    kind="gl_daily_rates",
                    filename=filename,
                    storage_key=filename,
                )
                db.add(source_file)
                db.flush()

            # NOTE: unlike aging/watcher.py, we do NOT archive other
            # gl_daily_rates SourceFile rows here -- every GL rates file
            # contributes rows (via upsert) rather than replacing a single
            # "current" one. Rate history is meant to accumulate.

            result = load_gl_rates_into_db(db, source_file)
            log.info(
                f"[gl_rates_watcher] Loaded '{filename}': "
                f"row_count={result['row_count']}, written_count={result['written_count']}"
            )

        return True

    except Exception as e:
        log.error(f"[gl_rates_watcher] Failed to process '{filename}': {e}")
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
    log.info(f"[gl_rates_watcher] Watching '{folder}' every {interval}s")
    while True:
        try:
            _scan_once(folder)
        except Exception as e:
            log.error(f"[gl_rates_watcher] Scan error: {e}")
        time.sleep(interval)


# ── Public API ────────────────────────────────────────────────────────────────

def start_gl_rates_watcher() -> None:
    """
    Called from app.main on_startup, alongside aging's start_watcher().
    1. Creates the watch folder.
    2. Runs an immediate scan (loads whatever is already there).
    3. Starts the background polling thread.
    """
    settings = get_settings()
    folder = _get_watch_folder()
    interval = int(getattr(settings, "GL_RATES_POLL_INTERVAL_SECONDS", 30))

    log.info(f"[gl_rates_watcher] Watch folder: {folder.resolve()}")

    _scan_once(folder)

    thread = threading.Thread(
        target=_watch_loop,
        args=(folder, interval),
        daemon=True,
        name="gl-rates-watcher",
    )
    thread.start()
    log.info("[gl_rates_watcher] Background watcher thread started.")