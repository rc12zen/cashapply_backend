"""app.bff.results_routes — /api/results/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import LineItem, User
from ..deps import get_db
from ..auth import require_permission
from ..auth.permissions import get_user_permission_codes
from ..audit.service import log_activity
from .metrics import compute_metrics, compute_run_summary
from .row_detail import build_row_detail
from .shortage import compute_shortage_summary

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/metrics")
def get_metrics(run_id: int | None = None, date_from: str | None = None,
                 date_to: str | None = None, bank_name: str | None = None,
                 business_unit: str | None = None, run_by: str | None = None,
                 db: Session = Depends(get_db),
                 user: User = Depends(require_permission("run:view"))):
    return compute_metrics(db, run_id=run_id, date_from=date_from, date_to=date_to,
                            bank_name=bank_name, business_unit=business_unit,
                            run_by=run_by)


@router.get("/run-summary/{run_id}")
def get_run_summary(run_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("run:view"))):
    return compute_run_summary(db, run_id)


@router.get("/row-detail/{record_id}")
def get_row_detail(record_id: int, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("run:view"))):
    return build_row_detail(db, record_id, user_permission_codes=get_user_permission_codes(db, user))


@router.get("/row-detail/{record_id}/reveal-account-number")
def reveal_row_account_number(record_id: int, request: Request,
                               db: Session = Depends(get_db),
                               user: User = Depends(require_permission("run:view"))):
    """Full bank_account_number, on demand -- same permission as
    get_row_detail above. Audit-logged so pulling the real number is a
    deliberate, traceable action rather than something that ships in every
    row-detail response (VAPT remediation)."""
    row = db.query(LineItem).get(record_id)
    if not row:
        raise AppError(ErrorCode.ROW_NOT_FOUND)

    log_activity(
        db, user, action="line_item.account_number_revealed", entity_type="LineItem",
        entity_id=record_id, ip_address=_client_ip(request),
    )
    db.commit()
    return {"account_number": row.account_number}


@router.get("/not-found")
def get_not_found(db: Session = Depends(get_db),
                   user: User = Depends(require_permission("run:view"))):
    from .metrics import get_unidentified_rows
    return get_unidentified_rows(db)


@router.get("/validation-failures")
def get_validation_failures(db: Session = Depends(get_db),
                             user: User = Depends(require_permission("run:view"))):
    from .metrics import get_conflict_rows
    return get_conflict_rows(db)


@router.get("/processed-shortage-summary")
def get_processed_shortage_summary(
    run_id: int | None = None, date_from: str | None = None,
    date_to: str | None = None, bank_name: str | None = None,
    business_unit: str | None = None, db: Session = Depends(get_db),
    user: User = Depends(require_permission("run:view")),
):
    return compute_shortage_summary(db, run_id=run_id, date_from=date_from, date_to=date_to,
                                     bank_name=bank_name, business_unit=business_unit)