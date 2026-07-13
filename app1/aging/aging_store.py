"""
app.aging.aging_store
========================
Holds the CURRENT AgingMap in memory. Replaces the old pattern of
truncate+reload into an `aging_invoices` DB table.

WHY: the AgingInvoice table was a single GLOBAL table — every upload
truncated and replaced it, so two users uploading aging reports around the
same time would corrupt each other's data, and a run could have its aging
ledger yanked out from under it mid-run by an unrelated upload elsewhere.

NOW: the parsed aging file becomes an AgingMap object, held in memory,
never written to a shared DB table.

SCOPING (current state — intentionally simple):
  This module holds ONE AgingMap globally, for the whole backend process.
  That is correct for now (no user scoping yet, per your call). When SSO
  lands and per-user isolation is needed, change ONLY this file:
    - swap `_state: AgingStoreState` for `_state_by_user: dict[str, AgingStoreState]`
    - every method below takes a `user_id` key instead of operating globally
  No other module should ever import AgingMap directly from the DB —
  everything goes through get_aging_map()/set_aging_map() here, so that
  swap is contained to this one file.

CONCURRENCY NOTE: if the backend ever runs as MULTIPLE PROCESSES/workers
(not just multiple threads), a plain in-memory dict like this will NOT be
shared across them — uploads handled by worker A won't be visible to
requests landed on worker B. That's fine for a single-process deployment;
flag it again if/when you move to multi-worker so we swap this for Redis
or similar at that point.
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from typing import Optional

from .aging_map import AgingMap

_lock = threading.Lock()


@dataclass
class AgingStoreState:
    aging_map: Optional[AgingMap] = None
    filename:  Optional[str] = None
    row_count: int = 0
    loaded_at: Optional[dt.datetime] = None


_state = AgingStoreState()


def set_aging_map(aging_map: AgingMap, filename: str, row_count: int) -> None:
    """Replace the current in-memory AgingMap. Called by refresh-aging."""
    global _state
    with _lock:
        _state = AgingStoreState(
            aging_map=aging_map,
            filename=filename,
            row_count=row_count,
            loaded_at=dt.datetime.utcnow(),
        )


def get_aging_map() -> Optional[AgingMap]:
    """
    Returns the currently loaded AgingMap, or None if nothing has been
    uploaded+refreshed yet this process lifetime.
    """
    with _lock:
        return _state.aging_map


def clear_aging_map() -> None:
    """Called by remove-aging — clears the in-memory map (file in storage is untouched)."""
    global _state
    with _lock:
        _state = AgingStoreState()


def get_status() -> dict:
    """Backs GET /api/config/aging-status — shape matches the frontend's AgingStatus type."""
    with _lock:
        return {
            "loaded": _state.aging_map is not None,
            "row_count": _state.row_count,
            "filename": _state.filename,
            "loaded_at": _state.loaded_at.isoformat() if _state.loaded_at else None,
        }