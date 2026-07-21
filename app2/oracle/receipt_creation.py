"""
app.oracle.receipt_creation
=============================
Step 1 of the two-step Oracle receipt flow — creates a BARE receipt (no
remittanceReferences) for a single LineItem, and writes the outcome onto
the row's receipt-creation fields (oracle_ref_no, standard_receipt_id,
oracle_status_code, oracle_post_status, oracle_posted_at, post_message,
oracle_payload).

Called from two places:
  - rule_engine/orchestrator.py's Step 4.5 — once per credit row, for
    EVERY row in a completed run, regardless of category.
  - hitl/service.py's invoice-mapping step — as a retry, if a row somehow
    reaches ready_for_oracle + SPOC approval without ever having a
    successful receipt (e.g. the original creation attempt failed).

Kept as its own module (not inlined in fusion_client.py) since it owns
the DB write, whereas fusion_client.py is kept as a pure HTTP client with
no DB dependency.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from ..db.models import LineItem
from .fusion_client import OracleFusionClient, build_receipt_creation_payload

logger = logging.getLogger(__name__)


def create_receipt_for_line_item(db: Session, line_item: LineItem) -> dict:
    """
    Creates a bare Oracle receipt for one row and persists the outcome.
    Idempotent-ish: safe to call again for a row whose previous attempt
    failed (rebuilds the payload fresh each time, same ReceiptNumber via
    _build_receipt_number's deterministic scheme — see fusion_client.py).
    Does NOT re-create a receipt that already succeeded — callers should
    check `line_item.oracle_post_status == "success"` first if that
    matters to them (orchestrator's Step 4.5 only ever calls this once per
    row per run anyway).
    """
    payload = build_receipt_creation_payload(line_item)
    line_item.oracle_payload = payload

    logger.info(
        "[receipt_creation] row=%s bank=%s account=%s amount=%s %s — creating receipt...",
        line_item.id, line_item.bank_name, line_item.account_number,
        line_item.credit_amount, line_item.statement_currency,
    )

    client = OracleFusionClient()
    result = client.post_standard_receipt(payload)

    line_item.oracle_post_status  = "success" if result["success"] else "failed"
    line_item.oracle_ref_no       = result.get("oracle_ref_no")
    line_item.standard_receipt_id = result.get("standard_receipt_id")
    line_item.oracle_status_code  = result.get("status_code")
    line_item.post_message        = result.get("message")
    line_item.oracle_posted_at    = dt.datetime.utcnow()
    # PATCH: the full raw Oracle response was being discarded — only a few
    # extracted fields were kept. Persist it so the row-detail page can
    # show the actual "receipt created" output, not just a status string.
    line_item.oracle_response_raw = result.get("raw")

    if result["success"]:
        logger.info(
            "[receipt_creation] row=%s SUCCESS — StandardReceiptId=%s ReceiptNumber=%s",
            line_item.id, line_item.standard_receipt_id, line_item.oracle_ref_no,
        )
    else:
        logger.warning(
            "[receipt_creation] row=%s FAILED (status=%s) — %s",
            line_item.id, line_item.oracle_status_code, line_item.post_message,
        )

    return result