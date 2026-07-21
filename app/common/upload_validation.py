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


def validate_statement_upload(filename: str | None) -> None:
    """Raise AppError(STATEMENT_FILE_TYPE_UNSUPPORTED) unless `filename` ends
    in an allowed extension. No-op on success."""
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if ext not in ALLOWED_STATEMENT_EXTENSIONS:
        shown = filename or "(no filename)"
        detail = (
            f"'{shown}' has an unsupported type"
            + (f" ('.{ext}')" if ext else " (no extension)")
        )
        raise AppError(ErrorCode.STATEMENT_FILE_TYPE_UNSUPPORTED, detail=detail)
