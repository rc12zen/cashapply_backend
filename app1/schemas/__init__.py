"""
app.schemas
============
Pydantic data contracts that define the payload shapes passed between
every processing layer. Each layer imports ONLY from here — never from
a sibling layer's internal types. This lets each team member work on
their layer in isolation.

Layer boundary map
------------------
Phase 1 → Phase 2:   CreditRowSchema  (one credit row from bank statement)
                      ChunkPayloadSchema (batch of rows dispatched in parallel)

Phase 2A → Phase 2B: Layer2AResultSchema (found + no_invoice_found buckets)

Phase 2 → Phase 3:   ExtractionResultSchema (final identified / unknown split)
                      IdentifiedPayment / UnknownPayment (individual row results)

Phase 3 input:        RuleEngineInputSchema
Phase 3 output:       RuleResultSchema
"""
from .chunk import CreditRowSchema, ChunkPayloadSchema
from .layer2 import Layer2AResultSchema, Layer2ARow
from .extraction import ExtractionResultSchema, IdentifiedPayment, UnknownPayment
from .rule_engine import RuleEngineInputSchema, RuleResultSchema

__all__ = [
    # Phase 1 → 2
    "CreditRowSchema",
    "ChunkPayloadSchema",
    # Phase 2A → 2B
    "Layer2AResultSchema",
    "Layer2ARow",
    # Phase 2 → 3
    "ExtractionResultSchema",
    "IdentifiedPayment",
    "UnknownPayment",
    # Phase 3
    "RuleEngineInputSchema",
    "RuleResultSchema",
]
