"""
app.tasks.analysis_tasks
==========================
Procrastinate task wrapping the existing rule_engine/orchestrator.py logic.
The orchestrator's actual analysis code (_run_analysis) is UNCHANGED — this
is purely a new way to invoke it (deferred job instead of a bare thread),
plus the advisory-lock wrap described in design doc §4.
"""
from __future__ import annotations

from ..common.request_context import set_job_context
from .app import procrastinate_app


@procrastinate_app.task(name="run_analysis", retry=2)
def run_analysis_task(run_id: int, selected_files: list[str]) -> None:
    # No HTTP request exists for a background job, so every log line this
    # task (and anything it calls) emits is tagged with the run id instead
    # -- see common/request_context.py / common/logging_config.py. This is
    # what lets "why did run 482 fail" be answered with a log search.
    set_job_context(f"run:{run_id}")
    # Imported inside the task body (not at module load time) to avoid a
    # circular import between tasks/ and rule_engine/ at app startup.
    from ..rule_engine.orchestrator import _run_analysis_locked
    _run_analysis_locked(run_id, selected_files)
