"""
app.common.logging_config
============================
Configures Python's logging module for this app — console output (so logs
show in the same terminal you already watch) AND a rotating file (so you
have a durable, searchable history even after your terminal scrollback is
gone or the process restarts).

WHY THIS EXISTS:
The app has logger.info()/logger.warning() calls scattered across
oracle/http_debug_log.py, rule_engine/fx_service.py, extraction/layer_2b_ai.py,
oracle/receipt_creation.py, hitl/service.py, etc. — but nothing anywhere
ever called logging.basicConfig() or attached a handler. Python's logging
module defaults to WARNING level with no handler configured on the root
logger, so every one of those logger.info() calls was being silently
dropped — not printed to console, not written anywhere. The Oracle
request/response logging in particular (log_oracle_request/
log_oracle_response) was fully wired up and completely invisible the
entire time.

USAGE: call configure_logging() once, as early as possible, in BOTH
entrypoints — app/main.py (API process) and app/tasks/worker.py (worker
process) — since receipt creation and invoice mapping both run in the
worker, not the API.

LOG_LEVEL env var controls verbosity (default INFO). Set to DEBUG for
maximum detail, WARNING to quiet things down.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return  # idempotent — safe to call from multiple entrypoints/modules
    _configured = True

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_dir = Path(os.environ.get("LOG_DIR", "./logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 10MB per file, keep 5 — durable history without growing unbounded.
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logging.getLogger(__name__).info(
        "Logging configured: level=%s, file=%s", level_name, log_file.resolve(),
    )