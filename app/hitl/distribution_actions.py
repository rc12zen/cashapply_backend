"""
app.hitl.distribution_actions
================================
Per-entry Approve & Post / Reject / Edit GL Rate for a "distributed"
parent row's LineItem.distribution_breakdown -- the no-child-rows
redesign of Split & Map (see hitl/split_and_map.py's confirm_distribution()
docstring for why child LineItem rows were dropped).

Each function here is the direct per-entry counterpart of one in
hitl/service.py (approve_row / reject_row / edit_gl_rate) -- same
two-step Oracle flow, same GL-rate CASE A/B split, same ledger semantics.
The one structural difference: instead of a real LineItem to read/write,
these operate on one dict inside r.distribution_breakdown, addressed by
its entry_id, and reuse the EXACT SAME Oracle payload-building functions
(build_receipt_creation_payload, build_remittance_reference_payloads) by
constructing a throwaway LineItem as a value object -- built but never
db.add()'d, so it's never a real row, never counted anywhere, never
visible to any query. Those functions only ever read plain attributes
off whatever LineItem-shaped object they're given, so this is safe reuse,
not a hack -- split_and_map.py's _resolve_entry() already does the same
thing for classification, one layer earlier in this same flow.

distribution_breakdown is a JSON column with no SQLAlchemy Mutable
tracking configured (matches how matched_invoices etc. are handled
elsewhere in this codebase -- always reassigned fresh, never mutated in
place and left unassigned). Every function here follows the same rule:
mutate the local `breakdown` list/dicts freely, then always finish with
`r.distribution_breakdown = breakdown` (a fresh list) before commit, or
the change will not be detected/persisted.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..db.models import LineItem, RowStatusHistory
from ..oracle.fusion_client import OracleFusionClient, build_receipt_creation_payload, build_remittance_reference_payloads
from ..rule_engine.invoice_ledger import confirm_application_for_entry, release_application_for_entry
from .split_and_map import _COPIED_FIELDS

logger = logging.getLogger(__name__)


def _persist_breakdown(r: LineItem, breakdown: list[dict]) -> None:
    """ALWAYS use this instead of `r.distribution_breakdown = breakdown`
    directly. Every entry dict inside `breakdown` is the SAME object
    already referenced by r.distribution_breakdown's currently-loaded
    value (since _find_entry() returns a reference into the existing
    list, not a copy) -- so by the time this reassignment happens, the
    "new" list and the "old" list already contain identical (indeed
    identical-by-reference) dicts. A plain `=` reassignment can be judged
    equal-to-old by SQLAlchemy's default dirty check for a Column(JSON)
    with no Mutable tracking configured, and the UPDATE gets silently
    skipped -- the request runs clean end-to-end, commits with no error,
    and NOTHING was actually written. flag_modified() forces the UPDATE
    unconditionally, regardless of any equality comparison."""
    r.distribution_breakdown = breakdown
    flag_modified(r, "distribution_breakdown")


def _pseudo_line_item_id(parent_id: int, entry_id: str) -> int:
    """A stable, unique-enough int for ReceiptNumber generation (Oracle's
    ReceiptNumber format ends in <line_item_id> -- ee fusion_client.py's
    _build_receipt_number()). Real LineItems get this for free from the
    DB's auto-increment id; a distribution entry has no such id, so this
    deterministically derives one from (parent_id, entry_id) instead --
    same parent + same entry always produces the same number, so retries
    are idempotent exactly like a real row's ReceiptNumber is."""
    try:
        seq = int(entry_id)
    except (TypeError, ValueError):
        seq = abs(hash(entry_id)) % 1000
    return int(f"{parent_id}{seq:03d}")


def _find_entry(breakdown: list[dict], entry_id: str) -> dict | None:
    return next((e for e in breakdown if e.get("entry_id") == entry_id), None)


