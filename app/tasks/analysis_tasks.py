"""
app.tasks.analysis_tasks
==========================
Procrastinate task wrapping the existing rule_engine/orchestrator.py logic.
The orchestrator's actual analysis code (_run_analysis) is UNCHANGED — this
is purely a new way to invoke it (deferred job instead of a bare thread),
plus the advisory-lock wrap described in design doc §4.
"""
from __future__ import annotations

from .app import procrastinate_app


@procrastinate_app.task(name="run_analysis", retry=2)
def run_analysis_task(run_id: int, selected_files: list[str]) -> None:
    # Imported inside the task body (not at module load time) to avoid a
    # circular import between tasks/ and rule_engine/ at app startup.
    from ..rule_engine.orchestrator import _run_analysis_locked
    _run_analysis_locked(run_id, selected_files)
