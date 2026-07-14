"""
app.tasks.remittance_recheck_worker
======================================
Run this as its own process, alongside the FastAPI app and the
procrastinate worker (tasks/worker.py):

    python -m app.tasks.remittance_recheck_worker

Every REMITTANCE_RECHECK_INTERVAL_SECONDS (see db/settings.py), scans
every LineItem still sitting in needs_remittance (rule_id R7) and checks
whether a remittance persisted by App2 (cashapply-remittance-agent) now
matches it — see rule_engine/remittance_recheck.py for the actual logic
and the reasoning behind it.

Kept as a plain interval loop (not a procrastinate periodic/cron task)
deliberately: this needs "run every N seconds" from a plain env var,
not a cron schedule, and a single dedicated loop is simpler to reason
about than translating an arbitrary second-count into a cron expression
(procrastinate's periodic tasks are cron-based, minute granularity).
This process is independent of the procrastinate worker — it never
defers a job, it just wakes up, does a bounded scan, and sleeps again.
"""
from __future__ import annotations

import logging
import time

from ..common.logging_config import configure_logging
configure_logging()

from ..db.session import session_scope
from ..db.settings import get_settings
from ..rule_engine.remittance_recheck import recheck_needs_remittance_rows

logger = logging.getLogger("cashapply.remittance_recheck_worker")


def run_once() -> dict:
    with session_scope() as db:
        result = recheck_needs_remittance_rows(db)
    if result.get("error"):
        logger.warning("[remittance_recheck] %s", result["error"])
    else:
        logger.info(
            "[remittance_recheck] checked=%d changed=%d",
            result.get("checked", 0), result.get("changed", 0),
        )
        for res in result.get("results", []):
            if res.get("changed"):
                logger.info(
                    "[remittance_recheck] row=%s %s -> %s (category=%s)",
                    res["id"], res.get("from_rule_id"), res.get("to_rule_id"), res.get("to_category"),
                )
    return result


def main():
    settings = get_settings()
    interval = settings.REMITTANCE_RECHECK_INTERVAL_SECONDS
    logger.info(
        "remittance_recheck_worker starting. interval=%ss (REMITTANCE_RECHECK_INTERVAL_SECONDS)",
        interval,
    )
    try:
        while True:
            try:
                run_once()
            except Exception:
                # A single bad cycle (e.g. transient DB hiccup) should never
                # kill the whole long-lived worker process — log it and try
                # again next interval, same philosophy as the procrastinate
                # worker's own per-job error isolation.
                logger.exception("remittance_recheck_worker cycle failed")
            time.sleep(interval)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()