def _build_transient_line_item(r: LineItem, entry: dict) -> LineItem:
    """A throwaway LineItem built from the parent's shared facts (bank,
    account, OU, statement currency, ...) plus this ONE entry's own
    amount/customer/invoice/FX data -- never persisted (no db.add()), so
    it never becomes a real row. Fed straight into the existing Oracle
    payload builders, which only ever read plain attributes off it."""
    per_invoice_fields = [
        "invoice_currency", "is_cross_currency", "fx_credit_to_invoice",
        "fx_credit_to_invoice_source", "is_cross_ledger", "fx_invoice_to_functional",
        "fx_invoice_to_functional_source", "is_cross_ou_currency", "ou_evidence",
    ]
    copied = {f: getattr(r, f) for f in _COPIED_FIELDS if f not in per_invoice_fields}
    t = LineItem(
        **copied,
        invoice_currency=entry["invoice_currency"],
        is_cross_currency=entry["is_cross_currency"],
        fx_credit_to_invoice=entry["fx_credit_to_invoice"],
        fx_credit_to_invoice_source=entry["fx_credit_to_invoice_source"],
        is_cross_ledger=entry["is_cross_ledger"],
        fx_invoice_to_functional=entry["fx_invoice_to_functional"],
        fx_invoice_to_functional_source=entry["fx_invoice_to_functional_source"],
        is_cross_ou_currency=entry["is_cross_ou_currency"],
        ou_evidence=entry["ou_evidence"],
        credit_amount=entry["amount"],
        extracted_customer_name=entry["customer_name"],
        customer_match_pct=entry.get("customer_match_pct"),
        matched_invoices=[{
            "invoice_number":     entry["invoice_number"],
            "outstanding_amount": entry["target_total"],
            "stated_amount":      entry["target_total"],
            "customer_name":      entry["customer_name"],
            "ou_number":          r.ou_number,
            "invoice_currency":   entry["currency"],
        }],
        target_total=entry["target_total"],
        shortfall_pct=entry.get("shortfall_pct"),
        rule_id=entry["rule_id"],
        reason_code=entry["reason_code"],
        created_at=r.created_at,
    )
    t.id = _pseudo_line_item_id(r.id, entry["entry_id"])
    if entry.get("standard_receipt_id"):
        t.standard_receipt_id = entry["standard_receipt_id"]
    return t


def approve_distribution_entry(
    db: Session, parent_id: int, entry_id: str, comment: str | None, triggered_by: str,
) -> dict:
    """Per-entry counterpart of hitl/service.py's approve_row() -- same
    two-step Oracle flow (bare receipt, then remittanceReference), same
    ledger confirm-on-success semantics, just targeting one entry inside
    the parent's distribution_breakdown instead of a real row."""
    r = db.query(LineItem).get(parent_id)
    if not r:
        return {"error": "not found"}

    breakdown = list(r.distribution_breakdown or [])
    entry = _find_entry(breakdown, entry_id)
    if entry is None:
        return {"error": "entry_not_found", "message": f"No entry '{entry_id}' on row {parent_id}."}
    if entry["hitl_status"] == "approved":
        return {"error": "already_approved", "message": f"Entry {entry_id} was already approved and posted."}
    if entry["hitl_status"] == "rejected":
        return {"error": "already_rejected", "message": f"Entry {entry_id} was rejected — cannot approve a rejected entry."}
    if not entry.get("passed_validation", True):
        return {
            "error": "not_approvable",
            "message": (
                f"Entry {entry_id} ({entry['customer_name']} / {entry['invoice_number']}) is "
                f"rule {entry['rule_id']} ({entry['reason_code']}) — needs re-routing before it "
                f"can be approved."
            ),
        }

    t = _build_transient_line_item(r, entry)
    client = OracleFusionClient()

    # ── Step 1: bare receipt creation (mirrors oracle/receipt_creation.py's
    # create_receipt_for_line_item(), just writing into `entry` instead of
    # onto a LineItem's own columns) ─────────────────────────────────────
    try:
        payload = build_receipt_creation_payload(t)
    except ValueError as exc:
        entry["oracle_post_status"] = "failed"
        entry["post_message"] = str(exc)
        _persist_breakdown(r, breakdown)
        db.commit()
        return {"id": parent_id, "entry_id": entry_id, "error": "payload_error", "message": str(exc)}

    entry["oracle_payload"] = payload
    logger.info(
        "[distribution_approve] parent=%s entry=%s customer=%s invoice=%s amount=%s — creating receipt...",
        parent_id, entry_id, entry["customer_name"], entry["invoice_number"], entry["amount"],
    )
    result = client.post_standard_receipt(payload)
    entry["oracle_post_status"]  = "success" if result["success"] else "failed"
    entry["oracle_ref_no"]       = result.get("oracle_ref_no")
    entry["standard_receipt_id"] = result.get("standard_receipt_id")
    entry["oracle_status_code"]  = result.get("status_code")
    entry["post_message"]        = result.get("message")
    entry["oracle_posted_at"]    = dt.datetime.utcnow().isoformat()
    entry["oracle_response_raw"] = result.get("raw")

    if not result["success"]:
        _persist_breakdown(r, breakdown)
        db.add(RowStatusHistory(
            line_item_id=r.id, from_state="distributed", to_state="distributed",
            trigger="spoc_distribution_entry_approve", rule_id=entry["rule_id"],
            triggered_by=triggered_by,
            comment=(
                f"Entry {entry_id} ({entry['customer_name']} / {entry['invoice_number']}) "
                f"receipt creation FAILED: {entry['post_message']}"
                + (f" — {comment}" if comment else "")
            ),
        ))
        db.commit()
        return {
            "id": parent_id, "entry_id": entry_id, "success": False,
            "message": entry["post_message"],
        }

    # ── Step 2: invoice mapping (remittanceReferences) ────────────────────
    t.standard_receipt_id = entry["standard_receipt_id"]
    reference_payloads = build_remittance_reference_payloads(t, invoice_breakup=None)
    entry["reference_payload"] = reference_payloads

    logger.info(
        "[distribution_approve] parent=%s entry=%s standard_receipt_id=%s — attaching %d reference(s)...",
        parent_id, entry_id, entry["standard_receipt_id"], len(reference_payloads),
    )
    ref_results = [
        client.post_remittance_reference(entry["standard_receipt_id"], ref)
        for ref in reference_payloads
    ]
    entry["reference_response_raw"] = [res.get("raw") for res in ref_results]
    all_succeeded = all(res["success"] for res in ref_results) if ref_results else True
    entry["reference_status"] = "success" if all_succeeded else "failed"

    if all_succeeded:
        entry["reference_message"] = f"{len(ref_results)} reference(s) added successfully"
        entry["hitl_status"] = "approved"
        confirm_application_for_entry(db, parent_id, entry_id)
    else:
        failed_msgs = [res["message"] for res in ref_results if not res["success"]]
        entry["reference_message"] = "; ".join(failed_msgs)[:2000]
        # Left as "pending", same as a normal row's post_failed state --
        # the receipt DOES exist, only the invoice-mapping step failed, so
        # the invoice claim stays reserved rather than released.

    _persist_breakdown(r, breakdown)
    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="distributed", to_state="distributed",
        trigger="spoc_distribution_entry_approve", rule_id=entry["rule_id"],
        triggered_by=triggered_by,
        comment=(
            f"Entry {entry_id} ({entry['customer_name']} / {entry['invoice_number']}) "
            f"{'approved and posted' if all_succeeded else 'receipt created but invoice mapping failed'}"
            + (f" — {comment}" if comment else "")
        ),
    ))
    db.commit()

    return {
        "id": parent_id,
        "entry_id": entry_id,
        "success": all_succeeded,
        "hitl_status": entry["hitl_status"],
        "oracle_ref_no": entry["oracle_ref_no"],
        "standard_receipt_id": entry["standard_receipt_id"],
        "reference_status": entry["reference_status"],
        "message": entry["reference_message"] or entry["post_message"],
    }


