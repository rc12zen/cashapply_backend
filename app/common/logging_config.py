"""
app.common.logging_config
============================
Configures Python's logging module for this app -- console output (so logs
show in the same terminal you already watch) AND a rotating-by-DAY text
file per day (so you have a durable, searchable, per-day history even
after your terminal scrollback is gone or the process restarts).

WHY THIS EXISTS:
The app has logger.info()/logger.warning() calls scattered across
oracle/http_debug_log.py, rule_engine/fx_service.py, extraction/layer_2b_ai.py,
oracle/receipt_creation.py, hitl/service.py, etc. -- but nothing anywhere
ever called logging.basicConfig() or attached a handler. Python's logging
module defaults to WARNING level with no handler configured on the root
logger, so every one of those logger.info() calls was being silently
dropped -- not printed to console, not written anywhere. The Oracle
request/response logging in particular (log_oracle_request/
log_oracle_response) was fully wired up and completely invisible the
entire time.

USAGE: call configure_logging() once, as early as possible, in BOTH
entrypoints -- app/main.py (API process) and app/tasks/worker.py (worker
process) -- since receipt creation and invoice mapping both run in the
worker, not the API.

LOG_LEVEL env var controls verbosity (DEBUG or INFO; default INFO). DEBUG
shows everything a developer would want mid-incident; INFO is the quieter,
day-to-day default -- see "everyday vs genuine failure" logging split in
common/errors.py, which is independent of this (a HEAVY error is always
logged in full regardless of LOG_LEVEL; LOG_LEVEL only controls how much
routine/diagnostic chatter besides errors gets written).

ONE .txt FILE PER CALENDAR DAY, in LOG_DIR (default ./logs), named
app-YYYY-MM-DD.txt, rotating at midnight local time. This is intentionally
plain text (not JSON) for now -- see the note below about upgrading to
structured logs later if a log-aggregation tool is adopted.

TRACING: every log line includes a `[ref]` tag -- the current request's
short trace id (see common/request_context.py), or a background job's
run/job reference, or "-" if neither is set. This is what lets a user-
reported error (which shows the same ref/request_id) be found in the log
file by a simple text search.

SENSITIVE VALUES: a logging Filter (_RedactSensitiveFilter) scrubs common
secret-shaped substrings (password=..., token=..., api key=..., Bearer ...)
out of every formatted line before it's written anywhere, console or file.
This is a best-effort safety net, not a substitute for not logging secrets
in the first place -- don't rely on it to log a raw credentials blob "because
it'll get redacted anyway". 
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

from .request_context import get_trace_ref

_configured = False

# ── Sensitive-value redaction ────────────────────────────────────────────────
# Matches key=value / key: value / key "value" shapes for common secret
# field names, and bare "Bearer <token>" headers. Best-effort, not exhaustive
# -- the real control is "don't log secrets", this is a safety net on top.
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|"
    r"authorization)(\s*[:=]\s*)([\"']?)([^\s\"',}]{3,})([\"']?)"
)
_BEARER_PATTERN = re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9\-._~+/]{8,}=*)")


def _redact(text: str) -> str:
    text = _SECRET_KEY_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***REDACTED***{m.group(5)}", text)
    text = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)}***REDACTED***", text)
    return text


class _RedactSensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact(str(record.getMessage()))
            record.args = ()
        except Exception:
            pass  # never let redaction itself break logging
        return True


class _TraceRefFilter(logging.Filter):
    """Injects the current request/job trace ref (see request_context.py)
    as `record.trace_ref`, so the formatter can include it on every line
    without every logger.info() call having to pass it explicitly."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_ref = get_trace_ref() or "-"
        return True


def configure_logging() -> None:
    global _configured
    if _configured:
        return  # idempotent -- safe to call from multiple entrypoints/modules
    _configured = True

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    if level_name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level_name = "INFO"
    level = getattr(logging, level_name, logging.INFO)

    log_dir = Path(os.environ.get("LOG_DIR", "./logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    # Base filename -- TimedRotatingFileHandler appends the date suffix
    # itself (see `suffix` below) and writes one plain-text file per day,
    # e.g. logs/app.txt.2026-07-17. `.txt` extension is kept on the base
    # name so today's active file is also clearly a text file while it's
    # being written.
    log_file = log_dir / "app.txt"

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(trace_ref)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    trace_filter = _TraceRefFilter()
    redact_filter = _RedactSensitiveFilter()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(trace_filter)
    console_handler.addFilter(redact_filter)

    # One file per calendar day (midnight rollover), kept as plain .txt --
    # per the requirement: "log file will be saved as txt as file per day
    # in directory". backupCount=365 keeps roughly a year of daily history
    # before old files are removed; raise/lower via LOG_RETENTION_DAYS.
    retention_days = int(os.environ.get("LOG_RETENTION_DAYS", "365"))
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=retention_days,
        encoding="utf-8", utc=False,
    )
    file_handler.suffix = "%Y-%m-%d.txt"
    file_handler.setFormatter(formatter)
    file_handler.addFilter(trace_filter)
    file_handler.addFilter(redact_filter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # uvicorn installs its own console-only handlers on these loggers with
    # propagate=False, so without this their startup banner + access lines
    # (and the app's own uvicorn.error startup warnings in main.py) never
    # reach the root file/console handlers above. Drop uvicorn's own handlers
    # and let the records bubble to root -> app.txt + console. Safe when
    # uvicorn isn't running: these loggers just have no records to emit.
    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _ulog = logging.getLogger(_name)
        _ulog.handlers.clear()
        _ulog.propagate = True

    logging.getLogger(__name__).info(
        "Logging configured: level=%s, dir=%s (one .txt file per day, %s-day retention)",
        level_name, log_dir.resolve(), retention_days,
    )
