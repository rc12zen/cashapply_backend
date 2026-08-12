"""
app.tasks.worker
==================
Run this as a separate process alongside the FastAPI app:

    python -m app.tasks.worker

This is the "one new process" referenced in the design doc — no new
infrastructure (broker/datastore), just a second process reading the same
Postgres DATABASE_URL as the API, via procrastinate's SELECT ... FOR UPDATE
SKIP LOCKED polling.

(The Windows event-loop-policy fix needed for psycopg's async pool lives in
tasks/app.py, not here — see that file's comment for why it has to be
there rather than in this file.)
"""
from __future__ import annotations

from ..common.logging_config import configure_logging
configure_logging()  # must run before any task module import that grabs a logger at load time

from ..common.tls_trust import configure_tls_trust
configure_tls_trust()  # must run before any task opens an HTTPS connection — see common/tls_trust.py

# Registers the tasks on procrastinate_app via their @procrastinate_app.task
# decorators — required so the worker knows about them.
from . import analysis_tasks, ingestion_tasks  # noqa: F401
from .app import procrastinate_app

if __name__ == "__main__":
    # run_worker() opens the app (async) and runs the event loop internally —
    # no separate with_sync_connector() call needed in this procrastinate version.
    #
    # listen_notify=True uses Postgres LISTEN/NOTIFY so new jobs are picked
    # up instantly instead of on a polling interval.
    #
    # install_signal_handlers=False — Windows' asyncio does NOT implement
    # loop.add_signal_handler() at all (NotImplementedError, regardless of
    # which event loop policy is active — this is a separate Windows
    # platform limitation, not fixed by the event-loop-policy change in
    # tasks/app.py). Procrastinate tries to install a Ctrl+C/SIGINT handler
    # by default for graceful shutdown; disabling that here avoids the crash
    # on Windows. Ctrl+C still stops the process — it just does a harder
    # stop (a job that's mid-execution may not finish cleanly) rather than
    # procrastinate's graceful drain. Safe to set back to True on Linux/macOS
    # if you want graceful-shutdown behavior there.
    procrastinate_app.run_worker(listen_notify=True, install_signal_handlers=False)