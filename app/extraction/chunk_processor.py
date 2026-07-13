"""
app.extraction.chunk_processor
================================
Splits a list of CreditRowSchema items into fixed-size chunks and
dispatches each chunk in parallel using ThreadPoolExecutor.

Each chunk flows through:
    Layer 2A (regex)  →  Layer 2B (AI fallback)  →  merger
    → ExtractionResultSchema

The orchestrator calls dispatch_chunks() and receives a merged list of
ExtractionResultSchema, one per chunk, ready for rule engine evaluation.

Team contract:
  INPUT:  list[CreditRowSchema]  (from bank_statement layer via schemas.chunk)
  OUTPUT: list[ExtractionResultSchema]  (from schemas.extraction)
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..schemas.chunk import CreditRowSchema, ChunkPayloadSchema
from ..schemas.extraction import ExtractionResultSchema
from ..aging.aging_map import AgingMap
from ..db.settings import get_settings
from .layer_2a_regex import run_layer_2a
from .layer_2b_ai import run_layer_2b
from .merger import merge_extraction_results

# Fallback values only — actual defaults come from Settings (.env:
# CHUNK_SIZE / CHUNK_MAX_WORKERS) via _default_chunk_size()/_default_max_workers()
# below. Kept here so this module still works if Settings can't be imported
# (e.g. in isolated unit tests).
DEFAULT_CHUNK_SIZE = 50
DEFAULT_MAX_WORKERS = 4


def _default_chunk_size() -> int:
    try:
        return get_settings().CHUNK_SIZE
    except Exception:
        return DEFAULT_CHUNK_SIZE


def _default_max_workers() -> int:
    try:
        return get_settings().CHUNK_MAX_WORKERS
    except Exception:
        return DEFAULT_MAX_WORKERS


def _process_single_chunk(
    chunk: ChunkPayloadSchema,
    aging_map: AgingMap,
    customer_fuzzy_min_pct: float,
) -> ExtractionResultSchema:
    """
    Full extraction pipeline for one chunk:
      1. Layer 2A — regex + AgingMap exact match
      2. Layer 2B — AI fallback for no_invoice_found rows
      3. Merger   — combine into ExtractionResultSchema
    """
    layer_2a_result = run_layer_2a(chunk, aging_map, customer_fuzzy_min_pct)
    layer_2b_result = run_layer_2b(layer_2a_result, aging_map)
    return merge_extraction_results(layer_2a_result, layer_2b_result)


def dispatch_chunks(
    run_id: int,
    rows: list[CreditRowSchema],
    aging_map: AgingMap,
    chunk_size: int | None = None,
    max_workers: int | None = None,
    customer_fuzzy_min_pct: float = 40.0,
) -> list[ExtractionResultSchema]:
    """
    Split rows into chunks, dispatch in parallel, collect results.

    Args:
        run_id:                 Parent run identifier
        rows:                   All credit rows for this run
        aging_map:              Pre-built in-memory aging lookup (read-only, thread-safe)
        chunk_size:             Rows per parallel work unit.
                                 None (default) → Settings.CHUNK_SIZE (.env: CHUNK_SIZE)
        max_workers:            Thread pool size.
                                 None (default) → Settings.CHUNK_MAX_WORKERS (.env: CHUNK_MAX_WORKERS)
        customer_fuzzy_min_pct: Minimum fuzzy score to accept a customer match

    Returns:
        List of ExtractionResultSchema, one per chunk, in chunk_index order.
    """
    chunk_size = chunk_size if chunk_size is not None else _default_chunk_size()
    max_workers = max_workers if max_workers is not None else _default_max_workers()

    # ── Split into chunks ─────────────────────────────────────────────────────
    batches = [rows[i: i + chunk_size] for i in range(0, len(rows), chunk_size)]
    total_chunks = len(batches)

    chunks = [
        ChunkPayloadSchema(
            chunk_id=str(uuid.uuid4()),
            run_id=run_id,
            chunk_index=idx,
            total_chunks=total_chunks,
            rows=batch,
        )
        for idx, batch in enumerate(batches)
    ]

    results: dict[int, ExtractionResultSchema] = {}

    # ── Parallel dispatch ─────────────────────────────────────────────────────
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_process_single_chunk, chunk, aging_map, customer_fuzzy_min_pct): chunk.chunk_index
            for chunk in chunks
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                # Log and create an empty result so the run continues
                # In prod: write a CHUNK_FAILED status to the DB here
                print(f"[chunk_processor] chunk {idx} failed: {exc}")
                results[idx] = _empty_result(chunks[idx])

    # Return in deterministic order
    return [results[i] for i in range(total_chunks) if i in results]


def _empty_result(chunk: ChunkPayloadSchema) -> ExtractionResultSchema:
    """Fallback empty result for a chunk that errored — keeps the run from stalling."""
    return ExtractionResultSchema(
        chunk_id=chunk.chunk_id,
        run_id=chunk.run_id,
        chunk_index=chunk.chunk_index,
        total_chunks=chunk.total_chunks,
        identified_payments=[],
        unknown_payments=[],
        total_rows_in_chunk=len(chunk.rows),
        identified_count=0,
        unknown_count=len(chunk.rows),
    )