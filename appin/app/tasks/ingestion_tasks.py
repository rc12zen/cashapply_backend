"""
app.tasks.ingestion_tasks
===========================
Procrastinate task for the background parse/dedupe step of an upload.
See design doc §0 / §4 and ingestion/ingest_service.py.
"""
from __future__ import annotations

from .app import procrastinate_app


@procrastinate_app.task(name="ingest_statement", retry=2)
def ingest_statement_task(source_file_id: int) -> None:
    from ..db.session import session_scope
    from ..ingestion.ingest_service import ingest_and_parse

    with session_scope() as db:
        ingest_and_parse(db, source_file_id)
