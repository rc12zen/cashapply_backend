"""
app.bff.storage_routes
========================
/api/storage/* — generic, read-only file proxy over the configured storage
backend (local disk in dev, Azure Blob in prod). Existed only as a stub URL
string inside storage/viewer.py's generate_presigned_read_url() before this
("Local stub — a real endpoint would serve the file bytes") — this is that
endpoint.

Primary consumer today: the row-detail page's remittance panel, which needs
to let a SPOC open/download the original remittance email/document (.msg /
.pdf / .eml) that cashapply-remittance-agent (App2) archived under the
"remittance-inbox" bucket. Also usable for bank-statements / aging-reports
if a future screen wants a direct download link for those.

Deliberately locked to a small allow-list of known buckets — this is a
generic-looking "read any key from any bucket" shape, and without the
allow-list a bad `bucket` value would let a caller read arbitrary paths
under LOCAL_STORAGE_ROOT.
"""
from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
import io

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import User
from ..auth import require_permission
from ..storage.client import get_storage_client

router = APIRouter()

_ALLOWED_BUCKETS = {"bank-statements", "aging-reports", "remittance-inbox"}


@router.get("/download")
def download_file(
    bucket: str, key: str,
    user: User = Depends(require_permission("file:download")),
):
    if bucket not in _ALLOWED_BUCKETS:
        raise AppError(ErrorCode.STORAGE_BUCKET_UNKNOWN, detail=f"bucket '{bucket}'")

    storage = get_storage_client()
    if not storage.exists(bucket, key):
        raise AppError(ErrorCode.STORAGE_FILE_NOT_FOUND)

    data = storage.read(bucket, key)
    content_type, _ = mimetypes.guess_type(key)
    # .msg has no reliable standard MIME type — fall back to a generic
    # download rather than guessing wrong and having the browser try (and
    # fail) to render it inline.
    content_type = content_type or "application/octet-stream"

    # Strip any timestamp prefix node_store_raw_file() added
    # ("20240115120000_original_name.msg") so the browser's save-as dialog
    # shows the filename the SPOC actually recognizes.
    download_name = key.split("_", 1)[1] if "_" in key else key

    return StreamingResponse(
        io.BytesIO(data),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )