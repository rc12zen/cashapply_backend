"""
app.storage.client
===================
StorageClient ABC with two implementations:
  LocalStorageClient  — writes under LOCAL_STORAGE_ROOT (dev/PoC)
  AzureBlobStorageClient — writes to Azure Blob containers (prod)

Swap implementations purely via ENVIRONMENT env var.
"""
from __future__ import annotations

import abc
import os
import shutil
from pathlib import Path
from typing import BinaryIO

from ..db.settings import get_settings


class StorageClient(abc.ABC):
    @abc.abstractmethod
    def save(self, bucket: str, key: str, data: bytes) -> str: ...

    @abc.abstractmethod
    def save_stream(self, bucket: str, key: str, stream: BinaryIO) -> str: ...

    @abc.abstractmethod
    def read(self, bucket: str, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, bucket: str, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, bucket: str, key: str) -> None: ...

    @abc.abstractmethod
    def list_keys(self, bucket: str, prefix: str = "") -> list[str]: ...

    @abc.abstractmethod
    def local_path_for_read(self, bucket: str, key: str) -> str:
        """Return a real filesystem path — needed by openpyxl/pandas."""


class LocalStorageClient(StorageClient):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, bucket: str, key: str) -> Path:
        p = self.root / bucket / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def save(self, bucket, key, data):
        p = self._path(bucket, key)
        p.write_bytes(data)
        return str(p)

    def save_stream(self, bucket, key, stream):
        p = self._path(bucket, key)
        with open(p, "wb") as f:
            shutil.copyfileobj(stream, f)
        return str(p)

    def read(self, bucket, key):
        return self._path(bucket, key).read_bytes()

    def exists(self, bucket, key):
        return self._path(bucket, key).exists()

    def delete(self, bucket, key):
        p = self._path(bucket, key)
        if p.exists():
            p.unlink()

    def list_keys(self, bucket, prefix=""):
        base = self.root / bucket
        if not base.exists():
            return []
        return [
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and str(p.relative_to(base)).startswith(prefix)
        ]

    def local_path_for_read(self, bucket, key):
        return str(self._path(bucket, key))


class AzureBlobStorageClient(StorageClient):
    def __init__(self, connection_string: str | None, account_url: str | None):
        from azure.storage.blob import BlobServiceClient
        if connection_string:
            self._client = BlobServiceClient.from_connection_string(connection_string)
        elif account_url:
            from azure.identity import DefaultAzureCredential
            self._client = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
        else:
            raise RuntimeError("AzureBlobStorageClient requires AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL.")

    def _container(self, bucket: str):
        c = self._client.get_container_client(bucket)
        if not c.exists():
            c.create_container()
        return c

    def save(self, bucket, key, data):
        self._container(bucket).upload_blob(key, data, overwrite=True)
        return f"azure://{bucket}/{key}"

    def save_stream(self, bucket, key, stream):
        self._container(bucket).upload_blob(key, stream, overwrite=True)
        return f"azure://{bucket}/{key}"

    def read(self, bucket, key):
        return self._container(bucket).download_blob(key).readall()

    def exists(self, bucket, key):
        return self._container(bucket).get_blob_client(key).exists()

    def delete(self, bucket, key):
        bc = self._container(bucket).get_blob_client(key)
        if bc.exists():
            bc.delete_blob()

    def list_keys(self, bucket, prefix=""):
        return [b.name for b in self._container(bucket).list_blobs(name_starts_with=prefix)]

    def local_path_for_read(self, bucket, key):
        import tempfile
        data = self.read(bucket, key)
        suffix = os.path.splitext(key)[1] or ".tmp"
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return tmp


_client_singleton: StorageClient | None = None


def get_storage_client() -> StorageClient:
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton
    s = get_settings()
    if s.ENVIRONMENT == "azure":
        _client_singleton = AzureBlobStorageClient(
            connection_string=s.AZURE_STORAGE_CONNECTION_STRING,
            account_url=s.AZURE_STORAGE_ACCOUNT_URL,
        )
    else:
        _client_singleton = LocalStorageClient(root=s.LOCAL_STORAGE_ROOT)
    return _client_singleton
