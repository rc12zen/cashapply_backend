"""
app.extraction.debug_logger
=============================
Shared debug logging utility for Layer 2A (regex) and Layer 2B (AI).

Every extraction decision — regex hit, aging lookup, AI prompt, AI response,
validation outcome — should be logged through `dbg()` so the full reasoning
trail for a run can be replayed later for audit / HITL review / bug triage.

Output goes to BOTH:
  1. Console (print) — for live visibility during a run
  2. A per-run log file — logs/extraction/run_<run_id>.log

Set EXTRACTION_LOG_DIR env var to change the log directory (defaults to
"logs/extraction" relative to wherever the process runs).

Usage:
    from .debug_logger import dbg
    dbg(run_id, layer="2A", row_ref=row.bank_reference, message="REF NO hit: ...")
"""
from __future__ import annotations

import os
import threading
import datetime as dt

# .abspath() up front so EXTRACTION_LOG_DIR is pinned to one concrete
# directory as soon as it's read from the environment, rather than a raw,
# unprocessed string flowing straight into os.makedirs/open further down
# (the CWE-23 pattern static scanners flag). It's operator-controlled at
# deploy time, not attacker-reachable, so it's intentionally still free to
# point anywhere the operator chooses -- this just makes resolution explicit.
_LOG_DIR = os.path.abspath(os.environ.get("EXTRACTION_LOG_DIR", "logs/extraction"))
_lock = threading.Lock()  # log file is shared across ThreadPoolExecutor workers


def get_log_path(run_id: int) -> str:
    os.makedirs(_LOG_DIR, exist_ok=True)
    return os.path.join(_LOG_DIR, f"run_{run_id}.log")


def dbg(run_id: int, layer: str, row_ref: str, message: str) -> None:
    """
    Log a single debug line to console + file.

    Args:
        run_id:  the AnalysisRun id — used to name the log file
        layer:   '2A' | '2B' | '2A-OUscan' etc — keep it short, consistent
        row_ref: short row identifier (bank_reference, or f"idx={row_index}")
        message: the actual debug content
    """
    ts = dt.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}][{layer}][row={row_ref}] {message}"
    print(line)
    try:
        with _lock:
            with open(get_log_path(run_id), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as exc:
        # Never let logging failure break the extraction pipeline
        print(f"[debug_logger] WARNING: could not write log file: {exc}")


def dbg_block(run_id: int, layer: str, row_ref: str, title: str, lines: list[str]) -> None:
    """Log a multi-line block (e.g. full AI prompt/response) under one title."""
    dbg(run_id, layer, row_ref, f"── {title} " + "─" * max(0, 40 - len(title)))
    for ln in lines:
        for sub in str(ln).splitlines() or [""]:
            dbg(run_id, layer, row_ref, f"    {sub}")
    dbg(run_id, layer, row_ref, "─" * 44)