"""
app.tasks.ingestion_tasks
===========================
Procrastinate task for the background parse/dedupe step of an upload.
See design doc §0 / §4 and ingestion/ingest_service.py.
"""
from __future__ import annotations

from ..common.request_context import set_job_context
from .app import procrastinate_app


@procrastinate_app.task(name="ingest_statement", retry=2)
def ingest_statement_task(source_file_id: int) -> None:
    set_job_context(f"ingest:{source_file_id}")
    from ..db.session import session_scope
    from ..ingestion.ingest_service import ingest_and_parse

    with session_scope() as db:
        ingest_and_parse(db, source_file_id)
