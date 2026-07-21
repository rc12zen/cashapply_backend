"""
app.common.request_context
=============================
Correlates one user-visible error to one exact log entry, per the logging
requirement: "every request carries a short reference that appears both in
what the user sees and in the logs, so any reported error can be traced to
its exact log entry." Background jobs have no request, so they use a job/
run reference instead — same mechanism, different source.

Usage:
    - HTTP requests: RequestIdMiddleware (registered in main.py) generates
      or reuses an id per request, stores it in a contextvar, and echoes it
      back as the `X-Request-Id` response header AND inside every error body
      (see common/errors.py) as `request_id`.
    - Background jobs (procrastinate tasks / worker processes): call
      set_job_context(job_ref) at the top of the task, so every log line
      emitted during that task carries the same reference (see
      common/logging_config.py's ContextFilter, which reads this contextvar).
"""
from __future__ import annotations

import contextvars
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_trace_ref: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_ref", default=None)

REQUEST_ID_HEADER = "X-Request-Id"


def new_trace_ref() -> str:
    return uuid.uuid4().hex[:12]


def get_trace_ref() -> str | None:
    return _trace_ref.get()


def set_trace_ref(ref: str | None) -> None:
    _trace_ref.set(ref)


def set_job_context(job_ref: str) -> None:
    """Call at the top of a background task/job so every log line it emits
    (via the standard `logging` module) carries this run/job reference —
    the equivalent of a request id for work that has no HTTP request."""
    _trace_ref.set(f"job:{job_ref}")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assigns (or reuses, if the caller already sent one) a short trace
    reference to every request, stores it for the duration of the request
    (so logger calls anywhere in the call stack can pick it up — see
    logging_config.ContextFilter), and echoes it back as a response header
    and inside error bodies."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        ref = incoming.strip() if incoming else new_trace_ref()
        set_trace_ref(ref)
        request.state.trace_ref = ref
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = ref
        return response
