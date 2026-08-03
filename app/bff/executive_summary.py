"""
app.bff.executive_summary  —  /api/executive-summary/*
========================================================
Executive Summary Dashboard.

PATCH NOTE (two-step Oracle receipt flow):
  A bare Oracle receipt is now created for EVERY credit row during Bank
  Reconciliation (rule_engine/orchestrator.py's Step 4.5), regardless of
  category — tracked via LineItem.oracle_post_status. Invoice mapping
  (attaching remittanceReferences to that receipt) still only happens for
  ready_for_oracle rows at SPOC-approval time, tracked via the separate
  LineItem.reference_status. "Posted" in this file always means
  reference_status == "success" — a row having a bare receipt
  (oracle_post_status == "success") is NOT sufficient, since every row
  gets one of those regardless of whether it's actually been reviewed.
  Dates in this file (the "Posted Date" column, date_from/date_to filters)
  are reference_added_at (when invoice mapping happened), not
  oracle_posted_at (when the bare receipt was created — every row, not
  meaningful for an audit view of what's actually been posted).

POSTED VIEW  ("Invoice Mapped" conceptually — see PATCH note above)
-----------
Only rows where LineItem.reference_status == "success" — i.e. rows that
have actually completed invoice mapping in Oracle Fusion. There are exactly THREE
categories of row that ever reach Oracle (confirmed against the rule
engine / state machine):
    - Full Payment              (credited == outstanding)
    - Acceptable Short Payment  (R9b — within policy tolerance)
    - Cross Currency            (statement_currency != invoice_currency,
                                  i.e. Leg-1 FX was applied)
A row can be BOTH "Acceptable Short Payment" AND "Cross Currency" at once
(e.g. a short payment that also happened to arrive in a different
currency) — these two are independent lenses, not mutually exclusive
buckets. "Full Payment" and "Acceptable Short Payment" ARE mutually
exclusive (every posted row is exactly one of the two).

NON-POSTED OVERVIEW
--------------------
Everything that has NOT reached Oracle yet, reusing the exact same
group taxonomy as the main Dashboard / Analysis History page
(see app.bff.metrics._category_for_row — the single source of truth for
"which bucket does this row belong in" everywhere else in the app):
    unidentified | needs_remittance | needs_distribution | short_payment |
    conflict_exception | rejected | post_failed
("ready_for_oracle" (exact match only, as of the short-payment split) and
"processed" are deliberately excluded here — ready_for_oracle rows haven't
failed or stalled, they're just pending an action, and processed rows are
by definition posted, so they live in the Posted view instead.
needs_distribution and short_payment ARE included, on request, for their
own exec-level visibility even though they're also technically "pending
an action" — see NON_POSTED_GROUPS's own comment.)
Plus a separate Cross-OU tag (`is_cross_ou_currency`), since that's a
flag that can co-occur with any of the above states, not a state itself.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db.models import AnalysisRun, LineItem, User
from ..deps import get_db
from ..auth import require_permission
from .metrics import (
    GROUP_CONFLICT_EXCEPTION, GROUP_NEEDS_REMITTANCE, GROUP_POST_FAILED,
    GROUP_REJECTED, GROUP_UNIDENTIFIED, GROUP_NEEDS_DISTRIBUTION, GROUP_SHORT_PAYMENT,
    GROUP_LABELS, _category_for_row,
)
from .date_range import parse_date_from, parse_date_to

router = APIRouter()


def _apply_run_by(q, run_by: Optional[str]):
    """
    Shared by both _base_query (posted) and _non_posted_base_query.
    PATCH: was RowStatusHistory.triggered_by ("approved_by") — that only
    exists once a human has actually approved/rejected a row, so this
    stayed unusable for any run nobody had done HITL work on yet. Uses
    AnalysisRun.triggered_by instead (who STARTED the run — known
    immediately, same field bff/metrics.py and Analysis History's
    "Started By" filter now use).
    """
    if not run_by:
        return q
    matching_run_ids = (
        q.session.query(AnalysisRun.run_id)
        .filter(AnalysisRun.triggered_by == run_by)
        .subquery()
    )
    return q.filter(LineItem.run_id.in_(matching_run_ids))


# ── POSTED — the only 3 real audit categories ────────────────────────────────

POSTED_PILL_DEFINITIONS: list[dict] = [
    {"key": "full_payment",   "label": "Full Payment"},
    {"key": "short_payment",  "label": "Acceptable Short Payment"},
    {"key": "cross_currency", "label": "Cross Currency"},
]


def _posted_tags(r: LineItem) -> list[str]:
    """Returns every posted-pill key that applies to this posted row."""
    tags: list[str] = []

    target = float(r.target_total or 0)
    received = float(r.credit_amount or 0)
    variance = target - received
    tags.append("full_payment" if abs(variance) < 0.01 else "short_payment")

    if r.is_cross_currency:
        tags.append("cross_currency")

    return tags


def _base_query(
    db: Session,
    bank_name: Optional[str],
    business_unit: Optional[str],
    ou_number: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    run_id: Optional[int],
    run_by: Optional[str] = None,
):
    # PATCH: was oracle_post_status == "success" — that now means only "a
    # bare receipt was created during reconciliation" (every row gets
    # one). "Posted" means invoice mapping actually completed —
    # reference_status is that outcome.
    q = db.query(LineItem).filter(LineItem.reference_status == "success")
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    if bank_name:
        q = q.filter(LineItem.bank_name == bank_name)
    if business_unit:
        q = q.filter(LineItem.business_unit == business_unit)
    if ou_number:
        q = q.filter(LineItem.ou_number == ou_number)
    if date_from:
        # PATCH: was oracle_posted_at (receipt-creation time — every row
        # has one). reference_added_at is when invoice mapping happened,
        # which is what "Posted Date" means for this audit view.
        q = q.filter(LineItem.reference_added_at >= parse_date_from(date_from))
    if date_to:
        q = q.filter(LineItem.reference_added_at <= parse_date_to(date_to))
    q = _apply_run_by(q, run_by)
    return q


def _serialize_record(r: LineItem) -> dict:
    tags = _posted_tags(r)
    return {
        "id": r.id,
        "run_id": r.run_id,
        "bank_name": r.bank_name,
        "account_number": r.account_number,
        "business_unit": r.business_unit,
        "ou_number": r.ou_number,
        "statement_date": r.statement_date.isoformat() if r.statement_date else None,
        "narrative": r.narrative,
        "credit_amount": float(r.credit_amount or 0),
        "statement_currency": r.statement_currency,
        "invoice_currency": r.invoice_currency,
        "functional_currency": r.functional_currency,
        "customer_name": (r.matched_invoices or [{}])[0].get("customer_name") if r.matched_invoices else None,
        "invoice_numbers": ",".join(m.get("invoice_number", "") for m in (r.matched_invoices or [])),
        "target_total": float(r.target_total or 0),
        "shortfall_pct": r.shortfall_pct,
        "rule_id": r.rule_id,
        "reason_code": r.reason_code,
        "hitl_status": r.hitl_status,
        "oracle_ref_no": r.oracle_ref_no,
        "oracle_status_code": r.oracle_status_code,
        "standard_receipt_id": r.standard_receipt_id,
        # PATCH: was r.oracle_posted_at (bare receipt-creation time — set
        # for every row regardless of category). This key name is kept for
        # frontend compatibility, but now sources reference_added_at (when
        # invoice mapping completed), which is what "Posted Date" means
        # for this specific audit view.
        "oracle_posted_at": r.reference_added_at.isoformat() if r.reference_added_at else None,
        "receipt_created_at": r.oracle_posted_at.isoformat() if r.oracle_posted_at else None,
        "tags": tags,
        "tags_label": ", ".join(next(p["label"] for p in POSTED_PILL_DEFINITIONS if p["key"] == t) for t in tags),
    }


@router.get("/filters")
def get_executive_filters(mode: str = "posted", db: Session = Depends(get_db),
                          user: User = Depends(require_permission("run:view"))):
    """Dropdown options for Bank / Business Unit.

    `mode` scopes the population the options are drawn from, so the dropdown
    never shows a bank/BU that has zero rows in the view currently being
    looked at:
      mode=posted     -> only rows already posted to Oracle (Posted Records tab)
      mode=non_posted -> only rows NOT yet posted (Non-Posted Overview tab)
    """
    q = db.query(LineItem)
    if mode == "posted":
        # PATCH: was oracle_post_status — see module PATCH note at top of file
        q = q.filter(LineItem.reference_status == "success")
    else:
        q = q.filter(or_(LineItem.reference_status.is_(None), LineItem.reference_status != "success"))
    banks = sorted({v for (v,) in q.with_entities(LineItem.bank_name).distinct() if v})
    bus = sorted({v for (v,) in q.with_entities(LineItem.business_unit).distinct() if v})
    pills = POSTED_PILL_DEFINITIONS if mode == "posted" else NON_POSTED_PILL_DEFINITIONS

    # "users" — sourced from AnalysisRun.triggered_by (who STARTED the run
    # that produced these line items), not RowStatusHistory (who
    # approved/rejected a row) — known immediately for every run, no
    # waiting on HITL activity. Same field bff/filters_routes.py and
    # Analysis History's "Started By" filter use.
    run_ids_subq = q.with_entities(LineItem.run_id).distinct().subquery()
    uq = (
        db.query(AnalysisRun.triggered_by)
        .filter(AnalysisRun.run_id.in_(db.query(run_ids_subq.c.run_id)))
        .filter(AnalysisRun.triggered_by.isnot(None))
    )
    users = sorted({u for (u,) in uq.distinct() if u})

    return {"banks": banks, "business_units": bus, "pills": pills, "users": users}


@router.get("/summary")
def get_executive_summary(
    bank_name: Optional[str] = None,
    business_unit: Optional[str] = None,
    ou_number: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    run_id: Optional[int] = None,
    run_by: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    rows = _base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, run_by).all()

    pill_counts = {p["key"]: 0 for p in POSTED_PILL_DEFINITIONS}
    total_amount = 0.0
    by_bank: dict[str, dict] = {}
    by_bu: dict[str, dict] = {}

    for r in rows:
        amt = float(r.credit_amount or 0)
        total_amount += amt

        for tag in _posted_tags(r):
            pill_counts[tag] += 1

        bk = r.bank_name or "Unknown"
        by_bank.setdefault(bk, {"count": 0, "amount": 0.0})
        by_bank[bk]["count"] += 1
        by_bank[bk]["amount"] += amt

        bu = r.business_unit or "Unknown"
        by_bu.setdefault(bu, {"count": 0, "amount": 0.0})
        by_bu[bu]["count"] += 1
        by_bu[bu]["amount"] += amt

    return {
        "total_posted": len(rows),
        "total_amount": round(total_amount, 2),
        "pills": [
            {"key": p["key"], "label": p["label"], "count": pill_counts[p["key"]]}
            for p in POSTED_PILL_DEFINITIONS
        ],
        "by_bank": [
            {"bank_name": k, "count": v["count"], "amount": round(v["amount"], 2)}
            for k, v in sorted(by_bank.items())
        ],
        "by_business_unit": [
            {"business_unit": k, "count": v["count"], "amount": round(v["amount"], 2)}
            for k, v in sorted(by_bu.items())
        ],
    }


@router.get("/records")
def get_executive_records(
    bank_name: Optional[str] = None,
    business_unit: Optional[str] = None,
    ou_number: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    run_id: Optional[int] = None,
    category: Optional[str] = None,
    run_by: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    q = _base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, run_by)
    q = q.order_by(LineItem.reference_added_at.desc())  # PATCH: was oracle_posted_at — see _base_query note
    rows = q.all()

    if category:
        rows = [r for r in rows if category in _posted_tags(r)]

    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    return {
        "data": [_serialize_record(r) for r in page_rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/export")
def export_executive_csv(
    bank_name: Optional[str] = None,
    business_unit: Optional[str] = None,
    ou_number: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    run_id: Optional[int] = None,
    category: Optional[str] = None,
    run_by: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    """Streams a CSV of every posted record matching the current filters —
    same filter contract as /records, so what the user sees on screen is
    exactly what they download."""
    q = _base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, run_by)
    q = q.order_by(LineItem.reference_added_at.desc())  # PATCH: was oracle_posted_at — see _base_query note
    rows = q.all()

    if category:
        rows = [r for r in rows if category in _posted_tags(r)]

    records = [_serialize_record(r) for r in rows]

    buffer = io.StringIO()
    fieldnames = [
        "id", "run_id", "bank_name", "account_number", "business_unit", "ou_number",
        "statement_date", "narrative", "credit_amount", "statement_currency",
        "invoice_currency", "functional_currency", "customer_name", "invoice_numbers",
        "target_total", "shortfall_pct", "rule_id", "reason_code", "hitl_status",
        "oracle_ref_no", "oracle_status_code", "standard_receipt_id",
        "oracle_posted_at", "tags_label",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow(rec)

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=executive_summary_posted_records.csv"},
    )


# ── NON-POSTED OVERVIEW ───────────────────────────────────────────────────────
# Everything that has NOT reached Oracle yet. Reuses the same group taxonomy
# as the main Dashboard (metrics._category_for_row) so these counts can
# never disagree with what the operational worklist shows, plus a
# stand-alone Cross-OU tag since that's a flag, not a state.

# NON_POSTED_GROUPS previously excluded ready_for_oracle (merged R9a+R9b)
# on the grounds that "these rows haven't failed or stalled, they're just
# pending an action" — same logic would argue against including
# short_payment too, since operationally it's still just pending Approve.
# Added anyway, on request: these two are worth exec-level visibility in
# their own right — needs_distribution is real, unposted exposure sitting
# in consolidated bank lines (credit card / cheque / third-party) with NO
# receipt yet, and short_payment is an open shortfall against an invoice
# that finance may want visibility into even though the row itself isn't
# "stuck". GROUP_READY_FOR_ORACLE (now exact-match only) and GROUP_PROCESSED
# remain excluded for the original reason.
NON_POSTED_GROUPS = [
    GROUP_UNIDENTIFIED, GROUP_NEEDS_REMITTANCE, GROUP_NEEDS_DISTRIBUTION,
    GROUP_SHORT_PAYMENT, GROUP_CONFLICT_EXCEPTION, GROUP_REJECTED, GROUP_POST_FAILED,
]

# NOTE: no standalone "Cross OU" pill here. is_cross_ou_currency is only ever
# set on rows that already matched and reached ready_to_post /
# acceptable_short_payment (see db.models.LineItem docstring) — a row that's
# blocked because the OU didn't match in the first place lands in
# Conflict / Exception via rule R14 (WRONG_OU_PAYMENT) instead. A separate
# Cross-OU pill here would always read 0 and just duplicate that bucket.
NON_POSTED_PILL_DEFINITIONS: list[dict] = [
    {"key": g, "label": GROUP_LABELS[g]} for g in NON_POSTED_GROUPS
]


def _non_posted_base_query(
    db: Session,
    bank_name: Optional[str],
    business_unit: Optional[str],
    ou_number: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    run_id: Optional[int],
    run_by: Optional[str] = None,
):
    # NOTE: reference_status is NULL for every row that hasn't been
    # invoice-mapped yet (the vast majority of "non-posted" rows — includes
    # rows with a bare receipt already created, since that's a separate
    # field now — see module PATCH note). Plain `!= "success"` would
    # silently exclude all of those, since SQL's NULL comparison semantics
    # make `NULL != 'success'` evaluate to UNKNOWN, not TRUE. Explicitly OR
    # in the NULL case.
    q = db.query(LineItem).filter(
        or_(LineItem.reference_status.is_(None), LineItem.reference_status != "success")
    )
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    if bank_name:
        q = q.filter(LineItem.bank_name == bank_name)
    if business_unit:
        q = q.filter(LineItem.business_unit == business_unit)
    if ou_number:
        q = q.filter(LineItem.ou_number == ou_number)
    if date_from:
        # PATCH: was LineItem.statement_date (the bank transaction's own
        # date) — same fix as bff/metrics.py: a Today/Yesterday/WTD/MTD-
        # style pill means "when did we process this", not "what date is
        # on the statement".
        q = q.filter(LineItem.created_at >= parse_date_from(date_from))
    if date_to:
        q = q.filter(LineItem.created_at <= parse_date_to(date_to))
    q = _apply_run_by(q, run_by)
    return q


def _serialize_non_posted_record(r: LineItem) -> dict:
    category = _category_for_row(r)
    return {
        "id": r.id,
        "run_id": r.run_id,
        "bank_name": r.bank_name,
        "business_unit": r.business_unit,
        "ou_number": r.ou_number,
        "statement_date": r.statement_date.isoformat() if r.statement_date else None,
        "narrative": r.narrative,
        "credit_amount": float(r.credit_amount or 0),
        "statement_currency": r.statement_currency,
        "extracted_customer_name": r.extracted_customer_name,
        "current_state": r.current_state,
        "hitl_status": r.hitl_status,
        "reason_code": r.reason_code,
        "rule_id": r.rule_id,
        "is_cross_ou_currency": bool(r.is_cross_ou_currency),
        "category": category,
        "category_label": GROUP_LABELS.get(category, category),
    }


@router.get("/non-posted/summary")
def get_non_posted_summary(
    bank_name: Optional[str] = None,
    business_unit: Optional[str] = None,
    ou_number: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    run_id: Optional[int] = None,
    run_by: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    rows = _non_posted_base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, run_by).all()

    pill_counts = {g: 0 for g in NON_POSTED_GROUPS}

    for r in rows:
        cat = _category_for_row(r)
        if cat in pill_counts:
            pill_counts[cat] += 1

    return {
        "total_non_posted": len(rows),
        "pills": [
            {"key": p["key"], "label": p["label"], "count": pill_counts.get(p["key"], 0)}
            for p in NON_POSTED_PILL_DEFINITIONS
        ],
    }


@router.get("/non-posted/records")
def get_non_posted_records(
    bank_name: Optional[str] = None,
    business_unit: Optional[str] = None,
    ou_number: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    run_id: Optional[int] = None,
    category: Optional[str] = None,
    run_by: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    rows = _non_posted_base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, run_by).all()

    if category:
        rows = [r for r in rows if _category_for_row(r) == category]

    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    return {
        "data": [_serialize_non_posted_record(r) for r in page_rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }