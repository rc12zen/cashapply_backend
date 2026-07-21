"""
app.common.errors
====================
Single place that defines how errors leave this API. Every error response
sent to the frontend has the SAME shape:

    { "code": 5000, "title": "...", "message": "...", "request_id": "a1b2c3d4e5f6" }

`code` is the stable number+name from common/error_codes.py (a support
ticket can quote it). `title` is short, `message` is one plain-English
sentence a non-developer can act on. Neither title nor message is ever a
raw exception string, a SQLAlchemy dump, or a Python traceback -- those go
to the server log only, tagged with the same `request_id`, where a
developer can find them.

No user-facing error message exists outside app.common.error_codes.ErrorCode
-- see that module's docstring for the numbering scheme and how to add one.

Usage in a route:
    from ..common.errors import AppError
    from ..common.error_codes import ErrorCode

    raise AppError(ErrorCode.ACCOUNT_UNRESOLVED, detail=f"Account '{acct}' has no OU mapping.")

`detail`, if given, is appended to the code's canned message -- keep it
short and non-technical; it's still shown to the user.

For the "genuinely unexpected" case (anything not raised deliberately as
an AppError or a plain HTTPException), register_exception_handlers()
below catches it, logs the full traceback server-side (ALWAYS in full,
regardless of the Debug/Info verbosity setting -- see logging_config.py),
and returns a generic-but-honest message instead of letting FastAPI/
Starlette's default 500 body (or a bare str(exc)) reach the client.

Any plain HTTPException(status, "...") still in the codebase (not yet
migrated to a specific AppError(ErrorCode...)) is mapped onto a generic,
still-defined code for that status (see ErrorCode.generic_for_status) --
so the "no message outside the defined set" rule holds everywhere, even
before every individual raise site is migrated to a precise, domain-
specific code.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .error_codes import ErrorCode, ErrorDef, Severity
from .request_context import get_trace_ref

logger = logging.getLogger("cashapply.errors")


class AppError(Exception):
    """Raise this instead of HTTPException(status, f"...{e}...") whenever a
    route wants to surface a clear, specific, LOOKED-UP problem to the user.
    Every AppError is backed by an ErrorDef from common/error_codes.py, so
    the code/title/message/severity are all defined in one place rather
    than invented ad hoc at the raise site.

    Preferred (new) call style:
        raise AppError(ErrorCode.ACCOUNT_UNRESOLVED, detail="account '1234'")

    Legacy call style — still supported for call sites not yet migrated to
    a specific ErrorCode entry (`raise AppError(422, "Title", "Message")`).
    These are mapped onto the generic, still-defined code for that HTTP
    status (see ErrorCode.generic_for_status) so they keep their exact
    existing title/message (no behavior change) while still getting a
    code, a request_id, and a severity-correct log line. New call sites
    should add a proper ErrorDef to error_codes.py instead of using this
    form.
    """

    def __init__(self, error_def_or_status: ErrorDef | int, detail: str | None = None,
                 message: str | None = None):
        if isinstance(error_def_or_status, ErrorDef):
            error_def = error_def_or_status
            final_message = error_def.message if not detail else f"{error_def.message} ({detail})"
            title = error_def.title
        else:
            # Legacy form: (status_code, title, message) -- title is passed
            # positionally as the 2nd arg, so `detail` here actually holds
            # it in that call shape.
            status_code = error_def_or_status
            title = detail or "Request failed"
            final_message = message or "The request could not be completed."
            base = ErrorCode.generic_for_status(status_code)
            # Keep the caller's own status code (legacy sites sometimes use a
            # status not covered by the generic map, e.g. 422) rather than
            # silently overriding it.
            error_def = ErrorDef(base.code, base.name, status_code, title, final_message, base.severity)
            detail = None

        self.error_def = error_def
        self.detail = detail
        self.status_code = error_def.http_status
        self.title = title
        self.message = final_message
        super().__init__(final_message)


def _log_for(error_def: ErrorDef, context: str, exc: BaseException | None = None) -> None:
    ref = get_trace_ref() or "-"
    if error_def.severity == Severity.HEAVY:
        # Genuine failure -- full detail, traceback included, regardless of
        # LOG_LEVEL (heavy errors are never downgraded to a quiet log line).
        logger.error(
            "[%s] %s (code=%s) - %s", ref, error_def.name, error_def.code, context,
            exc_info=exc is not None,
        )
    else:
        # Everyday, expected condition -- one short line, no traceback.
        logger.info("[%s] %s (code=%s) - %s", ref, error_def.name, error_def.code, context)


def _error_body(error_def: ErrorDef, title: str, message: str) -> dict:
    return {
        "code": error_def.code,
        "title": title,
        "message": message,
        "request_id": get_trace_ref(),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        _log_for(exc.error_def, f"{request.method} {request.url.path} - {exc.detail or exc.message}")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.error_def, exc.title, exc.message),
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        # Plain HTTPException(status, "some message") calls throughout the
        # codebase still work -- mapped onto a generic-but-defined code for
        # that status so every response still comes from the registry (see
        # module docstring). detail is still developer-written, short, and
        # deliberate at each raise site (never str(exc) directly for a
        # caught unexpected exception -- that's the handler below).
        error_def = ErrorCode.generic_for_status(exc.status_code)
        message = str(exc.detail) if exc.detail else error_def.message
        _log_for(error_def, f"{request.method} {request.url.path} - {message}")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(error_def, error_def.title, message),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        # FastAPI raises this itself (before any route code runs) when the
        # request body doesn't match the endpoint's pydantic model -- e.g. a
        # required field like ou_number/business_unit was left out. Turn its
        # field-level error list into one readable sentence instead of
        # letting it fall through to the generic 500 handler below.
        parts = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err.get("loc", []) if p != "body") or "request"
            parts.append(f"{field}: {err.get('msg', 'invalid value')}")
        message = "; ".join(parts) or ErrorCode.VALIDATION_FAILED.message
        _log_for(ErrorCode.VALIDATION_FAILED, f"{request.method} {request.url.path} - {message}")
        return JSONResponse(
            status_code=422,
            content=_error_body(ErrorCode.VALIDATION_FAILED, ErrorCode.VALIDATION_FAILED.title, message),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        # The catch-all. Anything reaching here was NOT anticipated by the
        # route -- log it in full (traceback included) server-side, and
        # give the user a short, honest, non-technical message instead of
        # the stack trace / SQLAlchemy dump / etc that `str(exc)` would be.
        ref = get_trace_ref() or "-"
        logger.exception("[%s] Unhandled error on %s %s", ref, request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=_error_body(ErrorCode.UNEXPECTED_ERROR, ErrorCode.UNEXPECTED_ERROR.title, ErrorCode.UNEXPECTED_ERROR.message),
        )
