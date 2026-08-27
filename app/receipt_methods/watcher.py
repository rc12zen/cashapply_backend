"""
app.receipt_methods.watcher
==============================
Auto-detects new AR Receipt Methods extract files from a watched folder
and REGENERATES oracle/configs/receipt_method_map.json from them.

Deliberately modeled after gl_rates/watcher.py -- same watch-folder /
poll-interval / "new filename = new file" mechanics. The one real
difference: gl_rates' _process_file() upserts rows into a DB table that
accumulates history; this module's _process_file() calls
build_receipt_method_map() + write_receipt_method_map(), which REPLACES
the JSON config wholesale -- there's no history to accumulate here, each
extract is simply the current, complete picture (same model as
aging/watcher.py's in-memory snapshot, except this snapshot is a file on
disk, not an in-memory map, because oracle/receipt_method_resolver.py
already reads a file today and changing that contract wasn't necessary).

Source:
  RECEIPT_METHODS_SOURCE = "local_folder" (default) or "sftp" (future)
  RECEIPT_METHODS_WATCH_FOLDER = path to watch (default ./receipt_methods_watch)

On startup:
  1. Creates the watch folder if it doesn't exist.
  2. Scans for any existing eligible file(s) -> loads the newest one.
  3. Starts a background thread that polls every
     RECEIPT_METHODS_POLL_INTERVAL_SECONDS (30). When a new file appears
     (detected by filename), it:
       a. Reads the bytes.
       b. Saves to blob storage (receipt-methods bucket).
       c. Creates/updates the SourceFile DB record (kind="receipt_methods")
          -- same audit-trail pattern as aging/gl_rates, even though the
          actual output (receipt_method_map.json) isn't DB-backed.
       d. Parses the file and REWRITES oracle/configs/receipt_method_map.json.

"New" means a filename not seen in this process lifetime (we track a set
of processed filenames) -- same convention as aging/watcher.py and
gl_rates/watcher.py. To force a re-load of a file whose content changed
under the same filename, restart the process (which re-scans everything
currently in the folder) -- or just drop it under a different filename,
same as the other two watchers.

Once written, oracle/receipt_method_resolver.py picks up the new file on
its very next call -- app.common.json_cache is mtime-based, not a bare
in-process cache, so both the API process and the worker process converge
automatically without a restart or a manual reload endpoint.
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
from ..common.json_cache import invalidate as invalidate_json_cache
from .parser import build_receipt_method_map, write_receipt_method_map, get_output_path, RECEIPT_METHODS_BUCKET

log = logging.getLogger(__name__)

ELIGIBLE_EXTENSIONS = {".xlsx", ".xls", ".csv", ".txt"}

# Filenames processed in this server lifetime -- avoids re-processing same file.
_processed: set[str] = set()
_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_watch_folder() -> Path:
    settings = get_settings()
    folder = Path(getattr(settings, "RECEIPT_METHODS_WATCH_FOLDER", "./receipt_methods_watch"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _eligible_files(folder: Path) -> list[Path]:
    """All files in folder with an eligible extension, sorted oldest-first
    -- same convention as gl_rates/watcher.py, so if multiple files landed
    while the watcher was down, they're processed in drop order and the
    newest one's regeneration wins last."""
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in ELIGIBLE_EXTENSIONS
    ]
    return sorted(files, key=lambda f: f.stat().st_mtime)


def _process_file(filepath: Path) -> bool:
    """
    Load a single receipt-methods file: save to blob -> SourceFile record
    -> parse -> rewrite receipt_method_map.json. Returns True on success.
    """
    filename = filepath.name
    log.info(f"[receipt_methods_watcher] Processing new receipt methods file: {filename}")

    try:
        data = filepath.read_bytes()
        storage = get_storage_client()
        storage.save(RECEIPT_METHODS_BUCKET, filename, data)

        with session_scope() as db:
            existing = db.query(SourceFile).filter(
                SourceFile.kind == "receipt_methods",
                SourceFile.filename == filename,
            ).first()

            if existing:
                source_file = existing
                source_file.archived = False
            else:
                source_file = SourceFile(
                    kind="receipt_methods",
                    filename=filename,
                    storage_key=filename,
                )
                db.add(source_file)
                db.flush()

        output_path = get_output_path()
        map_dict = build_receipt_method_map(str(filepath))
        write_receipt_method_map(map_dict, output_path)
        # Not strictly required -- json_cache is mtime-based and will pick
        # up the new file on its own next call -- but forces an immediate
        # re-read in THIS process rather than waiting for the next lookup,
        # and costs nothing.
        invalidate_json_cache(output_path)

        log.info(
            f"[receipt_methods_watcher] Regenerated receipt method map at {output_path} from "
            f"'{filename}': {len(map_dict.get('accounts', {}))} account(s), "
            f"{len(map_dict.get('_accounts_with_unresolved_ambiguity', []))} flagged ambiguous."
        )
        return True

    except Exception as e:
        log.error(f"[receipt_methods_watcher] Failed to process '{filename}': {e}")
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
    log.info(f"[receipt_methods_watcher] Watching '{folder}' every {interval}s")
    while True:
        try:
            _scan_once(folder)
        except Exception as e:
            log.error(f"[receipt_methods_watcher] Scan error: {e}")
        time.sleep(interval)


# ── Public API ────────────────────────────────────────────────────────────────

def start_receipt_methods_watcher() -> None:
    """
    Called from app.main on_startup, alongside aging's start_watcher() and
    gl_rates' start_gl_rates_watcher().
    1. Creates the watch folder.
    2. Runs an immediate scan (loads whatever is already there).
    3. Starts the background polling thread.
    """
    settings = get_settings()
    folder = _get_watch_folder()
    interval = int(getattr(settings, "RECEIPT_METHODS_POLL_INTERVAL_SECONDS", 30))

    log.info(f"[receipt_methods_watcher] Watch folder: {folder.resolve()}")

    _scan_once(folder)

    thread = threading.Thread(
        target=_watch_loop,
        args=(folder, interval),
        daemon=True,
        name="receipt-methods-watcher",
    )
    thread.start()
    log.info("[receipt_methods_watcher] Background watcher thread started.")