def reject_distribution_entry(
    db: Session, parent_id: int, entry_id: str, comment: str | None, triggered_by: str,
) -> dict:
    """Per-entry counterpart of hitl/service.py's reject_row() -- frees
    this entry's invoice claim, leaving every sibling entry under the
    same parent untouched."""
    r = db.query(LineItem).get(parent_id)
    if not r:
        return {"error": "not found"}

    breakdown = list(r.distribution_breakdown or [])
    entry = _find_entry(breakdown, entry_id)
    if entry is None:
        return {"error": "entry_not_found", "message": f"No entry '{entry_id}' on row {parent_id}."}
    if entry["hitl_status"] == "approved":
        return {
            "error": "already_approved",
            "message": f"Entry {entry_id} was already approved and posted — cannot reject a posted entry here.",
        }

    entry["hitl_status"]      = "rejected"
    entry["rejected_at"]      = dt.datetime.utcnow().isoformat()
    entry["rejected_by"]      = triggered_by
    entry["rejected_reason"]  = comment

    release_application_for_entry(db, parent_id, entry_id)

    _persist_breakdown(r, breakdown)
    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="distributed", to_state="distributed",
        trigger="spoc_distribution_entry_reject", rule_id=entry["rule_id"],
        triggered_by=triggered_by,
        comment=(
            f"Entry {entry_id} ({entry['customer_name']} / {entry['invoice_number']}) rejected"
            + (f" — {comment}" if comment else "")
        ),
    ))
    db.commit()

    return {"id": parent_id, "entry_id": entry_id, "status": "Rejected"}


