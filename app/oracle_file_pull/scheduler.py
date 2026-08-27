"""
app.oracle_file_pull.scheduler
================================
Runs the Oracle file puller (puller.run_once) on a fixed daily schedule,
inside the backend process itself -- same architectural pattern as
app.aging.watcher and app.gl_rates.watcher (a background thread started
at app startup), rather than relying on OS-level cron. This keeps the
schedule visible in application logs and avoids a separate, easy-to-forget
crontab entry that a fresh VM stand-up wouldn't recreate automatically.

Timestamp-based dedupe (only download a file if its remote mtime changed
since the last successful pull) is unchanged -- that logic lives entirely
in puller.run_once() / the local state file it maintains. This module only
controls WHEN run_once() gets called; it does not touch that logic at all.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .puller import run_once
from ..db.settings import get_settings

logger = logging.getLogger("cashapply.oracle_file_pull.scheduler")

_scheduler: BackgroundScheduler | None = None


def _run_once_job() -> None:
    try:
        result = run_once()
        logger.info("[oracle_file_pull_scheduler] Scheduled run complete: %s", result)
    except Exception:
        # Same resilience as puller.run_loop() -- a failed run (e.g. the
        # SSH jump chain being down) must never crash the whole backend
        # process; log it and let tomorrow's scheduled run try again.
        logger.error("[oracle_file_pull_scheduler] Scheduled run failed", exc_info=True)


def start_oracle_file_pull_scheduler() -> None:
    """
    Call once at app startup (see main.py's other watcher-thread starts,
    e.g. aging_watcher / gl_rates_watcher).

    Fires daily at 09:30 server-local time. misfire_grace_time=3600 means
    if the backend process was down/restarting right at 09:30, the job
    still runs as soon as the process is back up, as long as it's within
    an hour of the scheduled time -- otherwise a routine restart around
    that time would silently skip that day's pull entirely.
    """
    global _scheduler
    if _scheduler is not None:
        logger.warning("[oracle_file_pull_scheduler] Already started -- ignoring duplicate start call.")
        return

    # get_settings() call kept here (even though unused directly) to mirror
    # the other watcher start_*() functions' shape, and so this is the
    # natural place to read a schedule-time override from settings later
    # if the fixed 09:30 ever needs to become configurable.
    get_settings()

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _run_once_job,
        trigger=CronTrigger(hour=9, minute=30),
        id="oracle_file_pull_daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("[oracle_file_pull_scheduler] Started -- daily run scheduled for 09:30.")