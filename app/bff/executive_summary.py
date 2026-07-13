"""
app.bff.executive_summary  —  /api/executive-summary/*
========================================================
Executive Summary Dashboard.

POSTED VIEW
-----------
Only rows where LineItem.oracle_post_status == "success" — i.e. rows that
have actually been posted to Oracle Fusion. There are exactly THREE
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
7-group taxonomy as the main Dashboard / Analysis History page
(see app.bff.metrics._category_for_row — the single source of truth for
"which bucket does this row belong in" everywhere else in the app):
    unidentified | needs_remittance | conflict_exception | rejected | post_failed
("ready_for_oracle" and "processed" are deliberately excluded here —
ready_for_oracle rows haven't failed or stalled, they're just pending an
action, and processed rows are by definition posted, so they live in the
Posted view instead.)
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

from ..db.models import LineItem, RowStatusHistory, User
from ..deps import get_db
from .metrics import (
    GROUP_CONFLICT_EXCEPTION, GROUP_NEEDS_REMITTANCE, GROUP_POST_FAILED,
    GROUP_REJECTED, GROUP_UNIDENTIFIED, GROUP_LABELS, _category_for_row,
)

router = APIRouter()


def _apply_approved_by(q, approved_by: Optional[str]):
    """
    Shared by both _base_query (posted) and _non_posted_base_query.
    Same approach as bff/metrics.py's compute_metrics: RowStatusHistory.
    triggered_by is the acting user's email, recorded on every
    spoc_approve/spoc_reject transition — there's no approved_by column on
    LineItem itself.
    """
    if not approved_by:
        return q
    matching_ids = (
        q.session.query(RowStatusHistory.line_item_id)
        .filter(RowStatusHistory.triggered_by == approved_by)
        .subquery()
    )
    return q.filter(LineItem.id.in_(matching_ids))


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
    approved_by: Optional[str] = None,
):
    q = db.query(LineItem).filter(LineItem.oracle_post_status == "success")
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    if bank_name:
        q = q.filter(LineItem.bank_name == bank_name)
    if business_unit:
        q = q.filter(LineItem.business_unit == business_unit)
    if ou_number:
        q = q.filter(LineItem.ou_number == ou_number)
    if date_from:
        q = q.filter(LineItem.oracle_posted_at >= date_from)
    if date_to:
        q = q.filter(LineItem.oracle_posted_at <= date_to)
    q = _apply_approved_by(q, approved_by)
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
        "oracle_posted_at": r.oracle_posted_at.isoformat() if r.oracle_posted_at else None,
        "tags": tags,
        "tags_label": ", ".join(next(p["label"] for p in POSTED_PILL_DEFINITIONS if p["key"] == t) for t in tags),
    }


@router.get("/filters")
def get_executive_filters(mode: str = "posted", db: Session = Depends(get_db)):
    """Dropdown options for Bank / Business Unit.

    `mode` scopes the population the options are drawn from, so the dropdown
    never shows a bank/BU that has zero rows in the view currently being
    looked at:
      mode=posted     -> only rows already posted to Oracle (Posted Records tab)
      mode=non_posted -> only rows NOT yet posted (Non-Posted Overview tab)
    """
    q = db.query(LineItem)
    if mode == "posted":
        q = q.filter(LineItem.oracle_post_status == "success")
    else:
        q = q.filter(or_(LineItem.oracle_post_status.is_(None), LineItem.oracle_post_status != "success"))
    banks = sorted({v for (v,) in q.with_entities(LineItem.bank_name).distinct() if v})
    bus = sorted({v for (v,) in q.with_entities(LineItem.business_unit).distinct() if v})
    pills = POSTED_PILL_DEFINITIONS if mode == "posted" else NON_POSTED_PILL_DEFINITIONS

    # Same approach as bff/filters_routes.py's get_filter_options — join
    # against the real users table so only genuine registered people show
    # up (RowStatusHistory is also written by the rule engine's own
    # automatic categorization with triggered_by="system", which is not a
    # real user).
    uq = (
        db.query(RowStatusHistory.triggered_by)
        .join(User, User.email == RowStatusHistory.triggered_by)
        .join(LineItem, LineItem.id == RowStatusHistory.line_item_id)
    )
    if mode == "posted":
        uq = uq.filter(LineItem.oracle_post_status == "success")
    else:
        uq = uq.filter(or_(LineItem.oracle_post_status.is_(None), LineItem.oracle_post_status != "success"))
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
    approved_by: Optional[str] = None,
    db: Session = Depends(get_db),
):
    rows = _base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, approved_by).all()

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
    approved_by: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    q = _base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, approved_by)
    q = q.order_by(LineItem.oracle_posted_at.desc())
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
    approved_by: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Streams a CSV of every posted record matching the current filters —
    same filter contract as /records, so what the user sees on screen is
    exactly what they download."""
    q = _base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, approved_by)
    q = q.order_by(LineItem.oracle_posted_at.desc())
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

NON_POSTED_GROUPS = [GROUP_UNIDENTIFIED, GROUP_NEEDS_REMITTANCE, GROUP_CONFLICT_EXCEPTION, GROUP_REJECTED, GROUP_POST_FAILED]

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
    approved_by: Optional[str] = None,
):
    # NOTE: oracle_post_status is NULL for every row that hasn't gone through
    # Oracle posting yet (the vast majority of "non-posted" rows). Plain
    # `!= "success"` would silently exclude all of those, since SQL's NULL
    # comparison semantics make `NULL != 'success'` evaluate to UNKNOWN, not
    # TRUE — so the WHERE clause would only ever match explicit non-success
    # strings like "failed". Explicitly OR in the NULL case.
    q = db.query(LineItem).filter(
        or_(LineItem.oracle_post_status.is_(None), LineItem.oracle_post_status != "success")
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
        q = q.filter(LineItem.created_at >= date_from)
    if date_to:
        end_of_day = dt.datetime.strptime(date_to, "%Y-%m-%d") + dt.timedelta(days=1) - dt.timedelta(microseconds=1)
        q = q.filter(LineItem.created_at <= end_of_day)
    q = _apply_approved_by(q, approved_by)
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
    approved_by: Optional[str] = None,
    db: Session = Depends(get_db),
):
    rows = _non_posted_base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, approved_by).all()

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
    approved_by: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    rows = _non_posted_base_query(db, bank_name, business_unit, ou_number, date_from, date_to, run_id, approved_by).all()

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