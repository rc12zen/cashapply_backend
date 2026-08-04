"""
app.bff.settlement_identifier_routes
=======================================
/api/bank-accounts/settlement-identifiers/* — manage the three lists that
drive row identity for credit card / cheque / third-party provider
payments (see bank_statement/settlement_identifier.py, which is the only
reader of this table at classification time).

Lives under the same /api/bank-accounts prefix and the same `ou:manage`
write-permission tier as the rest of the Accounts & OU's page (see
bank_accounts_routes.py's module docstring for why that permission, not
config:author, gates writes here) — view is open to anyone with run:view,
same as every other reference table on that page.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.error_codes import ErrorCode
from ..db.models import SettlementIdentifier, SettlementIdentifierType, User
from ..deps import get_db
from ..auth import require_permission
from ..audit.service import log_activity

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _dict(row: SettlementIdentifier) -> dict:
    return {
        "id":              row.id,
        "identifier_type": row.identifier_type.value,
        "pattern":         row.pattern,
        "provider_name":   row.provider_name,
        "sub_customers":   row.sub_customers or [],
        "active":          row.active,
        "created_at":      row.created_at.isoformat() + "Z" if row.created_at else None,
        "created_by":      row.created_by,
        "updated_at":      row.updated_at.isoformat() + "Z" if row.updated_at else None,
        "updated_by":      row.updated_by,
    }


@router.get("/settlement-identifiers")
def list_settlement_identifiers(db: Session = Depends(get_db),
                                 user: User = Depends(require_permission("run:view"))):
    """Grouped the same way the Accounts & OU's page renders them: one list
    per identifier type, so the UI doesn't need to filter client-side."""
    rows = db.query(SettlementIdentifier).order_by(SettlementIdentifier.id.asc()).all()
    grouped: dict[str, list[dict]] = {t.value: [] for t in SettlementIdentifierType}
    for r in rows:
        grouped[r.identifier_type.value].append(_dict(r))
    return {"identifiers": grouped}


class CreateNarrativeIdentifierRequest(BaseModel):
    identifier_type: str = Field(..., description="'card_narrative' | 'cheque_narrative'")
    pattern: str = Field(..., min_length=1, description="Substring, or /regex/ wrapped in slashes")


class CreateProviderIdentifierRequest(BaseModel):
    provider_name: str = Field(..., min_length=1, description="e.g. 'Accurant'")
    sub_customers: list[str] = Field(default_factory=list, description="e.g. ['SITA', 'Kig', 'Lament']")


@router.post("/settlement-identifiers/narrative")
def create_narrative_identifier(body: CreateNarrativeIdentifierRequest, request: Request,
                                 db: Session = Depends(get_db),
                                 user: User = Depends(require_permission("ou:manage"))):
    """Adds one card_narrative / cheque_narrative fingerprint — e.g. the
    PRD's '526221017886' reference or 'Cash Letter Pre-Encoded Dep CR'."""
    if body.identifier_type not in (
        SettlementIdentifierType.CARD_NARRATIVE.value,
        SettlementIdentifierType.CHEQUE_NARRATIVE.value,
    ):
        raise AppError(ErrorCode.VALIDATION_FAILED,
                        detail="identifier_type must be 'card_narrative' or 'cheque_narrative'.")

    row = SettlementIdentifier(
        identifier_type=SettlementIdentifierType(body.identifier_type),
        pattern=body.pattern.strip(),
        active=True,
        created_by=user.email,
    )
    db.add(row)
    db.flush()

    log_activity(
        db, user, action="settlement_identifier.create", entity_type="SettlementIdentifier",
        entity_id=row.id, ip_address=_client_ip(request),
        metadata={"identifier_type": body.identifier_type, "pattern": body.pattern},
    )
    db.commit()
    db.refresh(row)
    return {"success": True, "identifier": _dict(row)}


@router.post("/settlement-identifiers/third-party-provider")
def create_provider_identifier(body: CreateProviderIdentifierRequest, request: Request,
                                db: Session = Depends(get_db),
                                user: User = Depends(require_permission("ou:manage"))):
    """Registers a broker/aggregator and the roster of ITS customers whose
    invoices a payment from them may need to be split across — e.g.
    Accurant -> [SITA, Kig, Lament]. This roster is exactly what the Row
    Detail Payment Distribution table lists once a row is tagged for this
    provider."""
    sub_customers = [c.strip() for c in body.sub_customers if c.strip()]
    row = SettlementIdentifier(
        identifier_type=SettlementIdentifierType.THIRD_PARTY_PROVIDER,
        provider_name=body.provider_name.strip(),
        sub_customers=sub_customers,
        active=True,
        created_by=user.email,
    )
    db.add(row)
    db.flush()

    log_activity(
        db, user, action="settlement_identifier.create", entity_type="SettlementIdentifier",
        entity_id=row.id, ip_address=_client_ip(request),
        metadata={"provider_name": body.provider_name, "sub_customers": sub_customers},
    )
    db.commit()
    db.refresh(row)
    return {"success": True, "identifier": _dict(row)}


class UpdateActiveRequest(BaseModel):
    active: bool


@router.put("/settlement-identifiers/{identifier_id}")
def set_settlement_identifier_active(identifier_id: int, body: UpdateActiveRequest, request: Request,
                                      db: Session = Depends(get_db),
                                      user: User = Depends(require_permission("ou:manage"))):
    """Soft toggle only — see delete below for why there's no hard edit."""
    row = db.query(SettlementIdentifier).get(identifier_id)
    if not row:
        raise AppError(ErrorCode.RECORD_NOT_FOUND, detail=f"Settlement identifier #{identifier_id}")

    old_active = row.active
    row.active = body.active
    row.updated_by = user.email
    row.updated_at = dt.datetime.utcnow()
    db.flush()

    log_activity(
        db, user, action="settlement_identifier.toggle", entity_type="SettlementIdentifier",
        entity_id=row.id, ip_address=_client_ip(request),
        metadata={"active": {"from": old_active, "to": body.active}},
    )
    db.commit()
    db.refresh(row)
    return {"success": True, "identifier": _dict(row)}


@router.delete("/settlement-identifiers/{identifier_id}")
def delete_settlement_identifier(identifier_id: int, request: Request,
                                  db: Session = Depends(get_db),
                                  user: User = Depends(require_permission("ou:manage"))):
    """Hard delete rather than an edit endpoint, deliberately: these
    identifiers are read live at classification time on every incoming row
    (see settlement_identifier.py), so mutating one in place would silently
    change what past AND future unprocessed rows mean with no audit trail
    of what the pattern used to be. Retiring one (delete) and adding a new
    one costs one extra click, for a real audit-trail benefit."""
    row = db.query(SettlementIdentifier).get(identifier_id)
    if not row:
        raise AppError(ErrorCode.RECORD_NOT_FOUND, detail=f"Settlement identifier #{identifier_id}")

    label = row.provider_name or row.pattern or f"#{identifier_id}"
    identifier_type = row.identifier_type.value
    db.delete(row)
    db.flush()

    log_activity(
        db, user, action="settlement_identifier.delete", entity_type="SettlementIdentifier",
        entity_id=identifier_id, ip_address=_client_ip(request),
        metadata={"identifier_type": identifier_type, "label": label},
    )
    db.commit()
    return {"success": True}
