"""
app.hitl.reopen_with_edits
=============================
Reopen a rejected row (or a parked overpayment) WITH edits, and recompute its
bucket from those edits — the flow described in
`cashapply_backend/reopen_with_edits_plan.md`.

WHY THIS EXISTS
---------------
reject_row() writes only hitl_status / current_state / status / pre_reject_state
and releases the invoice claim. It never touches rule_id. Because the display
bucket is RULE_ID_TO_GROUP[rule_id] (bff/metrics.py's _category_for_row), the
old reopen_row() — a pure undo that just clears hitl_status — mechanically
returns the row to the exact bucket it was rejected from, with the same
mapping and no way to change it. A row rejected out of Ready for Oracle came
straight back to Ready for Oracle, unedited. That is the gap this closes.

DESIGN — composes existing services, adds no new classifier
-----------------------------------------------------------
Nothing here re-implements matching or classification. Two proven paths do the
work, routed by WHAT THE SPOC ACTUALLY EDITED:

  invoices edited  -> hitl/manual_mapping.py's apply_selection()
                      (its own R9 classifier; the ONLY one allowed to produce
                      R9d/R9e, because it caps every reference at that
                      invoice's own outstanding — see that module's _classify)
  customer only    -> rule_engine/customer_name_correction.py's
                      correct_customer_name(), which owns the single
                      evaluate_row() call site for a re-evaluation
  nothing edited   -> a dry-run re-evaluation via evaluate_as_customer(), so
                      the SPOC still sees where the row lands today

This deliberately introduces NO fourth evaluate_row() call site (see
evaluator.py's documented three-call-site invariant) and touches neither
orchestrator.py nor evaluator.py.

ORDERING IS LOAD-BEARING
------------------------
The rejection is cleared FIRST, then the edits are applied. That ordering is
not cosmetic:

  * correct_customer_name() and manual_mapping refuse outright while
    hitl_status is set — correctly, for their own entry points. Clearing it
    first means they run unmodified rather than needing a bypass flag.
  * state_machine.apply_transition() reads from_state via .value and then
    writes current_state as a plain STRING. Calling it twice in one session
    raises AttributeError (see customer_name_correction.py's comment). Only the
    customer path calls it; the invoice path sets fields directly, so at most
    one apply_transition runs per confirm.

Everything is staged on one session and committed once, so a failure part-way
can never leave a row mapped-but-still-rejected.

WHAT IS NOT EDITABLE HERE
-------------------------
  * Amounts — never typed. Every amount comes from the aging report, same rule
    as manual mapping.
  * Bank facts (credit_amount, statement_date, narrative, account) — facts.
  * The CUSTOMER, once an Oracle receipt exists. build_receipt_creation_payload
    stamps CustomerAccountNumber into a receipt created for EVERY credit row at
    analysis time, and reject never voids it. Changing the customer would leave
    a live receipt against the wrong customer, and Oracle almost certainly will
    not PATCH that field (the analogous case in service.py's edit_gl_rate is
    documented as needing reverse-and-recreate). Locked server-side, not just
    disabled in the UI.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from ..aging import aging_store
from ..db.models import LineItem, RowStatusHistory, User
from ..oracle.fusion_client import build_receipt_creation_payload
from ..rule_engine.customer_name_correction import (
    correct_customer_name, evaluate_as_customer,
)
from ..rule_engine.invoice_ledger import release_applications
from . import manual_mapping
from .service import validate_reopen_claims

logger = logging.getLogger(__name__)


# ── Eligibility ──────────────────────────────────────────────────────────────

def _reopen_kind(r: LineItem) -> str | None:
    """"rejected" | "parked" | None — the two terminal-but-reversible states,
    matching reopen_row()'s own contract and the action registry's
    applicable_categories for "reopen"."""
    if r.hitl_status == "rejected":
        return "rejected"
    if r.current_state and _state_value(r.current_state) == "overpayment_parked":
        return "parked"
    return None


def _state_value(state) -> str | None:
    """current_state is an Enum on a freshly-loaded row but a plain string once
    apply_transition() has run in this session — tolerate both."""
    if state is None:
        return None
    return state.value if hasattr(state, "value") else str(state)


def _customer_lock(r: LineItem) -> tuple[bool, str | None]:
    """Whether the customer field must be locked, and why (see module
    docstring). Keyed on the receipt actually existing in Oracle, not on
    reference_status — a created-but-unmapped receipt is exactly the dangerous
    case."""
    if getattr(r, "standard_receipt_id", None):
        return True, (
            "An Oracle receipt already exists for this payment and is registered "
            "against the current customer, so the customer cannot be changed here — "
            "it would leave that receipt against the wrong account. The invoice "
            "mapping can still be edited."
        )
    return False, None


def _rejection_context(db: Session, r: LineItem) -> dict:
    """Why the row was rejected, for the modal to show. Reads the most recent
    reject/park entry from RowStatusHistory — the comment is optional (nothing
    collected one before the reject-comment change), so this can legitimately
    come back with comment=None on older rows."""
    entry = (
        db.query(RowStatusHistory)
        .filter(
            RowStatusHistory.line_item_id == r.id,
            RowStatusHistory.trigger.in_(["spoc_reject", "spoc_park_overpayment"]),
        )
        .order_by(RowStatusHistory.id.desc())
        .first()
    )
    return {
        "comment":      entry.comment if entry else None,
        "rejected_by":  entry.triggered_by if entry else None,
        "rejected_at":  entry.created_at.isoformat() if entry and getattr(entry, "created_at", None) else None,
        "rejected_from": r.pre_park_state or r.pre_reject_state,
    }


def _bucket_pinned_by(r: LineItem) -> str | None:
    """
    Non-null when this row's bucket is decided by something that OUTRANKS
    rule_id in _category_for_row()'s precedence — in which case re-evaluation
    genuinely cannot move it, and the UI must say so rather than implying a
    move it can't deliver.

    Only reference_status is reachable here: "success"/processed rows never
    surface as category "rejected" at all (precedence step 1 beats step 2), so
    in practice this fires for post_failed rows, which DO keep their Reopen
    button by explicit decision.
    """
    if r.reference_status == "success":
        return "processed"
    if r.reference_status == "failed":
        return "post_failed"
    return None


# ── Options ──────────────────────────────────────────────────────────────────

def get_reopen_options(db: Session, line_item_id: int) -> dict:
    """Everything the Reopen & Review modal needs to render itself."""
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}

    kind = _reopen_kind(r)
    if kind is None:
        return {
            "error": "not_reopenable",
            "message": (
                f"Row {r.id} is neither rejected nor a parked overpayment — only "
                f"those two can be reopened."
            ),
        }

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {
            "error": "no_aging_map",
            "message": "No aging report is currently loaded — load one before reopening.",
        }

    locked, lock_reason = _customer_lock(r)
    matched = r.matched_invoices or []
    current_customer = (
        (matched[0].get("customer_name") if matched else None)
        or r.extracted_customer_name
        or None
    )

    invoices = (
        [manual_mapping._serialize_invoice(v) for v in aging_map.invoices_for_customer(current_customer)]
        if current_customer else []
    )

    return {
        "id": r.id,
        "reopen_kind": kind,
        "version": r.version,
        "customer_locked": locked,
        "customer_locked_reason": lock_reason,
        "current_customer_name": current_customer,
        "current_invoice_numbers": [m.get("invoice_number") for m in matched if m.get("invoice_number")],
        # Only offered when the customer isn't locked — no point shipping 500
        # names the UI must then refuse to let anyone pick.
        "customers": (
            aging_map.customers_for_ou(r.ou_number, limit=500)
            if (not locked and r.ou_number) else []
        ),
        "invoices": invoices,
        "bucket_pinned_by": _bucket_pinned_by(r),
        "rejection": _rejection_context(db, r),
        # Read-only bank facts, so the modal never has to refetch them.
        "bank": {
            "credit_amount":  float(r.credit_amount) if r.credit_amount is not None else None,
            "currency":       r.statement_currency,
            "statement_date": r.statement_date.isoformat() if r.statement_date else None,
            "narrative":      r.narrative,
            "account_number": r.account_number,
        },
        **manual_mapping._credit_context(aging_map, r, current_customer),
    }


def get_invoices_for_customer(db: Session, line_item_id: int, customer_name: str) -> dict:
    """That customer's open invoices + credit context, for when the SPOC
    changes the customer in the modal. Thin pass-through to the manual-mapping
    equivalent so both screens show identical data."""
    return manual_mapping.get_invoices_for_customer(db, line_item_id, customer_name)


# ── Preview ──────────────────────────────────────────────────────────────────

def _outcome_snapshot(r: LineItem) -> dict:
    matched = r.matched_invoices or []
    return {
        "rule_id":         r.rule_id,
        "reason_code":     r.reason_code,
        "customer_name":   (matched[0].get("customer_name") if matched else None) or r.extracted_customer_name,
        "invoice_numbers": [m.get("invoice_number") for m in matched if m.get("invoice_number")],
        "target_total":    float(r.target_total) if r.target_total is not None else None,
        "shortfall_pct":   float(r.shortfall_pct) if r.shortfall_pct is not None else None,
    }


def preview_reopen(
    db: Session,
    line_item_id: int,
    customer_name: str | None = None,
    invoice_numbers: list[str] | None = None,
    overpayment_disposition: str | None = None,
) -> dict:
    """
    Read-only. Says what confirm_reopen() WOULD do, so the SPOC sees the
    reclassification before committing to it.

    Persists nothing: the invoice path runs manual_mapping.preview_selection()
    (already read-only), and the customer path runs evaluate_as_customer(),
    which is pure with respect to the LineItem. Advisory only — confirm
    re-validates from scratch.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}

    kind = _reopen_kind(r)
    if kind is None:
        return {
            "error": "not_reopenable",
            "message": (
                f"Row {r.id} is neither rejected nor a parked overpayment — only "
                f"those two can be reopened."
            ),
        }

    aging_map = aging_store.get_aging_map()
    if aging_map is None:
        return {
            "error": "no_aging_map",
            "message": "No aging report is currently loaded — load one before reopening.",
        }

    invoice_numbers = [n for n in (invoice_numbers or []) if n]
    customer_name = (customer_name or "").strip() or None
    locked, lock_reason = _customer_lock(r)

    before = _outcome_snapshot(r)
    blockers: list[dict] = []

    # A locked customer is a hard blocker if they tried to change it anyway
    # (stale page, or a direct API call bypassing the disabled field).
    if locked and customer_name and customer_name != before["customer_name"]:
        blockers.append({"code": "customer_locked", "message": lock_reason})

    # Guards 2 + 3 apply to the invoices the row ALREADY claims, and only when
    # those claims are being kept — a fresh selection replaces them outright,
    # and is validated on its own terms by preview_selection() below.
    if not invoice_numbers:
        claim_blocker = validate_reopen_claims(db, r, was_parked=(kind == "parked"))
        if claim_blocker:
            blockers.append({
                "code": claim_blocker.get("error"),
                "message": claim_blocker.get("message"),
            })

    effective_customer = customer_name or before["customer_name"]
    if customer_name and not aging_map.invoices_for_customer(customer_name):
        blockers.append({
            "code": "invalid_customer",
            "message": (
                f"'{customer_name}' was not found in the currently-loaded aging report. "
                f"Select a real customer from the list."
            ),
        })

    after: dict = dict(before)
    route = "unchanged"

    if invoice_numbers:
        # ── Invoice edit -> manual mapping's own classifier ──────────────────
        route = "invoice_mapping"
        sel = manual_mapping.preview_selection(db, r, invoice_numbers)
        if sel.get("error"):
            blockers.append({"code": "mapping_invalid", "message": sel["error"]})
        else:
            after = {
                "rule_id":         sel.get("rule_id"),
                "reason_code":     sel.get("reason_code"),
                "customer_name":   effective_customer,
                "invoice_numbers": invoice_numbers,
                "target_total":    sel.get("target_total"),
                "shortfall_pct":   sel.get("shortfall_pct"),
            }
            if not sel.get("qualifies"):
                blockers.append({
                    "code": "does_not_qualify",
                    "message": sel.get("message") or "This selection does not qualify.",
                })
            # An overpaid selection (R9e) needs a recorded reason for the
            # excess before apply_selection will commit it. This MUST be a
            # blocker when no disposition has been supplied, not just a flag:
            # reporting can_confirm=True here and then failing inside confirm
            # is exactly the preview/confirm disagreement this endpoint exists
            # to prevent (caught by verification, not by inspection).
            if sel.get("requires_disposition"):
                after["requires_disposition"] = True
                after["excess_amount"] = sel.get("excess_amount")
                if not (overpayment_disposition or "").strip():
                    blockers.append({
                        "code": "requires_disposition",
                        "message": (
                            f"This selection overpays the chosen invoice(s) by "
                            f"{sel.get('excess_amount')}. Record why the remainder stays "
                            f"unapplied before reopening."
                        ),
                    })
    else:
        # ── Customer-only, or no edit at all -> dry-run re-evaluation ────────
        route = "re_evaluation" if customer_name else "unchanged"
        if effective_customer:
            try:
                rule_result, _view = evaluate_as_customer(db, r, effective_customer, aging_map)
                after = {
                    "rule_id":         rule_result.rule_id,
                    "reason_code":     rule_result.reason_code,
                    "customer_name":   effective_customer,
                    # RuleResult.matched_invoices holds MatchedInvoice DATACLASSES
                    # (evaluator.py:92), not the dicts LineItem.matched_invoices
                    # stores -- attribute access, not .get(). Getting this wrong
                    # made every customer-only and no-edit preview fail into a
                    # "preview_failed" blocker instead of showing the outcome.
                    "invoice_numbers": [
                        m.invoice_number for m in (rule_result.matched_invoices or [])
                        if getattr(m, "invoice_number", None)
                    ],
                    "target_total":    rule_result.target_total,
                    "shortfall_pct":   rule_result.shortfall_pct,
                }
            except Exception as e:  # never let a preview 500
                logger.warning("[reopen_preview] row=%s dry-run failed: %s", r.id, e)
                blockers.append({
                    "code": "preview_failed",
                    "message": f"Could not evaluate this change: {e}",
                })

    pinned = _bucket_pinned_by(r)
    return {
        "id": r.id,
        "reopen_kind": kind,
        "version": r.version,
        "route": route,
        "customer_locked": locked,
        "customer_locked_reason": lock_reason,
        "from": before,
        "to": after,
        "changed": (
            after.get("rule_id") != before["rule_id"]
            or after.get("customer_name") != before["customer_name"]
            or after.get("invoice_numbers") != before["invoice_numbers"]
        ),
        # When set, re-evaluation cannot move the bucket no matter what the
        # rule_id becomes -- reference_status outranks it. The UI must say so.
        "bucket_pinned_by": pinned,
        "blockers": blockers,
        "can_confirm": not blockers,
    }


