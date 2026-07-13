"""
app.extraction.merger
======================
Merges Layer 2A found_invoices + Layer 2B identified/unknown into a single
ExtractionResultSchema that the rule engine orchestrator consumes.

Team contract:
  INPUT:  Layer2AResultSchema + Layer2BResult  (internal types)
  OUTPUT: ExtractionResultSchema               (from schemas.extraction)

This is the last step before Phase 3. After this point, the rule engine
sees only IdentifiedPayment and UnknownPayment — it never sees raw 2A/2B types.
"""
from __future__ import annotations

from ..schemas.extraction import ExtractionResultSchema, IdentifiedPayment, UnknownPayment
from ..schemas.layer2 import Layer2AResultSchema, Layer2ARow
from .layer_2b_ai import Layer2BResult


def _layer_2a_row_to_identified(row: Layer2ARow) -> IdentifiedPayment:
    """Convert a Layer2ARow (found_invoices bucket) into an IdentifiedPayment."""
    return IdentifiedPayment(
        original=row.original,
        confirmed_invoice_numbers=row.confirmed_invoice_numbers,
        customer_name=row.customer_fuzzy_match,
        customer_match_pct=row.customer_match_pct,
        extraction_method=row.extraction_method,
        confidence_score=None,          # no AI involved — deterministic match
        identified_by_layer="2a",
        ai_raw_response=None,
    )


def merge_extraction_results(
    layer_2a: Layer2AResultSchema,
    layer_2b: Layer2BResult,
) -> ExtractionResultSchema:
    """
    Merge 2A and 2B results into the final ExtractionResultSchema.

    Sources:
      identified_payments = 2A.found_invoices (converted) + 2B.identified
      unknown_payments    = 2B.unknown
    """
    from_2a: list[IdentifiedPayment] = [
        _layer_2a_row_to_identified(r) for r in layer_2a.found_invoices
    ]
    from_2b_identified: list[IdentifiedPayment] = layer_2b.identified
    unknown: list[UnknownPayment] = layer_2b.unknown

    all_identified = from_2a + from_2b_identified

    return ExtractionResultSchema(
        chunk_id=layer_2a.chunk_id,
        run_id=layer_2a.run_id,
        chunk_index=layer_2a.chunk_index,
        total_chunks=layer_2a.total_chunks,
        identified_payments=all_identified,
        unknown_payments=unknown,
        total_rows_in_chunk=len(layer_2a.found_invoices) + len(layer_2a.no_invoice_found),
        identified_count=len(all_identified),
        unknown_count=len(unknown),
        layer_2a_hit_count=len(from_2a),
        layer_2b_hit_count=len(from_2b_identified),
    )
