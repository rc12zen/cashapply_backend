"""
app.storage.uploader
=====================
Handles the mechanics of receiving a file upload and persisting it
under a run-scoped key. Decoupled from FastAPI so it can be called
from both route handlers and background tasks.
"""
from __future__ import annotations

from .client import get_storage_client
from .run_scoped_keys import build_key


def save_upload(bucket: str, run_id: int, filename: str, data: bytes) -> str:
    """
    Persist raw bytes to storage under runs/{run_id}/{filename}.
    Returns the storage key (not a full path — always use storage client to resolve).
    """
    key = build_key(run_id, filename)
    client = get_storage_client()
    client.save(bucket, key, data)
    return key


def delete_upload(bucket: str, storage_key: str) -> None:
    """Remove a previously saved file by its storage key."""
    get_storage_client().delete(bucket, storage_key)
