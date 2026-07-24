"""
app.bff.bank_accounts_routes
==============================
/api/bank-accounts/* — a simple, read-mostly "reference" page: which Bank
Accounts exist and which Business Unit(s) they belong to. Requested as a
nav-bar info page anyone with view access can check, plus an
Administrator-only action to change an account's Business Unit(s).

MULTI-BU: an account normally belongs to exactly one Business Unit (its
`ou_id` FK on BankAccount) — but some accounts legitimately receive
payments for more than one Business Unit, so an account can also have
"additional" Business Units via the BankAccountOU join table (see
db/models.py). `all_organization_units` / `all_ou_numbers` on the model
gives primary + additional together.

IMPORTANT — WHEN A CHANGE TAKES EFFECT: changing an account's Business
Unit(s) here only affects analysis runs started AFTER the change.
Already-completed runs are never touched — LineItem.business_unit is a
permanent snapshot taken at the time that run actually happened, not a
live join to this table (see rule_engine/orchestrator.py, which re-
resolves the CURRENT Business Unit(s) fresh each time a new run starts).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import BankAccount, BankAccountOU, OrganizationUnit, User
from ..deps import get_db
from ..auth import require_permission
from ..audit.service import log_activity

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _account_dict(a: BankAccount) -> dict:
    primary = a.organization_unit
    additional = list(a.additional_ous)
    return {
        "id": a.id,
        "bank_name": a.bank_name,
        "account_number": a.account_number,
        "account_last4": a.account_last4,
        "display_name": a.display_name,
        "currency": a.currency,
        "active": a.active,
        "primary_business_unit": {
            "ou_number": primary.ou_number,
            "ou_name": primary.ou_name,
            "functional_currency": primary.functional_currency,
        } if primary else None,
        "additional_business_units": [
            {"ou_number": ou.ou_number, "ou_name": ou.ou_name, "functional_currency": ou.functional_currency}
            for ou in additional
        ],
        "is_multi_bu": len(additional) > 0,
    }


@router.get("")
def list_bank_accounts(db: Session = Depends(get_db),
                        user: User = Depends(require_permission("run:view"))):
    """The nav-bar 'Bank Accounts' info page — every onboarded account and
    the Business Unit(s) it belongs to. View-only for every role except
    Viewer (same tier as Config/Overview/etc)."""
    accounts = db.query(BankAccount).order_by(BankAccount.bank_name, BankAccount.account_number).all()
    return {"accounts": [_account_dict(a) for a in accounts]}


@router.get("/business-units")
def list_business_units(db: Session = Depends(get_db),
                         user: User = Depends(require_permission("run:view"))):
    """Every known Business Unit (OrganizationUnit row) — powers the
    primary/additional Business Unit pickers on this page."""
    ous = db.query(OrganizationUnit).filter(OrganizationUnit.active.is_(True)).order_by(OrganizationUnit.ou_name).all()
    return {
        "business_units": [
            {"ou_number": ou.ou_number, "ou_name": ou.ou_name, "functional_currency": ou.functional_currency}
            for ou in ous
        ]
    }


class UpdateBusinessUnitsRequest(BaseModel):
    primary_ou_number: str
    additional_ou_numbers: list[str] = Field(default_factory=list)


@router.put("/{account_id}/business-units")
def update_business_units(account_id: int, body: UpdateBusinessUnitsRequest, request: Request,
                           db: Session = Depends(get_db),
                           user: User = Depends(require_permission("config:author"))):
    """Administrator-only: change which Business Unit(s) a bank account
    belongs to. ONLY affects analysis runs started from now on — see
    module docstring. Nothing here touches any past run/LineItem."""
    account = db.query(BankAccount).get(account_id)
    if not account:
        raise AppError(ErrorCode.BANK_ACCOUNT_NOT_FOUND)

    primary_number = (body.primary_ou_number or "").strip()
    if not primary_number:
        raise AppError(ErrorCode.BUSINESS_UNIT_REQUIRED)

    all_requested = {primary_number, *(n.strip() for n in body.additional_ou_numbers if n.strip())}
    ous_by_number = {
        ou.ou_number: ou
        for ou in db.query(OrganizationUnit).filter(OrganizationUnit.ou_number.in_(all_requested)).all()
    }
    missing = all_requested - set(ous_by_number)
    if missing:
        raise AppError(ErrorCode.BUSINESS_UNIT_UNKNOWN, detail=", ".join(sorted(missing)))

    old_primary = account.organization_unit.ou_number if account.organization_unit else None
    old_additional = sorted(ou.ou_number for ou in account.additional_ous)

    account.ou_id = ous_by_number[primary_number].id
    additional_numbers = sorted(n for n in all_requested if n != primary_number)
    account.additional_ou_links = [
        BankAccountOU(ou_id=ous_by_number[n].id) for n in additional_numbers
    ]
    db.flush()

    log_activity(
        db, user, action="bank_account.business_units_changed", entity_type="BankAccount",
        entity_id=account.id, ip_address=_client_ip(request),
        metadata={
            "account_number": account.account_number, "bank_name": account.bank_name,
            "primary_business_unit": {"from": old_primary, "to": primary_number},
            "additional_business_units": {"from": old_additional, "to": additional_numbers},
            "effective_from": "next analysis run only -- completed runs are unaffected",
        },
    )
    db.commit()
    db.refresh(account)
    return _account_dict(account)
