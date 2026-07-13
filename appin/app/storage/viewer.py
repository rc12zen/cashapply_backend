"""
app.storage.viewer
===================
Read-side operations: listing files, resolving local paths for pandas/openpyxl,
and (stub) pre-signed URL generation for direct browser downloads.
"""
from __future__ import annotations

import datetime as dt

from .client import get_storage_client


def list_files(bucket: str, prefix: str = "") -> list[str]:
    """List all storage keys in a bucket, optionally filtered by prefix."""
    return get_storage_client().list_keys(bucket, prefix)


def get_local_path(bucket: str, key: str) -> str:
    """
    Return a local filesystem path for the given key.
    For Azure: downloads to a temp file first.
    For local: returns the direct path.
    """
    return get_storage_client().local_path_for_read(bucket, key)


def file_exists(bucket: str, key: str) -> bool:
    return get_storage_client().exists(bucket, key)


def generate_presigned_read_url(bucket: str, key: str, expires_in_seconds: int = 3600) -> str:
    """
    Generate a pre-signed URL for direct browser download.

    Local (dev):  returns a placeholder token URL — wire to a /api/download/{token}
                  endpoint that proxies the file.
    Azure (prod): generates a real SAS URL with the specified TTL.
    """
    from ..db.settings import get_settings
    s = get_settings()

    if s.ENVIRONMENT == "azure":
        from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
        sas = generate_blob_sas(
            account_name=s.AZURE_STORAGE_ACCOUNT_URL.split(".")[0].replace("https://", "") if s.AZURE_STORAGE_ACCOUNT_URL else "",
            container_name=bucket,
            blob_name=key,
            account_key=None,          # use DefaultAzureCredential / user delegation key in prod
            permission=BlobSasPermissions(read=True),
            expiry=dt.datetime.utcnow() + dt.timedelta(seconds=expires_in_seconds),
        )
        return f"https://{s.AZURE_STORAGE_ACCOUNT_URL}/{bucket}/{key}?{sas}"

    # Local stub — a real endpoint would serve the file bytes
    return f"/api/storage/download?bucket={bucket}&key={key}"
