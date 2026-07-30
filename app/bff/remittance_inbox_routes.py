"""
app.bff.remittance_inbox_routes
==================================
/api/remittance-inbox/* — read-only browse view over every remittance
email/document App2 has extracted (RemittanceExtraction), independent of
any specific bank row. New feature: previously the only way to see a
remittance was the reverse direction — open a specific row and look at
its (at most one) matched remittance in the row-detail panel
(bff/row_detail.py). This lists ALL of them, newest first, and shows
whether each one has actually been matched to a row yet.

Matching a remittance -> row is NOT recomputed live here. LineItem already
persists remittance_extraction_id the moment a match is made (during a
normal run, a manual recheck, or a customer-name correction re-match —
see rule_engine/remittance_recheck.py and customer_name_correction.py).
So "matched" here is just: does any LineItem reference this extraction's
id. A remittance could in principle end up referenced by more than one
LineItem (e.g. re-matched after a correction, or amount/date coincidence
across runs) -- every match found is returned, not just the first.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from ..db.models import LineItem, RemittanceExtraction, User
from ..deps import get_db
from ..auth import require_permission
from .metrics import _category_for_row, GROUP_LABELS

router = APIRouter()


@router.get("/list")
def get_remittance_inbox(
    page: int = 1, page_size: int = 50,
    search: str | None = None,
    # "matched" | "unmatched" | None (None/omitted = both)
    status: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("run:view")),
):
    q = db.query(RemittanceExtraction)

    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            RemittanceExtraction.subject.ilike(like),
            RemittanceExtraction.sender.ilike(like),
            RemittanceExtraction.raw_payer_text.ilike(like),
            RemittanceExtraction.raw_customer_text.ilike(like),
            RemittanceExtraction.payment_reference.ilike(like),
            RemittanceExtraction.filename.ilike(like),
        ))

    # PATCH: matched/unmatched filter. A plain LEFT JOIN + "line_item id IS
    # (NOT) NULL" would double-count an extraction referenced by more than
    # one LineItem (inflating `total`/pagination), so this filters by a
    # correlated EXISTS instead -- one row per extraction either way.
    matched_exists = (
        db.query(LineItem.id)
        .filter(LineItem.remittance_extraction_id == RemittanceExtraction.id)
        .exists()
    )
    if status == "matched":
        q = q.filter(matched_exists)
    elif status == "unmatched":
        q = q.filter(~matched_exists)

    total = q.count()
    rows = (
        q.order_by(desc(RemittanceExtraction.extracted_at))
        .offset((page - 1) * page_size).limit(page_size).all()
    )

    # One extra query per page (not per row) for the LineItems that
    # reference any extraction on this page -- avoids an N+1 across
    # potentially 50 rows.
    ext_ids = [r.id for r in rows]
    matched_line_items = (
        db.query(LineItem)
        .filter(LineItem.remittance_extraction_id.in_(ext_ids))
        .all()
        if ext_ids else []
    )
    matches_by_ext: dict[int, list[LineItem]] = {}
    for li in matched_line_items:
        matches_by_ext.setdefault(li.remittance_extraction_id, []).append(li)

    data = []
    for ext in rows:
        matches = matches_by_ext.get(ext.id, [])
        data.append({
            "id": ext.id,
            "subject": ext.subject,
            "sender": ext.sender,
            "payer": ext.raw_payer_text,
            "customer_name": ext.raw_customer_text,
            "payment_reference": ext.payment_reference,
            "payment_date": ext.payment_date.isoformat() if ext.payment_date else None,
            "payment_amount": float(ext.payment_amount) if ext.payment_amount is not None else None,
            "payment_currency": ext.payment_currency,
            "filename": ext.filename,
            "extracted_at": ext.extracted_at.isoformat() if ext.extracted_at else None,
            "matched": len(matches) > 0,
            "matches": [{
                "line_item_id": li.id,
                "run_id": li.run_id,
                "category": GROUP_LABELS.get(_category_for_row(li), ""),
            } for li in matches],
        })

    return {"data": data, "total": total, "page": page, "page_size": page_size}