def edit_gl_rate_for_distribution_entry(
    db: Session, parent_id: int, entry_id: str, new_rate: float, reason: str | None, triggered_by: str,
) -> dict:
    """Per-entry counterpart of hitl/service.py's edit_gl_rate() -- same
    CASE A (receipt exists, PATCH it) / CASE B (receipt creation failed,
    correct the rate and retry) split, just targeting one entry."""
    r = db.query(LineItem).get(parent_id)
    if not r:
        return {"error": "not found"}

    breakdown = list(r.distribution_breakdown or [])
    entry = _find_entry(breakdown, entry_id)
    if entry is None:
        return {"error": "entry_not_found", "message": f"No entry '{entry_id}' on row {parent_id}."}
    if not entry["is_cross_ledger"]:
        return {
            "error": "not_cross_ledger",
            "message": f"Entry {entry_id} is not a cross-ledger-currency entry — there is no GL rate to edit.",
        }

    old_rate = entry["fx_invoice_to_functional"]
    if entry.get("gl_rate_original") is None:
        entry["gl_rate_original"] = old_rate

    client = OracleFusionClient()

    if not entry.get("standard_receipt_id"):
        # ── CASE B: no receipt exists yet -- correct the rate and retry ──
        if entry.get("oracle_post_status") != "failed":
            return {
                "error": "no_receipt",
                "message": (
                    f"Entry {entry_id} has no Oracle receipt yet, and receipt creation hasn't "
                    f"actually been attempted (or hasn't failed) — nothing to retry."
                ),
            }

        entry["fx_invoice_to_functional"]        = new_rate
        entry["fx_invoice_to_functional_source"] = "spoc_manual"
        entry["gl_rate_edited_at"]               = dt.datetime.utcnow().isoformat()
        entry["gl_rate_edited_by"]               = triggered_by
        entry["gl_rate_edit_reason"]             = reason

        t = _build_transient_line_item(r, entry)
        try:
            payload = build_receipt_creation_payload(t)
        except ValueError as exc:
            _persist_breakdown(r, breakdown)
            db.commit()
            return {"error": "payload_error", "message": str(exc)}

        entry["oracle_payload"] = payload
        result = client.post_standard_receipt(payload)
        entry["oracle_post_status"]  = "success" if result["success"] else "failed"
        entry["oracle_ref_no"]       = result.get("oracle_ref_no")
        entry["standard_receipt_id"] = result.get("standard_receipt_id")
        entry["oracle_status_code"]  = result.get("status_code")
        entry["post_message"]        = result.get("message")
        entry["oracle_posted_at"]    = dt.datetime.utcnow().isoformat()
        entry["oracle_response_raw"] = result.get("raw")

        _persist_breakdown(r, breakdown)
        db.add(RowStatusHistory(
            line_item_id=r.id, from_state="distributed", to_state="distributed",
            trigger="spoc_distribution_entry_edit_gl_rate_retry", rule_id=entry["rule_id"],
            triggered_by=triggered_by,
            comment=(
                f"Entry {entry_id} GL rate changed from {old_rate} to {new_rate}, receipt creation "
                f"retried ({'succeeded' if result['success'] else 'failed again: ' + str(result.get('message'))})"
                + (f" — {reason}" if reason else "")
            ),
        ))
        db.commit()

        if not result["success"]:
            return {
                "error": "retry_failed", "old_rate": old_rate, "new_rate": new_rate,
                "message": f"Rate updated, but retrying receipt creation failed again: {result.get('message')}",
            }

        return {
            "id": parent_id, "entry_id": entry_id, "success": True,
            "old_rate": old_rate, "new_rate": new_rate,
            "gl_rate_original": entry["gl_rate_original"],
            "standard_receipt_id": entry["standard_receipt_id"],
            "message": f"GL rate updated to {new_rate} and receipt created successfully ({entry['standard_receipt_id']}).",
        }

    # ── CASE A: receipt already exists -- PATCH it directly ──────────────
    if entry.get("reference_status") == "success":
        return {
            "error": "already_mapped",
            "message": (
                f"Entry {entry_id} already has invoice mapping (remittanceReferences) posted "
                f"against this receipt — the GL rate can no longer be edited here."
            ),
        }

    result = client.patch_standard_receipt(
        entry["standard_receipt_id"], {"ConversionRateType": "User", "ConversionRate": new_rate},
    )
    if not result.get("success"):
        return {"error": "oracle_patch_failed", "message": f"Oracle rejected the rate change: {result.get('message')}"}

    entry["fx_invoice_to_functional"]        = new_rate
    entry["fx_invoice_to_functional_source"] = "spoc_manual"
    entry["gl_rate_edited_at"]               = dt.datetime.utcnow().isoformat()
    entry["gl_rate_edited_by"]               = triggered_by
    entry["gl_rate_edit_reason"]             = reason

    _persist_breakdown(r, breakdown)
    db.add(RowStatusHistory(
        line_item_id=r.id, from_state="distributed", to_state="distributed",
        trigger="spoc_distribution_entry_edit_gl_rate", rule_id=entry["rule_id"],
        triggered_by=triggered_by,
        comment=f"Entry {entry_id} GL rate changed from {old_rate} to {new_rate}" + (f" — {reason}" if reason else ""),
    ))
    db.commit()

    return {
        "id": parent_id, "entry_id": entry_id, "success": True,
        "old_rate": old_rate, "new_rate": new_rate,
        "gl_rate_original": entry["gl_rate_original"],
        "message": f"GL rate updated to {new_rate} on Oracle receipt {entry['standard_receipt_id']}.",
    }