"""
app.tasks.app
==============
Procrastinate task-queue app. Postgres-backed (via SELECT ... FOR UPDATE
SKIP LOCKED) — no Redis, no RabbitMQ, no Celery. See design doc §5 and the
"why not Celery" discussion in the accompanying chat thread: Celery always
needs a separate broker (Redis/RabbitMQ); its Postgres transport is
polling-based and explicitly not the recommended path. Procrastinate is
built for Postgres as a first-class broker.

Run the worker with:
    python -m app.tasks.worker
Apply procrastinate's own schema (one-time, per database) with:
    python -m procrastinate --app=app.tasks.app.procrastinate_app schema --apply
"""
from __future__ import annotations

import re
import sys

# ── Windows fix — must run before anything else touches asyncio ────────────
# Windows' default event loop (ProactorEventLoop) doesn't support the
# add_reader/add_writer calls psycopg's async connector needs for its
# connection pool. Switch to the selector-based loop policy, which supports
# them. No-op on Linux/macOS (they already default to a selector loop).
#
# This lives HERE (not just in tasks/worker.py) because this module is the
# actual import target for every entrypoint that touches procrastinate:
#   - `python -m app.tasks.worker`                        (the worker process)
#   - `python -m procrastinate --app=app.tasks.app...`     (the schema/CLI tool)
#   - FastAPI routes calling task.defer() synchronously    (via app.main import chain)
# Putting the fix only in worker.py leaves the CLI tool broken, which is
# exactly the error that prompted this fix.
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from procrastinate import App, PsycopgConnector

from ..db.settings import get_settings


def _to_psycopg_conninfo(sqlalchemy_url: str) -> str:
    """
    DATABASE_URL is in SQLAlchemy form (postgresql+psycopg://user:pass@host/db
    or postgresql+psycopg2://...). Procrastinate's PsycopgConnector wants a
    plain libpq conninfo string (postgresql://user:pass@host/db) — strip the
    '+driver' part only.
    """
    return re.sub(r"^postgresql\+\w+://", "postgresql://", sqlalchemy_url)


settings = get_settings()

procrastinate_app = App(
    # kwargs={} (NOT omitted) is required here — some psycopg_pool versions
    # leave AsyncConnectionPool.kwargs as None when it's not passed at all,
    # and procrastinate's listen_notify() does `**self.pool.kwargs` when it
    # opens its dedicated LISTEN/NOTIFY connection, which raises
    # `TypeError: ... argument after ** must be a mapping, not NoneType`
    # the moment the worker starts (see procrastinate's own "Instantiate
    # your connector" docs, which always show kwargs={...} explicitly).
    connector=PsycopgConnector(
        conninfo=_to_psycopg_conninfo(settings.DATABASE_URL),
        kwargs={},
    ),
)