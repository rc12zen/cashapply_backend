"""
app.aging.aging_store
========================
Holds the CURRENT AgingMap as a per-process in-memory cache, backed by a
Postgres snapshot so every process sees the same data.

WHY THE POSTGRES SNAPSHOT (PATCH):
This module used to be PURE in-memory — a plain module-level global,
nothing written anywhere durable. That broke the moment the app ran as
more than one process, which it always does: the API server (uvicorn)
and the background worker (`python -m app.tasks.worker`) are two
SEPARATE OS processes with separate memory, even in a "single worker"
deployment. Refreshing the aging report via the frontend hits the API
process and populated ONLY that process's `_state`. The worker process's
own `_state` was never touched — so the moment an analysis run actually
executed (in the worker), `get_aging_map()` returned None there and the
run failed immediately with "No aging map loaded", even though the UI
(reading aging-status from the API process) correctly showed the report
as loaded. This was called out as a known limitation in this module's own
original docstring ("if the backend ever runs as MULTIPLE
PROCESSES/workers... flag it again... swap this for Redis or similar") —
except the app already runs as 2+ processes today, not just "someday".

FIX: set_aging_map() now ALSO persists the parsed rows as JSON into the
existing `app_config` table (key="aging_map_snapshot") — no new table, no
new infra (keeps the app's existing "Postgres is the only shared state"
philosophy, same as the Procrastinate task queue). get_aging_map() checks
this process's in-memory cache first (fast path, no DB hit on every
lookup during a run); if empty, it falls back to loading + rebuilding the
AgingMap from the Postgres snapshot and caches it in-process from then on.

SCOPING (unchanged from before): still ONE AgingMap globally, no
per-user isolation yet — see original design note for that migration path
if/when SSO lands.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Optional

from .aging_map import AgingMap

_CONFIG_KEY = "aging_map_snapshot"

_lock = threading.Lock()


@dataclass
class AgingStoreState:
    aging_map: Optional[AgingMap] = None
    filename:  Optional[str] = None
    row_count: int = 0
    loaded_at: Optional[dt.datetime] = None


_state = AgingStoreState()  # per-process cache only — Postgres is the source of truth


def _row_to_dict(row) -> dict:
    return asdict(row) if is_dataclass(row) else dict(vars(row))


def _persist_snapshot(raw_rows: list, filename: str, row_count: int, loaded_at: dt.datetime) -> None:
    """Writes the parsed rows + metadata to Postgres so every process can see them."""
    # Local imports: avoids a circular import at module load time (parser.py
    # imports this module at the top level) and keeps DB session setup out
    # of this module's import path for callers that never need it.
    from ..db.session import get_session_factory
    from ..db.models import AppConfig

    payload = json.dumps({
        "filename": filename,
        "row_count": row_count,
        "loaded_at": loaded_at.isoformat(),
        "rows": [_row_to_dict(r) for r in raw_rows],
    })

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        existing = db.query(AppConfig).filter(AppConfig.key == _CONFIG_KEY).first()
        if existing:
            existing.value = payload
        else:
            db.add(AppConfig(key=_CONFIG_KEY, value=payload))
        db.commit()
    finally:
        db.close()


def _load_snapshot_from_db() -> Optional[AgingStoreState]:
    from ..db.session import get_session_factory
    from ..db.models import AppConfig
    from .parser import RawAgingRow  # local import — parser.py imports aging_store at module level

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        row = db.query(AppConfig).filter(AppConfig.key == _CONFIG_KEY).first()
        if row is None:
            return None
        payload = json.loads(row.value)
        # Tolerate snapshot/code drift in BOTH directions rather than taking
        # the aging map down on deploy. A snapshot written before a field
        # existed simply omits it (RawAgingRow supplies the default); one
        # written by newer code carrying a field this build doesn't know
        # about is ignored instead of raising TypeError on the unexpected
        # kwarg. Either way the map loads and the app keeps running — a
        # missing description column degrades the credit pool, it doesn't
        # break matching.
        known = {f.name for f in fields(RawAgingRow)}
        raw_rows = [RawAgingRow(**{k: v for k, v in r.items() if k in known}) for r in payload["rows"]]
        aging_map = AgingMap.build(raw_rows)
        return AgingStoreState(
            aging_map=aging_map,
            filename=payload.get("filename"),
            row_count=payload.get("row_count", len(raw_rows)),
            loaded_at=dt.datetime.fromisoformat(payload["loaded_at"]) if payload.get("loaded_at") else None,
        )
    finally:
        db.close()


def set_aging_map(aging_map: AgingMap, filename: str, row_count: int,
                   raw_rows: list | None = None) -> None:
    """
    Replace the current in-memory AgingMap AND persist a snapshot to
    Postgres (unless raw_rows is omitted, e.g. by an old caller — in which
    case this process's cache is updated but the Postgres snapshot is left
    stale; every current caller passes raw_rows, so this is a safety net
    only).
    """
    global _state
    loaded_at = dt.datetime.utcnow()
    with _lock:
        _state = AgingStoreState(
            aging_map=aging_map,
            filename=filename,
            row_count=row_count,
            loaded_at=loaded_at,
        )
    if raw_rows is not None:
        _persist_snapshot(raw_rows, filename, row_count, loaded_at)


def get_aging_map() -> Optional[AgingMap]:
    """
    Returns the currently loaded AgingMap. Checks this process's in-memory
    cache first (no DB round-trip on every lookup during a run); if empty
    — e.g. this is the worker process and the report was refreshed via the
    API process — falls back to the Postgres snapshot and caches it
    in-process from then on.
    """
    global _state
    with _lock:
        if _state.aging_map is not None:
            return _state.aging_map

    loaded = _load_snapshot_from_db()
    if loaded is None:
        return None
    with _lock:
        _state = loaded
    return _state.aging_map


def clear_aging_map() -> None:
    """Called by remove-aging — clears the in-memory map AND the Postgres snapshot."""
    global _state
    with _lock:
        _state = AgingStoreState()

    from ..db.session import get_session_factory
    from ..db.models import AppConfig

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        db.query(AppConfig).filter(AppConfig.key == _CONFIG_KEY).delete()
        db.commit()
    finally:
        db.close()


def get_status() -> dict:
    """Backs GET /api/config/aging-status — shape matches the frontend's AgingStatus type."""
    aging_map = get_aging_map()  # also triggers the DB-fallback populate path so status is accurate
    with _lock:
        return {
            "loaded": aging_map is not None,
            "row_count": _state.row_count,
            "filename": _state.filename,
            "loaded_at": _state.loaded_at.isoformat() if _state.loaded_at else None,
        }