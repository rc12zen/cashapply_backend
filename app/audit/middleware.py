"""
app.audit.middleware
======================
DEPRECATED — no longer registered (see app/main.py). This used to catch
every GET/POST/PUT/DELETE/PATCH request and write one ActivityLog row per
call, which was the source of the "System Logs" bloat in the Activity Log
page (one row per page view, poll, or list call, growing without bound).
Domain-significant events (approve/reject/oracle post/role change/run
start/upload/config create/manual invoice mapping) still log explicitly at
their own call sites via audit.service.log_activity() — that's unaffected
by removing this. Left in place only for reference; do not re-register it.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..db.models import ActivityLog
from ..db.session import get_session_factory

_SKIP_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc")


class ActivityLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return response

        # Only log authenticated, state-relevant traffic (skip pure OPTIONS/etc).
        if request.method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            return response

        user = getattr(request.state, "current_user", None)
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else None
        )

        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            db.add(ActivityLog(
                user_id=user.id if user else None,
                action=f"{request.method} {path}",
                entity_type=None,
                entity_id=None,
                status="success" if response.status_code < 400 else "failure",
                ip_address=ip,
                log_metadata={"status_code": response.status_code},
            ))
            db.commit()
        except Exception:
            db.rollback()
            # Never let audit logging break the actual request.
        finally:
            db.close()

        return response