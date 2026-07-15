"""
app.common.errors
====================
Single place that defines how errors leave this API. Every error response
sent to the frontend has the SAME shape:

    { "title": "...", "message": "..." }

`title` is short (e.g. "Config not found"), `message` is one plain-English
sentence a non-developer can act on. Neither field is ever a raw
exception string, a SQLAlchemy dump, or a Python traceback — those go to
the server log only, via logger.exception(), where a developer can find
them by request id.

Usage in a route:
    from ..common.errors import AppError

    raise AppError(404, "Account not found", f"No account matches '{acct}'.")

For the "genuinely unexpected" case (anything not raised deliberately as
an AppError or a plain HTTPException), register_exception_handlers()
below catches it, logs the full traceback server-side, and returns a
generic-but-honest message instead of letting FastAPI/Starlette's default
500 body (or a bare str(exc)) reach the client.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("cashapply.errors")


class AppError(Exception):
    """Raise this instead of HTTPException(status, f"...{e}...") whenever a
    route wants to surface a clear, specific problem to the user — it
    forces a title and a message to be written separately, rather than
    letting one long f-string with an embedded exception do both jobs."""

    def __init__(self, status_code: int, title: str, message: str):
        self.status_code = status_code
        self.title = title
        self.message = message
        super().__init__(message)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"title": exc.title, "message": exc.message},
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        # Plain HTTPException(status, "some message") calls throughout the
        # codebase still work — wrap them in the same {title, message}
        # shape so the frontend never has to special-case which kind of
        # error it got. detail is still developer-written, short, and
        # deliberate at each raise site (never str(exc) directly for a
        # caught unexpected exception — that's the handler below).
        default_titles = {400: "Invalid request", 401: "Not signed in",
                           403: "Not allowed", 404: "Not found", 409: "Conflict"}
        title = default_titles.get(exc.status_code, "Request failed")
        return JSONResponse(
            status_code=exc.status_code,
            content={"title": title, "message": str(exc.detail)},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        # FastAPI raises this itself (before any route code runs) when the
        # request body doesn't match the endpoint's pydantic model — e.g. a
        # required field like ou_number/business_unit was left out. Turn its
        # field-level error list into one readable sentence instead of
        # letting it fall through to the generic 500 handler below.
        parts = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err.get("loc", []) if p != "body") or "request"
            parts.append(f"{field}: {err.get('msg', 'invalid value')}")
        return JSONResponse(
            status_code=422,
            content={"title": "Invalid request", "message": "; ".join(parts) or "The request was invalid."},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        # The catch-all. Anything reaching here was NOT anticipated by the
        # route — log it in full (traceback included) server-side, and
        # give the user a short, honest, non-technical message instead of
        # the stack trace / SQLAlchemy dump / etc that `str(exc)` would be.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "title": "Something went wrong",
                "message": (
                    "An unexpected error occurred while processing your request. "
                    "It's been logged — please try again, and contact support if "
                    "it keeps happening."
                ),
            },
        )
