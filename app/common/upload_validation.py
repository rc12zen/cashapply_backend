"""
app.common.upload_validation
===============================
Single place that decides which file types a bank-statement upload accepts.

Only Excel (.xlsx, .xls) and CSV (.csv) are allowed. Anything else — PDF,
Word, images, zip, .xlsm, .txt, extension-less, etc. — is rejected up front
with a clear, user-facing message (STATEMENT_FILE_TYPE_UNSUPPORTED) rather
than being accepted and failing later, deep in parsing, with a cryptic error.

Used by BOTH upload entry points so the rule can't drift between them:
  - bff/run_routes.py         POST /api/run/upload            (Home upload)
  - bff/config_builder_routes POST /api/config/builder/upload (Config Builder)

Validation is by filename extension — the same signal the frontend `accept`
attribute uses. It's intentionally strict on the extension rather than
sniffing magic bytes: the goal here is "reject obviously-wrong formats with
a clear message," and a genuinely corrupt/mislabelled file is still caught
later by the parser (CONFIG_FILE_UNREADABLE).
"""
from __future__ import annotations

import os

from .error_codes import ErrorCode
from .errors import AppError

# The only accepted bank-statement extensions (lower-case, no dot).
ALLOWED_STATEMENT_EXTENSIONS: frozenset[str] = frozenset({"xlsx", "xls", "csv"})

# Max upload size — 10 MB (binary), matching the frontend's "Max 10 MB each" hint.
MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024


def _human_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def validate_statement_size(num_bytes: int | None) -> None:
    """Raise AppError(STATEMENT_FILE_TOO_LARGE) if `num_bytes` exceeds the
    10 MB limit. A None size (unknown Content-Length) is a no-op — the caller
    re-checks with the actual byte count after reading. No-op on success."""
    if num_bytes is not None and num_bytes > MAX_UPLOAD_BYTES:
        raise AppError(
            ErrorCode.STATEMENT_FILE_TOO_LARGE,
            detail=f"file is {_human_mb(num_bytes)} (limit {_human_mb(MAX_UPLOAD_BYTES)})",
        )


def safe_upload_filename(filename: str | None) -> str:
    """Raise AppError(STATEMENT_FILENAME_INVALID) if `filename` could act as a
    path rather than a plain file name (CWE-23 / Path Traversal).

    `filename` ends up stored verbatim as SourceFile.storage_key, which the
    storage clients then use directly as (or as part of) a real filesystem
    path / blob key — so a name like "../../app/main.py" or "C:\\Windows\\x"
    must never reach that far. Returns `filename` unchanged on success (for
    call-site convenience); raises otherwise.
    """
    name = filename or ""
    # basename() strips any directory component -- if that changes the
    # string, "/" or "\" (both are separators on Windows; POSIX only treats
    # "/" as one, but rejecting "\" everywhere is the point) was present.
    if not name or os.path.basename(name) != name:
        raise AppError(ErrorCode.STATEMENT_FILENAME_INVALID, detail=f"'{filename}'")
    if name in (".", "..") or ".." in name:
        raise AppError(ErrorCode.STATEMENT_FILENAME_INVALID, detail=f"'{filename}'")
    # Catches a Windows drive-letter/UNC form on a non-Windows dev machine,
    # where os.path.basename() above wouldn't otherwise flag it as absolute.
    if ":" in name or name.startswith("\\\\"):
        raise AppError(ErrorCode.STATEMENT_FILENAME_INVALID, detail=f"'{filename}'")
    return name


def validate_statement_upload(filename: str | None) -> None:
    """Raise AppError(STATEMENT_FILE_TYPE_UNSUPPORTED) unless `filename` ends
    in an allowed extension, or AppError(STATEMENT_FILENAME_INVALID) if it
    isn't a safe plain file name. No-op on success."""
    safe_upload_filename(filename)
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if ext not in ALLOWED_STATEMENT_EXTENSIONS:
        shown = filename or "(no filename)"
        detail = (
            f"'{shown}' has an unsupported type"
            + (f" ('.{ext}')" if ext else " (no extension)")
        )
        raise AppError(ErrorCode.STATEMENT_FILE_TYPE_UNSUPPORTED, detail=detail)
