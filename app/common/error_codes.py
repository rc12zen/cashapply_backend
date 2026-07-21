"""
app.common.error_codes
=========================
THE single source of truth for every user-facing error in this system.

No route/service should invent a title or a message inline anymore. Instead:

    from ..common.error_codes import ErrorCode
    from ..common.errors import AppError

    raise AppError(ErrorCode.ACCOUNT_UNRESOLVED, detail=f"account '{acct}'")

`detail` is optional, short, and NEVER technical (no stack traces, no SQL,
no internal object dumps) — it's appended to the code's canned message when
it adds something a user can act on (e.g. which account). If you don't need
it, just omit it.

── NUMBERING (see requirements doc) ────────────────────────────────────────
    1000-1999  Auth / RBAC
    2000-2999  Upload / Ingestion
    3000-3999  Config / Config Builder
    4000-4999  Run / Analysis
    5000-5999  HITL / Approval
    6000-6999  Oracle / Fusion
    7000-7999  Results / Metrics / Aging
    8000-8999  Admin / Users
    9000-9999  System / Validation / Unexpected (also the fallback bucket
               for any plain HTTPException(...) raised somewhere that
               hasn't been migrated to a specific AppError(ErrorCode...)
               yet — see common/errors.py's HTTPException handler)

Adding a new error is a single new entry below — a name, a number in the
right range, an HTTP status, a title, a user-facing message, and how it
should be logged (see `Severity`).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Severity(str, enum.Enum):
    """How much detail this error gets in the server log.

    LIGHT — an everyday, expected condition (bad input, duplicate row,
        "not found", a business-rule rejection). Logged at INFO/WARNING
        with just the code + one line of context. No traceback.
    HEAVY — a genuine failure (unhandled exception, an integration that
        should have worked but didn't, a data-integrity problem). Logged
        at ERROR with the full traceback via logger.exception(), always,
        regardless of the Debug/Info verbosity setting.
    """
    LIGHT = "light"
    HEAVY = "heavy"


@dataclass(frozen=True)
class ErrorDef:
    code: int
    name: str
    http_status: int
    title: str
    message: str
    severity: Severity = Severity.LIGHT


class ErrorCode:
    """Namespace of ErrorDef constants, grouped by domain range.

    Referenced as ErrorCode.SOME_NAME (an ErrorDef), or looked up by
    numeric code via ErrorCode.by_number(1007).
    """

    # ── 1000-1999  Auth / RBAC ──────────────────────────────────────────────
    NOT_SIGNED_IN = ErrorDef(
        1000, "NOT_SIGNED_IN", 401,
        "Not signed in",
        "You need to sign in to continue.",
    )
    TOKEN_INVALID = ErrorDef(
        1001, "TOKEN_INVALID", 401,
        "Sign-in expired",
        "Your sign-in session is no longer valid. Please sign in again.",
    )
    ACCOUNT_NOT_ONBOARDED = ErrorDef(
        1002, "ACCOUNT_NOT_ONBOARDED", 403,
        "Account not set up",
        "This account has not been onboarded. Contact an administrator to get access.",
    )
    ACCOUNT_DISABLED = ErrorDef(
        1003, "ACCOUNT_DISABLED", 403,
        "Account disabled",
        "Your account has been disabled. Contact an administrator.",
    )
    PERMISSION_DENIED = ErrorDef(
        1004, "PERMISSION_DENIED", 403,
        "Not allowed",
        "You don't have permission to do that. Contact an administrator if you think this is wrong.",
    )
    SSO_NOT_CONFIGURED = ErrorDef(
        1005, "SSO_NOT_CONFIGURED", 401,
        "Sign-in unavailable",
        "Single sign-on isn't configured yet. Contact an administrator.",
        severity=Severity.HEAVY,
    )
    LAST_ADMIN_LOCKOUT_ROLE = ErrorDef(
        1006, "LAST_ADMIN_LOCKOUT_ROLE", 400,
        "Can't change this role",
        "This is the last active administrator — change another administrator's role first.",
    )
    LAST_ADMIN_LOCKOUT_DEACTIVATE = ErrorDef(
        1007, "LAST_ADMIN_LOCKOUT_DEACTIVATE", 400,
        "Can't deactivate this account",
        "This is the last active administrator — an administrator can't deactivate the only remaining admin account.",
    )
    CANNOT_DEACTIVATE_SELF = ErrorDef(
        1008, "CANNOT_DEACTIVATE_SELF", 400,
        "Can't deactivate your own account",
        "You can't deactivate the account you're currently signed in with.",
    )

    # ── 2000-2999  Upload / Ingestion ───────────────────────────────────────
    STATEMENT_DUPLICATE = ErrorDef(
        2000, "STATEMENT_DUPLICATE", 409,
        "Already uploaded",
        "This bank statement has already been uploaded and processed.",
    )
    STATEMENT_UPLOAD_FAILED = ErrorDef(
        2001, "STATEMENT_UPLOAD_FAILED", 500,
        "Upload failed",
        "We couldn't process that file. Please try again, or contact support if it keeps happening.",
        severity=Severity.HEAVY,
    )
    STATEMENT_FORMAT_UNRECOGNIZED = ErrorDef(
        2002, "STATEMENT_FORMAT_UNRECOGNIZED", 400,
        "Unrecognized file format",
        "We couldn't recognize this bank statement's format. Try mapping it in Config Builder first.",
    )
    STATEMENT_NOT_FOUND = ErrorDef(
        2003, "STATEMENT_NOT_FOUND", 404,
        "File not found",
        "That statement file could not be found.",
    )
    ACCOUNT_UNRESOLVED = ErrorDef(
        2004, "ACCOUNT_UNRESOLVED", 400,
        "Account not recognized",
        "We couldn't match this bank account to a known organization unit. An administrator may need to add a mapping in Config Builder.",
    )
    INGESTION_IN_PROGRESS = ErrorDef(
        2005, "INGESTION_IN_PROGRESS", 409,
        "Still processing",
        "This file is still being processed. Please wait a moment and try again.",
    )
    REMITTANCE_UPLOAD_FAILED = ErrorDef(
        2006, "REMITTANCE_UPLOAD_FAILED", 500,
        "Remittance upload failed",
        "We couldn't process that remittance file. Please try again.",
        severity=Severity.HEAVY,
    )
    STORAGE_BUCKET_UNKNOWN = ErrorDef(
        2007, "STORAGE_BUCKET_UNKNOWN", 400,
        "Can't access that file",
        "That file location isn't recognized.",
    )
    STORAGE_FILE_NOT_FOUND = ErrorDef(
        2008, "STORAGE_FILE_NOT_FOUND", 404,
        "File not found",
        "That file could not be found in storage.",
    )
    STATEMENT_FILE_TYPE_UNSUPPORTED = ErrorDef(
        2009, "STATEMENT_FILE_TYPE_UNSUPPORTED", 400,
        "Unsupported file type",
        "Only Excel (.xlsx, .xls) and CSV (.csv) bank statements are accepted. "
        "Please upload one of those formats.",
    )

    # ── 3000-3999  Config / Config Builder ──────────────────────────────────
    CONFIG_NOT_FOUND = ErrorDef(
        3000, "CONFIG_NOT_FOUND", 404,
        "Config not found",
        "No configuration exists for that account yet.",
    )
    CONFIG_RECIPE_INVALID = ErrorDef(
        3001, "CONFIG_RECIPE_INVALID", 400,
        "Invalid configuration",
        "That configuration couldn't be saved — check the field mappings and try again.",
    )
    AGING_REPORT_MISSING = ErrorDef(
        3002, "AGING_REPORT_MISSING", 400,
        "No aging report loaded",
        "There's no active aging report. Upload one from the Config page before running analysis.",
    )
    AGING_SOURCE_NOT_FOUND = ErrorDef(
        3005, "AGING_SOURCE_NOT_FOUND", 404,
        "Aging report not found",
        "That aging report file could not be found.",
    )
    AGING_REPORT_PARSE_FAILED = ErrorDef(
        3003, "AGING_REPORT_PARSE_FAILED", 400,
        "Couldn't read aging report",
        "We couldn't read that aging report. Check the file format and try again.",
        severity=Severity.HEAVY,
    )
    ABBREVIATIONS_INVALID = ErrorDef(
        3004, "ABBREVIATIONS_INVALID", 400,
        "Invalid abbreviation list",
        "That abbreviation list couldn't be saved — check the format and try again.",
    )
    CONFIG_FILE_UNREADABLE = ErrorDef(
        3006, "CONFIG_FILE_UNREADABLE", 422,
        "Could not read this file",
        "This file couldn't be read — it may be corrupted or actually be a different format than its extension suggests.",
        severity=Severity.HEAVY,
    )
    CONFIG_FILE_TYPE_UNSUPPORTED = ErrorDef(
        3007, "CONFIG_FILE_TYPE_UNSUPPORTED", 400,
        "Unsupported file type",
        "That file type isn't supported here — upload an xlsx, xls, csv, or txt bank statement.",
    )
    CONFIG_FIELD_REQUIRED = ErrorDef(
        3008, "CONFIG_FIELD_REQUIRED", 400,
        "Missing required field",
        "A required field is missing — check the form and try again.",
    )
    CONFIG_SAVE_FAILED = ErrorDef(
        3009, "CONFIG_SAVE_FAILED", 500,
        "Could not save this config",
        "We couldn't save this configuration. It's been logged — please try again.",
        severity=Severity.HEAVY,
    )
    BANK_ACCOUNT_NOT_FOUND = ErrorDef(
        3010, "BANK_ACCOUNT_NOT_FOUND", 404,
        "Bank account not found",
        "That bank account could not be found.",
    )
    BUSINESS_UNIT_UNKNOWN = ErrorDef(
        3011, "BUSINESS_UNIT_UNKNOWN", 400,
        "Unknown Business Unit",
        "That Business Unit doesn't exist yet — add it via Config Builder first.",
    )
    BUSINESS_UNIT_REQUIRED = ErrorDef(
        3012, "BUSINESS_UNIT_REQUIRED", 400,
        "A primary Business Unit is required",
        "Choose a primary Business Unit for this account — it can't be left unset.",
    )

    # ── 4000-4999  Run / Analysis ────────────────────────────────────────────
    RUN_ALREADY_IN_PROGRESS = ErrorDef(
        4000, "RUN_ALREADY_IN_PROGRESS", 409,
        "Analysis already running",
        "An analysis run is already in progress. Wait for it to finish before starting another.",
    )
    RUN_NO_FILES_SELECTED = ErrorDef(
        4001, "RUN_NO_FILES_SELECTED", 400,
        "No files selected",
        "Select at least one statement to include before starting analysis.",
    )
    RUN_NOT_FOUND = ErrorDef(
        4002, "RUN_NOT_FOUND", 404,
        "Run not found",
        "That analysis run could not be found.",
    )
    RUN_FAILED = ErrorDef(
        4003, "RUN_FAILED", 500,
        "Analysis failed",
        "The analysis run failed unexpectedly. It's been logged — please try again, and contact support if it keeps happening.",
        severity=Severity.HEAVY,
    )
    RUN_NO_ANALYZABLE_FILES = ErrorDef(
        4004, "RUN_NO_ANALYZABLE_FILES", 400,
        "Nothing to analyze",
        "None of the selected statements are analyzable — they're either unrecognized (configure the account first) or have no pending rows.",
    )

    # ── 5000-5999  HITL / Approval ──────────────────────────────────────────
    ROW_NOT_APPROVABLE = ErrorDef(
        5000, "ROW_NOT_APPROVABLE", 400,
        "Not ready for approval",
        "This row isn't eligible for approval yet.",
    )
    ROW_VERSION_CONFLICT = ErrorDef(
        5001, "ROW_VERSION_CONFLICT", 409,
        "Changed since you loaded it",
        "This row changed since you loaded it. Refresh and try again.",
    )
    ROW_NOT_FOUND = ErrorDef(
        5002, "ROW_NOT_FOUND", 404,
        "Row not found",
        "That line item could not be found.",
    )
    MAPPING_INVALID = ErrorDef(
        5003, "MAPPING_INVALID", 400,
        "Invalid invoice mapping",
        "Those invoice numbers couldn't be mapped to this row. Check them and try again.",
    )
    REMITTANCE_RECHECK_FAILED = ErrorDef(
        5004, "REMITTANCE_RECHECK_FAILED", 400,
        "Recheck failed",
        "We couldn't recheck this row against remittances right now.",
    )

    # ── 6000-6999  Oracle / Fusion ───────────────────────────────────────────
    ORACLE_POST_FAILED = ErrorDef(
        6000, "ORACLE_POST_FAILED", 502,
        "Oracle post failed",
        "We couldn't post this receipt to Oracle Fusion. It's been logged — you can retry from the row detail.",
        severity=Severity.HEAVY,
    )
    ORACLE_UNAVAILABLE = ErrorDef(
        6001, "ORACLE_UNAVAILABLE", 502,
        "Oracle unavailable",
        "Oracle Fusion isn't reachable right now. Please try again shortly.",
        severity=Severity.HEAVY,
    )
    ORACLE_AUTH_FAILED = ErrorDef(
        6002, "ORACLE_AUTH_FAILED", 502,
        "Oracle authentication failed",
        "We couldn't authenticate with Oracle Fusion. Contact an administrator.",
        severity=Severity.HEAVY,
    )

    # ── 7000-7999  Results / Metrics / Aging ─────────────────────────────────
    RECORD_NOT_FOUND = ErrorDef(
        7000, "RECORD_NOT_FOUND", 404,
        "Record not found",
        "That record could not be found.",
    )
    EXPORT_FAILED = ErrorDef(
        7001, "EXPORT_FAILED", 500,
        "Export failed",
        "We couldn't generate that export. Please try again.",
        severity=Severity.HEAVY,
    )
    FILTER_RANGE_INVALID = ErrorDef(
        7002, "FILTER_RANGE_INVALID", 400,
        "Invalid filter",
        "That date range or filter combination isn't valid.",
    )

    # ── 8000-8999  Admin / Users ──────────────────────────────────────────────
    USER_NOT_FOUND = ErrorDef(
        8000, "USER_NOT_FOUND", 404,
        "User not found",
        "That user could not be found.",
    )
    USER_EMAIL_INVALID = ErrorDef(
        8001, "USER_EMAIL_INVALID", 400,
        "Invalid email",
        "A valid email address is required.",
    )
    USER_ALREADY_EXISTS = ErrorDef(
        8002, "USER_ALREADY_EXISTS", 409,
        "User already exists",
        "A user with that email already exists.",
    )
    ROLE_UNKNOWN = ErrorDef(
        8003, "ROLE_UNKNOWN", 400,
        "Unknown role",
        "That role doesn't exist.",
    )
    USER_NO_ROLES_ASSIGNED = ErrorDef(
        8004, "USER_NO_ROLES_ASSIGNED", 400,
        "At least one role is required",
        "Assign at least one role — a user with no roles at all can't access anything (use Viewer if that's intended).",
    )

    # ── 9000-9999  System / Validation / Unexpected ─────────────────────────
    VALIDATION_FAILED = ErrorDef(
        9000, "VALIDATION_FAILED", 422,
        "Invalid request",
        "The request was invalid. Check the highlighted fields and try again.",
    )
    UNEXPECTED_ERROR = ErrorDef(
        9001, "UNEXPECTED_ERROR", 500,
        "Something went wrong",
        "An unexpected error occurred while processing your request. It's been logged — please try again, and contact support if it keeps happening.",
        severity=Severity.HEAVY,
    )
    # Fallback codes for plain HTTPException(status, "...") call sites that
    # have not yet been migrated to a specific AppError(ErrorCode....) — see
    # common/errors.py. Keeps the "no message outside the defined set" rule
    # true everywhere, even before every call site is individually migrated.
    GENERIC_BAD_REQUEST = ErrorDef(9400, "GENERIC_BAD_REQUEST", 400, "Invalid request", "The request could not be completed.")
    GENERIC_NOT_SIGNED_IN = ErrorDef(9401, "GENERIC_NOT_SIGNED_IN", 401, "Not signed in", "You need to sign in to continue.")
    GENERIC_NOT_ALLOWED = ErrorDef(9403, "GENERIC_NOT_ALLOWED", 403, "Not allowed", "You don't have permission to do that.")
    GENERIC_NOT_FOUND = ErrorDef(9404, "GENERIC_NOT_FOUND", 404, "Not found", "That item could not be found.")
    GENERIC_CONFLICT = ErrorDef(9409, "GENERIC_CONFLICT", 409, "Conflict", "That couldn't be completed because of a conflict with the current state.")
    GENERIC_FAILED = ErrorDef(9500, "GENERIC_FAILED", 500, "Request failed", "Something went wrong processing that request.", severity=Severity.HEAVY)

    _GENERIC_BY_STATUS = {
        400: GENERIC_BAD_REQUEST,
        401: GENERIC_NOT_SIGNED_IN,
        403: GENERIC_NOT_ALLOWED,
        404: GENERIC_NOT_FOUND,
        409: GENERIC_CONFLICT,
    }

    @classmethod
    def generic_for_status(cls, status_code: int) -> ErrorDef:
        return cls._GENERIC_BY_STATUS.get(status_code, cls.GENERIC_FAILED)

    @classmethod
    def all_defs(cls) -> list[ErrorDef]:
        return [v for v in vars(cls).values() if isinstance(v, ErrorDef)]

    @classmethod
    def by_number(cls, number: int) -> ErrorDef | None:
        for d in cls.all_defs():
            if d.code == number:
                return d
        return None


def _check_no_duplicate_codes() -> None:
    seen: dict[int, str] = {}
    for d in ErrorCode.all_defs():
        if d.code in seen:
            raise RuntimeError(f"Duplicate error code {d.code}: {seen[d.code]} vs {d.name}")
        seen[d.code] = d.name


_check_no_duplicate_codes()
