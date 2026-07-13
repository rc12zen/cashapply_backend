"""
app.audit.middleware
======================
Generic request-level activity logging. Catches "view"/"download"-style
events for free so every route doesn't need an explicit log_activity() call.
Domain-significant events (approve/reject/oracle post/role change) still log
explicitly at their call site with real entity context — see design doc §6.

Skips logging the noisy, low-value paths (health check, static assets,
OpenAPI docs) so the activity log stays about actual user actions.
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
