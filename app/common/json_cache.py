"""
app.common.json_cache
========================
Shared mtime-based cache for on-disk JSON config files (account_configs.json,
bank_ou_mapping.json, ou_functional_currency.json, receipt_method_map.json,
fx_conversion_type_map.json, etc).

WHY THIS EXISTS (bug it fixes):
Every one of those files used to be loaded via a bare `@lru_cache(maxsize=1)`
function. That's a PURE IN-PROCESS cache — once loaded, it never re-reads the
file for the lifetime of that process. The API server (uvicorn) and the
background worker (`python -m app.tasks.worker`) are always two SEPARATE
processes. Saving a new bank config via the Config Builder Wizard hits the
API process and calls `reload_account_configs()` there — which only clears
*that* process's cache. The worker process's own `@lru_cache`'d copy never
gets touched, so a newly-onboarded bank account would detect as UNKNOWN in
every analysis run (which executes in the worker) until the worker was
manually restarted — the exact same cross-process staleness bug already
fixed once for the aging map (see aging/aging_store.py), just recurring
here across three more files that never got the same treatment.

FIX: cache keyed by the file's own mtime, not just its path. Every call
checks the file's current mtime on disk (a cheap stat(), not a re-read)
and only re-parses the JSON if it's changed since this process last saw
it. Since the file itself lives on shared disk, both processes converge
automatically the moment either one saves a change — no manual reload
call, no cross-process signaling, no Postgres snapshot needed (unlike the
aging map, this data doesn't need DB-level durability or concurrent-write
safety, just staleness-free reads of a file every process can already see).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}  # path -> (mtime, parsed_json)


def load_json_cached(path: Path | str) -> Any:
    """
    Loads and parses a JSON file, re-parsing only when its mtime has
    changed since this process last loaded it. Raises the same exceptions
    a plain `json.load()` would (FileNotFoundError, json.JSONDecodeError)
    — callers that want a soft-fail default should catch those themselves.
    """
    path = Path(path)
    key = str(path)
    mtime = path.stat().st_mtime

    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    with _lock:
        _cache[key] = (mtime, data)
    return data


def invalidate(path: Path | str | None = None) -> None:
    """
    Drops the cached entry for one path, or every path if none is given.
    Not required for correctness (the mtime check already handles it) —
    useful only if a caller wants to force a re-read within the same
    mtime second (rare; mtime resolution is usually at least 1s).
    """
    with _lock:
        if path is None:
            _cache.clear()
        else:
            _cache.pop(str(Path(path)), None)