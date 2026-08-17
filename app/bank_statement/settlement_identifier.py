"""
app.bank_statement.settlement_identifier
===========================================
Classifies a bank statement row against the three configured identifier
lists (Accounts & OU's page -> Settlement Identifiers):

  - third_party_provider  : payer name matches a registered broker
                             (e.g. "Accurant" -> SITA, Kig, Lament)
  - card_narrative         : narration matches a registered credit-card
                             settlement fingerprint (PRD: ...526221017886)
  - cheque_narrative       : narration matches a registered cheque-deposit
                             fingerprint (PRD: "Cash Letter Pre-Encoded Dep CR")

This module is deliberately narrow: it only ANSWERS "what is this row" --
it does not split the amount, does not look up invoices, and does not touch
Oracle. That's the next step (the "Split & Map" flow discussed separately).
Called once per row, early, from rule_engine/orchestrator.py's
_build_rule_input() -- BEFORE the R0..R15 rule table runs, since a
settlement-identity match must short-circuit everything else (see
evaluator.py's R16/R17/R18, which sit above R0).

Matching rules
--------------
- provider_name / pattern comparisons are case-insensitive.
- `pattern` supports a plain substring OR a regex: if the stored string is
  wrapped in a leading/trailing "/" (e.g. "/526221017886$/"), it's compiled
  as a regex; otherwise it's a plain "is this substring present" check --
  matching how the two PRDs describe these identifiers (a fixed reference
  string / a fixed narration phrase), so most rows never need a regex at all.
- First match wins, in this priority order: third_party_provider,
  card_narrative, cheque_narrative. In practice these shouldn't collide
  (a broker payment doesn't also carry a card-settlement narration), but the
  order is fixed so behavior is deterministic if a config is ever ambiguous.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from ..common.errors import AppError
from ..common.regex_safety import safe_search
from ..db.models import SettlementIdentifier, SettlementIdentifierType


@dataclass
class SettlementMatch:
    settlement_type: str             # "third_party_provider" | "card_narrative" | "cheque_narrative"
    settlement_provider: Optional[str] = None   # provider_name, third-party only
    sub_customers: Optional[list] = None        # roster, third-party only
    matched_identifier_id: Optional[int] = None


def _text_matches(candidate: str, pattern: str) -> bool:
    if not candidate or not pattern:
        return False
    candidate = candidate.strip().lower()
    pattern = pattern.strip()
    if len(pattern) >= 2 and pattern.startswith("/") and pattern.endswith("/"):
        # A "/…/"-delimited identifier is the one place this table carries a
        # real regex. Validated + length-capped first -- see
        # common/regex_safety.py (ReDoS / CWE-1333). An unsafe or malformed
        # pattern degrades to "no match" here rather than raising, matching
        # this function's existing re.error behaviour: classification is
        # best-effort and must never break an in-flight analysis run.
        try:
            return bool(safe_search(pattern[1:-1], candidate, flags=re.IGNORECASE))
        except AppError:
            return False
    return pattern.lower() in candidate


def load_identifiers(db: Session, active_only: bool = True) -> dict[str, list[SettlementIdentifier]]:
    """Grouped by type -- what the Accounts & OU's page list, and what
    classify_settlement() below iterates over."""
    q = db.query(SettlementIdentifier)
    if active_only:
        q = q.filter(SettlementIdentifier.active.is_(True))
    rows = q.all()
    out: dict[str, list[SettlementIdentifier]] = {
        SettlementIdentifierType.THIRD_PARTY_PROVIDER.value: [],
        SettlementIdentifierType.CARD_NARRATIVE.value:       [],
        SettlementIdentifierType.CHEQUE_NARRATIVE.value:     [],
    }
    for r in rows:
        out[r.identifier_type.value].append(r)
    return out


def classify_settlement(
    db: Session,
    narration: Optional[str],
    payer_name: Optional[str] = None,
) -> Optional[SettlementMatch]:
    """Returns the first matching identity for this row, or None if it's an
    ordinary payment (the overwhelming majority of rows)."""
    if db is None:
        return None

    identifiers = load_identifiers(db)

    if payer_name:
        for row in identifiers[SettlementIdentifierType.THIRD_PARTY_PROVIDER.value]:
            if _text_matches(payer_name, row.provider_name or ""):
                return SettlementMatch(
                    settlement_type=SettlementIdentifierType.THIRD_PARTY_PROVIDER.value,
                    settlement_provider=row.provider_name,
                    sub_customers=row.sub_customers or [],
                    matched_identifier_id=row.id,
                )

    if narration:
        for row in identifiers[SettlementIdentifierType.CARD_NARRATIVE.value]:
            if _text_matches(narration, row.pattern or ""):
                return SettlementMatch(
                    settlement_type=SettlementIdentifierType.CARD_NARRATIVE.value,
                    matched_identifier_id=row.id,
                )
        for row in identifiers[SettlementIdentifierType.CHEQUE_NARRATIVE.value]:
            if _text_matches(narration, row.pattern or ""):
                return SettlementMatch(
                    settlement_type=SettlementIdentifierType.CHEQUE_NARRATIVE.value,
                    matched_identifier_id=row.id,
                )

    return None
