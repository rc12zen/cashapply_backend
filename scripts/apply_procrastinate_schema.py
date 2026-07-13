"""
scripts/apply_procrastinate_schema.py
=======================================
One-time (per database) setup of procrastinate's own tables/functions
(procrastinate_jobs, procrastinate_events, etc.) — separate from your
business tables, needed before the worker or any .defer() call will work.

WHY THIS SCRIPT EXISTS instead of just running:
    procrastinate --app=app.tasks.app.procrastinate_app schema --apply

On Windows, that CLI command fails with repeated
"Psycopg cannot use the 'ProactorEventLoop'..." warnings. Root cause:
procrastinate's own CLI entrypoint (procrastinate/cli.py) calls
`asyncio.run(cli(...))` BEFORE it dynamically imports your --app module —
so by the time app/tasks/app.py's Windows event-loop-policy fix executes
(during that dynamic import), Windows has already committed to the broken
ProactorEventLoop for that run. Setting the policy afterward doesn't
retroactively fix an already-created loop.

This script sidesteps the problem entirely: SchemaManager.apply_schema()
(unlike the CLI) uses procrastinate's SYNC connector path — no asyncio.run()
involved at all — so the Windows event-loop issue never comes up.

Usage:
    python -m scripts.apply_procrastinate_schema
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from procrastinate.schema import SchemaManager  # noqa: E402

from app.tasks.app import procrastinate_app  # noqa: E402


def main() -> None:
    # SyncPsycopgConnector does NOT lazily auto-open its pool — it must be
    # explicitly opened first, or every call (including apply_schema) raises
    # AppNotOpen. Using the app itself as a context manager both opens and
    # guarantees it's cleanly closed afterward.
    with procrastinate_app.open():
        manager = SchemaManager(connector=procrastinate_app.connector)
        manager.apply_schema()
    print("procrastinate schema applied successfully.")


if __name__ == "__main__":
    main()