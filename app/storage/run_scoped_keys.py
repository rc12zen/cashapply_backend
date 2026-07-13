"""
app.storage.run_scoped_keys
============================
All files stored in a bucket are namespaced under runs/{run_id}/ so that:
  - Files from different runs never collide.
  - Deleting a run's files is a single prefix delete.
  - Pre-signed URLs can be scoped to a run without wildcard permissions.

Usage:
    key = build_key(run_id=42, filename="SCB_GBP_June.xlsx")
    # → "runs/42/SCB_GBP_June.xlsx"
"""
from __future__ import annotations

import os


def build_key(run_id: int, filename: str) -> str:
    """
    Build a run-scoped storage key.
    Strips any path separators from filename to prevent traversal.
    """
    safe_name = os.path.basename(filename)
    return f"runs/{run_id}/{safe_name}"


def extract_run_id_from_key(key: str) -> int | None:
    """
    Parse run_id back out of a scoped key.
    Returns None if the key doesn't follow the runs/{id}/ convention.
    """
    parts = key.split("/")
    if len(parts) >= 2 and parts[0] == "runs":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None