# ── Confirm ──────────────────────────────────────────────────────────────────

def confirm_reopen(
    db: Session,
    line_item_id: int,
    user: User | None,
    customer_name: str | None = None,
    invoice_numbers: list[str] | None = None,
    comment: str | None = None,
    expected_version: int | None = None,
    overpayment_disposition: str | None = None,
    overpayment_comment: str | None = None,
) -> dict:
    """
    Clears the rejection, applies the edits, and lets the row's bucket
    recompute from them — in ONE transaction.

    Re-validates from scratch; never trusts the client's preview (the aging
    report or the row can move in between).

    Never posts to Oracle. If the new outcome is ready_for_oracle the row lands
    there awaiting the normal explicit Approve & Post — the two-gate model is
    unchanged.
    """
    r = db.query(LineItem).get(line_item_id)
    if not r:
        return {"error": "Row not found"}

    kind = _reopen_kind(r)
    if kind is None:
        return {
            "error": "not_reopenable",
            "message": (
                f"Row {r.id} is neither rejected nor a parked overpayment — only "
                f"those two can be reopened."
            ),
        }

    # ── Guard 1: optimistic locking (same contract as approve/reject/reopen)
    if expected_version is not None and r.version != expected_version:
        return {
            "id": r.id,
            "error": "version_conflict",
            "message": (
                f"Row {r.id} was modified by another user since you loaded it "
                f"(expected version {expected_version}, current version {r.version}). "
                f"Refresh and try again."
            ),
            "current_version": r.version,
        }

    invoice_numbers = [n for n in (invoice_numbers or []) if n]
    customer_name = (customer_name or "").strip() or None

    # ── Re-run the full preview as the authoritative validation ──────────────
    # The disposition is passed in so the preview's requires_disposition
    # blocker clears exactly when confirm would actually succeed — the two must
    # never disagree.
    pre = preview_reopen(
        db, line_item_id, customer_name, invoice_numbers,
        overpayment_disposition=overpayment_disposition,
    )
    if pre.get("error"):
        return pre
    if pre.get("blockers"):
        first = pre["blockers"][0]
        return {
            "id": r.id,
            "error": first.get("code") or "validation_failed",
            "message": first.get("message"),
            "blockers": pre["blockers"],
        }

    before = pre["from"]
    from_state = _state_value(r.current_state)
    was_parked = kind == "parked"

    # ── Step 2: release the old claims BEFORE anything re-stakes ────────────
    # reject_row() already released them, but a parked row's release and a
    # re-map to DIFFERENT invoices both need this to be unconditional: without
    # it an outcome that no longer claims an invoice leaves that invoice
    # marked "pending" forever, silently blocking a correct future mapping.
    # (This is the leak customer_name_correction still has -- see the plan.)
    release_applications(db, r)

    # ── Step 3: clear the rejection FIRST, so the existing edit services run
    # unmodified (both refuse while hitl_status is set) ─────────────────────
    restore_state = (r.pre_park_state if was_parked else r.pre_reject_state) or "review_approve"
    r.hitl_status      = None
    r.current_state    = restore_state
    r.status           = "Reopened"
    r.pre_reject_state = None
    if was_parked:
        r.pre_park_state             = None
        r.overpayment_disposition    = None
        r.overpayment_disposition_at = None
        r.overpayment_disposition_by = None

    # ── Steps 4-6: apply the edits, routed by what changed ──────────────────
    applied: list[str] = []
    outcome: dict = {}

    if customer_name and customer_name != before["customer_name"]:
        # A customer change invalidates any previously hand-picked mapping, so
        # drop the flag that would otherwise make correct_customer_name refuse.
        # Legitimate here and nowhere else: the SPOC is explicitly replacing
        # that decision, not having it overwritten underneath them.
        if r.manually_mapped and invoice_numbers:
            r.manually_mapped = False
        res = correct_customer_name(db, r.id, customer_name, corrected_by=(user.email if user else "unknown"))
        if res.get("error"):
            db.rollback()
            return {
                "id": r.id,
                "error": res["error"],
                "message": res.get("message") or "Could not apply the customer change.",
            }
        applied.append("customer_name")
        outcome = res

    if invoice_numbers:
        # apply_selection() re-validates and RE-CLASSIFIES directly (it does not
        # call apply_transition), so it safely overrides whatever the customer
        # re-evaluation above concluded -- which is the intended precedence: a
        # human-picked mapping beats an inferred one.
        res = manual_mapping.apply_selection(
            db, r, invoice_numbers, user,
            overpayment_disposition=overpayment_disposition,
            overpayment_comment=overpayment_comment,
            commit=False,
        )
        if res.get("error"):
            db.rollback()
            return {
                "id": r.id,
                "error": "mapping_failed",
                "message": res["error"],
                "requires_disposition": res.get("requires_disposition"),
                "excess_amount": res.get("excess_amount"),
            }
        applied.append("invoice_mapping")
        outcome = res

    # ── Step 7: audit + version ─────────────────────────────────────────────
    r.version            = (r.version or 0) + 1
    r.reopen_edited_at   = dt.datetime.utcnow()
    r.reopen_edited_by   = user.email if user else None
    after = _outcome_snapshot(r)
    r.reopen_edit_summary = {
        "applied": applied,
        "from": before,
        "to": after,
        "reopen_kind": kind,
        "comment": comment,
    }

    # ── Step 8: history, payload preview ────────────────────────────────────
    diff_bits = []
    if before["customer_name"] != after["customer_name"]:
        diff_bits.append(f"customer '{before['customer_name']}' -> '{after['customer_name']}'")
    if before["invoice_numbers"] != after["invoice_numbers"]:
        diff_bits.append(
            f"invoices {before['invoice_numbers'] or '—'} -> {after['invoice_numbers'] or '—'}"
        )
    if before["rule_id"] != after["rule_id"]:
        diff_bits.append(f"rule {before['rule_id']} -> {after['rule_id']}")
    history_comment = "Reopened with edits" + (f": {'; '.join(diff_bits)}" if diff_bits else " (no changes)")
    if (comment or "").strip():
        history_comment += f" | {comment.strip()}"

    db.add(RowStatusHistory(
        line_item_id=r.id,
        from_state="overpayment_parked" if was_parked else "rejected",
        to_state=_state_value(r.current_state),
        trigger="spoc_reopen_with_edits",
        rule_id=r.rule_id,
        triggered_by=user.email if user else None,
        comment=history_comment,
    ))

    try:
        r.oracle_payload = build_receipt_creation_payload(r)
    except Exception:
        pass  # never let a payload-preview rebuild block the reopen itself

    db.commit()

    logger.info(
        "[reopen_with_edits] row=%s kind=%s applied=%s | rule %s -> %s",
        r.id, kind, applied or ["none"], before["rule_id"], after["rule_id"],
    )

    return {
        "success": True,
        "id": r.id,
        "applied": applied,
        "from": before,
        "to": after,
        "current_state": _state_value(r.current_state),
        "bucket_pinned_by": _bucket_pinned_by(r),
        "message": outcome.get("message") or (
            "Row reopened. " + (
                "Its outcome was recomputed from your edits."
                if applied else "Nothing was changed, so its previous outcome stands."
            )
        ),
    }
