"""
app.bff.bank_accounts_routes
==============================
/api/bank-accounts/* — the "Accounts & OU's" reference + management page:
which Bank Accounts exist, which Business Unit(s) they belong to, and the
full roster of every Organization Unit (name + functional currency).
Requested as a nav-bar page anyone with view access can check, plus
writes gated behind the narrower `ou:manage` permission (Administrator,
Analyst, Oracle Operator — see scripts/seed_rbac.py).

PATCH: writes here used to require `config:author` (the same permission
that also unlocks Config Builder recipe-authoring). Split out into its own
`ou:manage` permission so Oracle Operator can hold it too — an OU/Business
Unit name or currency being wrong is squarely an Oracle-posting-accuracy
problem (a receipt 404s if the BusinessUnit string sent to Oracle doesn't
EXACTLY match what Oracle has registered, e.g. "PUNE(111)" vs "Pune(111)"),
so the role that approves/posts to Oracle should be able to fix it directly,
without also getting bank-statement-recipe-authoring access it doesn't need.

MULTI-BU: an account normally belongs to exactly one Business Unit (its
`ou_id` FK on BankAccount) — but some accounts legitimately receive
payments for more than one Business Unit, so an account can also have
"additional" Business Units via the BankAccountOU join table (see
db/models.py). `all_organization_units` / `all_ou_numbers` on the model
gives primary + additional together.

IMPORTANT — WHEN A CHANGE TAKES EFFECT: changing an account's Business
Unit(s), or an OU's own name/currency, here only affects analysis runs
started AFTER the change. Already-completed runs are never touched —
LineItem.business_unit is a permanent snapshot taken at the time that run
actually happened, not a live join to this table (see
rule_engine/orchestrator.py, which re-resolves the CURRENT Business
Unit(s) fresh each time a new run starts). Similarly,
rule_engine/fx_service.py's get_functional_currency() /
get_ou_display_name() read the organization_units table LIVE, with no
caching, so a fix made here is picked up by the very next Oracle retry —
see oracle/fusion_client.py's payload-building — with no extra step needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..common.account_masking import mask_account_number
from ..bank_statement.currency import normalize_currency
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
        # Masked -- the VAPT report flagged full account numbers shipping in
        # every response. Full value is only ever returned by GET
        # /{account_id}/reveal below, which re-checks this same permission
        # and audit-logs the reveal.
        "account_number": mask_account_number(a.account_number),
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


def _ou_dict(ou: OrganizationUnit) -> dict:
    return {
        "ou_number": ou.ou_number,
        "ou_name": ou.ou_name,
        "functional_currency": ou.functional_currency,
        "active": ou.active,
    }


@router.get("")
def list_bank_accounts(db: Session = Depends(get_db),
                        user: User = Depends(require_permission("run:view"))):
    """The nav-bar 'Accounts & OU's' info page — every onboarded account and
    the Business Unit(s) it belongs to. View-only for every role except
    Viewer (same tier as Config/Overview/etc)."""
    accounts = db.query(BankAccount).order_by(BankAccount.bank_name, BankAccount.account_number).all()
    return {"accounts": [_account_dict(a) for a in accounts]}


@router.get("/{account_id}/reveal")
def reveal_account_number(account_id: int, request: Request,
                           db: Session = Depends(get_db),
                           user: User = Depends(require_permission("run:view"))):
    """Full account number, on demand -- same permission as list_bank_accounts
    above (this doesn't unlock anything a viewer couldn't already reach, it
    just makes pulling the real number a deliberate, audited action instead
    of something that ships in every page load)."""
    account = db.query(BankAccount).get(account_id)
    if not account:
        raise AppError(ErrorCode.BANK_ACCOUNT_NOT_FOUND)

    log_activity(
        db, user, action="bank_account.account_number_revealed", entity_type="BankAccount",
        entity_id=account.id, ip_address=_client_ip(request),
    )
    db.commit()
    return {"account_number": account.account_number}


@router.get("/business-units")
def list_business_units(db: Session = Depends(get_db),
                         user: User = Depends(require_permission("run:view"))):
    """
    Every known Business Unit (OrganizationUnit row) — powers the
    primary/additional Business Unit pickers on this page AND the "Accounts
    & OU's" page's own OU roster table. Deliberately includes OUs with NO
    bank account attached yet (e.g. an OU number seen in the aging report
    but not yet onboarded via Config Builder) -- listing accounts alone
    would never surface those, since an account-shaped query can only ever
    show OUs reachable THROUGH an account.
    """
    ous = db.query(OrganizationUnit).filter(OrganizationUnit.active.is_(True)).order_by(OrganizationUnit.ou_name).all()
    return {"business_units": [_ou_dict(ou) for ou in ous]}


class UpdateBusinessUnitsRequest(BaseModel):
    primary_ou_number: str
    additional_ou_numbers: list[str] = Field(default_factory=list)


@router.put("/{account_id}/business-units")
def update_business_units(account_id: int, body: UpdateBusinessUnitsRequest, request: Request,
                           db: Session = Depends(get_db),
                           user: User = Depends(require_permission("ou:manage"))):
    """Change which Business Unit(s) a bank account belongs to. ONLY affects
    analysis runs started from now on — see module docstring. Nothing here
    touches any past run/LineItem."""
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


class CreateOrganizationUnitRequest(BaseModel):
    ou_number: str
    ou_name: str
    functional_currency: str


@router.post("/business-units")
def create_organization_unit(body: CreateOrganizationUnitRequest, request: Request,
                              db: Session = Depends(get_db),
                              user: User = Depends(require_permission("ou:manage"))):
    """
    Create a Business Unit directly, without onboarding a bank statement first.

    WHY THIS EXISTS -- the multi-BU dead end
    -----------------------------------------
    Until now an OrganizationUnit row could only come into being as a
    side-effect of saving a bank-account config in Config Builder (see
    config_builder_routes._get_or_create_organization_unit). But the "additional
    Business Units" picker on this page can only OFFER OUs that already exist,
    and PUT /{account_id}/business-units rejects anything unknown outright.

    So the exact case additional BUs exist FOR was unreachable: one bank account
    receiving money for two Business Units. Adding the second BU required
    onboarding a statement belonging to it -- and for a shared account there
    frequently IS no second statement, so it could never be added at all. The
    only workaround was to find some unrelated statement for that BU, configure
    it purely to bring the OU into existence, then come back here.

    The list endpoint above already assumed OUs could exist independently of
    accounts ("Deliberately includes OUs with NO bank account attached yet");
    nothing ever created them that way. This is that missing path.

    GET THE NAME EXACTLY RIGHT
    ---------------------------
    oracle/fusion_client.py's get_ou_display_name() builds Oracle's
    "BusinessUnit" field as EXACTLY f"{ou_name}({ou_number})" -- e.g.
    "PUNE(111)" -- and Oracle Fusion matches that string character for
    character. A wrong case or a stray space 404s every receipt for this OU,
    with no fuzzy fallback anywhere in the posting path. The create form shows
    the resulting string live for this reason; it is still worth checking
    against Oracle before the first run.

    Same `ou:manage` tier as editing an OU's name/currency below -- that edit
    already carries the identical exact-match risk, so creation is not a
    higher-stakes act than the correction path that already exists.
    """
    ou_number = (body.ou_number or "").strip()
    ou_name = (body.ou_name or "").strip()
    raw_currency = (body.functional_currency or "").strip()

    if not ou_number:
        raise AppError(ErrorCode.CONFIG_FIELD_REQUIRED, detail="OU Number cannot be blank")
    if not ou_name:
        raise AppError(ErrorCode.BUSINESS_UNIT_REQUIRED, detail="Business Unit name cannot be blank")
    if not raw_currency:
        raise AppError(ErrorCode.CONFIG_FIELD_REQUIRED, detail="Functional Currency cannot be blank")

    # Standardise to an ISO-4217 code the same way every other currency entry
    # point does (bank_statement/currency.py). functional_currency drives real
    # Oracle FX conversion, and once set it is never overwritten by a later
    # account save -- only by the edit endpoint below. A typo here is therefore
    # effectively permanent until someone notices wrong conversions.
    currency = normalize_currency(raw_currency)
    if not currency:
        raise AppError(
            ErrorCode.CONFIG_FIELD_REQUIRED,
            detail=f"Functional Currency '{raw_currency}' is not a recognised ISO-4217 code",
        )

    existing = db.query(OrganizationUnit).filter(OrganizationUnit.ou_number == ou_number).first()
    if existing:
        raise AppError(
            ErrorCode.ORGANIZATION_UNIT_EXISTS,
            detail=f"{ou_number} is already set up as '{existing.ou_name}'",
        )

    ou = OrganizationUnit(ou_number=ou_number, ou_name=ou_name, functional_currency=currency)
    db.add(ou)
    db.flush()

    log_activity(
        db, user, action="bank_account.ou_created", entity_type="OrganizationUnit",
        entity_id=ou.ou_number, ip_address=_client_ip(request),
        metadata={
            "ou_number": ou.ou_number,
            "ou_name": ou.ou_name,
            "functional_currency": ou.functional_currency,
            "oracle_business_unit_string": f"{ou.ou_name}({ou.ou_number})",
        },
    )
    db.commit()
    db.refresh(ou)
    return _ou_dict(ou)


class UpdateOrganizationUnitRequest(BaseModel):
    ou_name: str
    functional_currency: str


@router.put("/business-units/{ou_number}")
def update_organization_unit(ou_number: str, body: UpdateOrganizationUnitRequest, request: Request,
                              db: Session = Depends(get_db),
                              user: User = Depends(require_permission("ou:manage"))):
    """
    Edit an OrganizationUnit's own name and/or functional currency directly
    -- this is new: previously, once an OU was created (see
    bff/config_builder_routes.py's _get_or_create_organization_unit()), its
    ou_name could be silently overwritten by the NEXT account onboarded
    against it, and functional_currency could never be corrected at all
    short of a manual DB UPDATE. This is that missing correction path,
    exposed as a real, audited, permission-gated action instead.

    Get the exact Business Unit string right here BEFORE relying on it —
    oracle/fusion_client.py's get_ou_display_name() builds Oracle's
    "BusinessUnit" field as EXACTLY f"{ou_name}({ou_number})", e.g.
    "PUNE(111)". Oracle Fusion matches this as an exact string — "Pune(111)"
    (wrong case) or any other variation gets a 404 on every single receipt
    for that OU, not a validation warning. There is no case-insensitive or
    fuzzy fallback anywhere in the posting path.

    Every call here is audited (bank_account.ou_details_changed) with the
    before/after values, since a wrong edit here is exactly as consequential
    as a wrong value at onboarding time.
    """
    ou = db.query(OrganizationUnit).filter(OrganizationUnit.ou_number == ou_number).first()
    if not ou:
        raise AppError(ErrorCode.ORGANIZATION_UNIT_NOT_FOUND, detail=ou_number)

    new_name = (body.ou_name or "").strip()
    new_currency = (body.functional_currency or "").strip().upper()
    if not new_name:
        raise AppError(ErrorCode.BUSINESS_UNIT_REQUIRED, detail="Business Unit name cannot be blank")
    if not new_currency:
        raise AppError(ErrorCode.CONFIG_FIELD_REQUIRED, detail="Functional Currency cannot be blank")

    old_name = ou.ou_name
    old_currency = ou.functional_currency

    ou.ou_name = new_name
    ou.functional_currency = new_currency
    db.flush()

    log_activity(
        db, user, action="bank_account.ou_details_changed", entity_type="OrganizationUnit",
        entity_id=ou.ou_number, ip_address=_client_ip(request),
        metadata={
            "ou_number": ou.ou_number,
            "ou_name": {"from": old_name, "to": new_name},
            "functional_currency": {"from": old_currency, "to": new_currency},
            "effective_from": "next analysis run / next Oracle retry only -- completed runs and "
                               "already-posted receipts are unaffected",
        },
    )
    db.commit()
    db.refresh(ou)
    return _ou_dict(ou)