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
from .audit.middleware import ActivityLogMiddleware
from .bff import (
    run_routes, results_routes, hitl_routes, config_routes, filters_routes,
    executive_summary, config_builder_routes, auth_routes, admin_routes,
    activity_log_routes, ai_usage_routes,
)

app = FastAPI(title="CashApply Backend", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten to frontend origin in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Generic per-request activity logging (design doc §6). Registered AFTER
# CORSMiddleware so it still wraps every real request but doesn't interfere
# with preflight handling. Explicit log_activity() calls at domain-specific
# points (approve/reject/upload/role-change/...) still happen inside the
# route handlers themselves — this middleware only adds the generic
# view/list/download coverage those calls don't already provide.
app.add_middleware(ActivityLogMiddleware)


@app.on_event("startup")
def on_startup():
    settings = get_settings()
    init_db()   # create_all() — includes every new auth/RBAC/audit/dedup table.
    start_watcher()   # Auto-detect aging reports from AGING_WATCH_FOLDER

    # Procrastinate's sync connector does NOT lazily auto-open its pool —
    # every task.defer() call (run_routes.py's /start and /upload) would
    # raise AppNotOpen without this. Opened once here, for the lifetime of
    # the API process; closed on shutdown below. The worker process
    # (app.tasks.worker) opens its own instance separately via
    # run_worker() — that path already handles opening itself.
    from .tasks.app import procrastinate_app
    procrastinate_app.open()

    if settings.ENVIRONMENT == "local" and not settings.AZURE_TENANT_ID:
        import logging
        logging.getLogger("uvicorn.error").warning(
            "AZURE_TENANT_ID / AZURE_CLIENT_ID not set — Azure SSO token "
            "validation will fail on any real Bearer token. This is fine "
            "for local testing via the X-Dev-User bypass header (see "
            "README_SETUP_AND_TESTING.md), but must be configured before "
            "any deployment reachable outside your own machine."
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