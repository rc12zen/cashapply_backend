"""
app.main
=========
FastAPI entrypoint for cashapply-backend.
All routes are registered under /api/* prefixes that exactly mirror
the existing frontend lib/api.ts contract — payload shapes inside each
handler are free to change; the URL + method must not.

UPDATED — auth / RBAC / duplicate-detection / audit-logging integration.
See cashapply-platform-hardening-design.md for the full design.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .common.logging_config import configure_logging
configure_logging()  # must run before any other app import that grabs a logger at module load time

from .db.session import init_db
from .db.settings import get_settings
from .aging.watcher import start_watcher
from .gl_rates.watcher import start_gl_rates_watcher
from .common.errors import register_exception_handlers
from .common.request_context import RequestIdMiddleware
from .bff import (
    run_routes, results_routes, hitl_routes, config_routes, filters_routes,
    executive_summary, config_builder_routes, auth_routes, admin_routes,
    activity_log_routes, ai_usage_routes, storage_routes, bank_accounts_routes,
    remittance_inbox_routes, settlement_identifier_routes,
)

app = FastAPI(title="CashApply Backend", version="1.1.0")

# Every error the frontend receives — deliberate (AppError/HTTPException) or
# genuinely unexpected — comes back as {"title": ..., "message": ...}. Raw
# tracebacks/exception dumps never leave the server; see common/errors.py.
register_exception_handlers(app)

_settings = get_settings()
_cors_origins = (
    ["*"] if _settings.CORS_ALLOWED_ORIGINS.strip() == "*"
    else [o.strip() for o in _settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,  # "*" for local dev; set CORS_ALLOWED_ORIGINS for UAT/prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every request gets a short trace reference (reused if the caller already
# sent one via X-Request-Id) -- echoed back as a response header AND inside
# every error body (see common/errors.py), so a user-reported error can be
# traced to its exact log entry. Registered AFTER CORSMiddleware so it runs
# on the inside of the middleware stack (Starlette applies middleware in
# reverse registration order) and still sees every request, including ones
# CORS would otherwise short-circuit as a preflight.
app.add_middleware(RequestIdMiddleware)

# PATCH: the generic per-request ActivityLogMiddleware (design doc §6) has
# been REMOVED — it wrote one ActivityLog row for every single GET/POST/PUT/
# DELETE/PATCH request (page loads, status polls, list calls, ...), which
# is exactly what was ballooning the table's storage with low-value "System
# Logs" noise. Domain-significant events (run start, approve/reject, oracle
# post, upload, config create, manual invoice mapping, role change, ...)
# already log explicitly via log_activity() at their own call sites — that
# coverage is unaffected. See app/bff/activity_log_routes.py's
# purge-system-logs endpoint for a one-off cleanup of rows this middleware
# already wrote in existing environments.


@app.on_event("startup")
def on_startup():
    settings = get_settings()
    init_db()   # create_all() — includes every new auth/RBAC/audit/dedup table.
    start_watcher()   # Auto-detect aging reports from AGING_WATCH_FOLDER
    start_gl_rates_watcher()   # Auto-detect GL Daily Rates files from GL_RATES_WATCH_FOLDER -- see gl_rates/watcher.py

    # Procrastinate's sync connector does NOT lazily auto-open its pool —
    # every task.defer() call (run_routes.py's /start and /upload) would
    # raise AppNotOpen without this. Opened once here, for the lifetime of
    # the API process; closed on shutdown below. The worker process
    # (app.tasks.worker) opens its own instance separately via
    # run_worker() — that path already handles opening itself.
    from .tasks.app import procrastinate_app
    procrastinate_app.open()

    if settings.APP_ENV == "local" and not settings.AZURE_TENANT_ID:
        import logging
        logging.getLogger("uvicorn.error").warning(
            "AZURE_TENANT_ID / AZURE_CLIENT_ID not set — Azure SSO token "
            "validation will fail on any real Bearer token. This is fine "
            "for local testing via the X-Dev-User bypass header (see "
            "README_SETUP_AND_TESTING.md), but must be configured before "
            "any deployment reachable outside your own machine."
        )

    if settings.APP_ENV != "local" and not settings.AZURE_TENANT_ID:
        import logging
        logging.getLogger("uvicorn.error").error(
            "APP_ENV=%s but AZURE_TENANT_ID/AZURE_CLIENT_ID are not set, and "
            "the X-Dev-User bypass is disabled outside APP_ENV=local — every "
            "single request will 401 until real Azure AD SSO is configured. "
            "This is not a storage issue (STORAGE_BACKEND can still be "
            "'local'); it's specifically that no sign-in method is usable "
            "right now.",
            settings.APP_ENV,
        )


@app.on_event("shutdown")
def on_shutdown():
    from .tasks.app import procrastinate_app
    procrastinate_app.close()


@app.get("/health")
def health():
    return {"status": "ok"}


# ── BFF routes — all UI-facing endpoints ─────────────────────────────────────
app.include_router(run_routes.router,     prefix="/api/run",     tags=["run"])
app.include_router(results_routes.router, prefix="/api/results", tags=["results"])
app.include_router(hitl_routes.router,    prefix="/api/hitl",    tags=["hitl"])
app.include_router(config_routes.router,         prefix="/api/config",           tags=["config"])
app.include_router(config_builder_routes.router, prefix="/api/config",           tags=["config-builder"])  # Bank Data Ingestion Layer
app.include_router(filters_routes.router, prefix="/api/filters", tags=["filters"])
app.include_router(executive_summary.router, prefix="/api/executive-summary", tags=["executive-summary"])

# ── Auth / RBAC / Audit (new) ─────────────────────────────────────────────────
app.include_router(auth_routes.router,         prefix="/api/auth",          tags=["auth"])
app.include_router(admin_routes.router,        prefix="/api/admin",         tags=["admin"])
app.include_router(activity_log_routes.router, prefix="/api/activity-log",  tags=["activity-log"])
app.include_router(ai_usage_routes.router,     prefix="/api/ai-usage",      tags=["ai-usage"])
app.include_router(storage_routes.router,      prefix="/api/storage",       tags=["storage"])
app.include_router(bank_accounts_routes.router, prefix="/api/bank-accounts", tags=["bank-accounts"])
app.include_router(settlement_identifier_routes.router, prefix="/api/bank-accounts", tags=["settlement-identifiers"])
app.include_router(remittance_inbox_routes.router, prefix="/api/remittance-inbox", tags=["remittance-inbox"])