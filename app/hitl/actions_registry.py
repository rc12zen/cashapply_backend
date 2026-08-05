"""
app.hitl.actions_registry
============================
Resolves "which actions can be taken on THIS row, by THIS user, right
now" from the seed-defined ActionDefinition table (db/models.py) instead
of each caller (frontend JSX, individual route handlers) deciding
independently. Single source of truth for row-level action availability —
add a new action by adding a row via scripts/seed_actions.py, not by
touching frontend conditionals.

Usage (see bff/row_detail.py):
    from ..hitl.actions_registry import get_available_actions

    actions = get_available_actions(db, line_item, user_permission_codes)
    # -> [{"code": "approve", "label": "Approve & Post", "icon": "check-circle",
    #      "confirm_required": False, "is_danger": False}, ...]

Only actions BOTH valid for the row's current state AND permitted for the
user are returned — same "state AND permission must both allow it" rule
the app already used per-action (hitl_routes.py's require_permission +
hitl/service.py's category gate), just computed in one place now.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import ActionDefinition, LineItem
from ..auth.permissions import permission_set_has


def _cond_not_rejected(r: LineItem) -> bool:
    return r.hitl_status != "rejected"


def _cond_reference_status_failed(r: LineItem) -> bool:
    return r.reference_status == "failed"


def _cond_not_already_mapped_terminal(r: LineItem) -> bool:
    # Manual mapping only makes sense for a row not already sitting in a
    # terminal, already-posted state (category gate below already excludes
    # ready_for_oracle/processed; this just guards the edge case of a
    # rejected-then-reopened row, kept as its own named condition so it's
    # visible/adjustable independently of the category list).
    return r.current_state != "processed"


def _cond_receipt_eligibility_undecided(r: LineItem) -> bool:
    # Mark Eligible / Discard — only offered once, on a genuinely
    # undecided Unidentified row. See hitl/service.py's
    # _guard_unidentified_undecided (same check, enforced server-side
    # again at the actual endpoint — this is just what makes the button
    # appear/disappear on the frontend).
    return r.receipt_eligibility is None


def _cond_gl_rate_editable(r: LineItem) -> bool:
    # Edit GL Rate — two eligible situations, both handled by
    # hitl/service.py's edit_gl_rate():
    #   Case A: receipt already exists, not yet invoice-mapped (the
    #           original case — PATCH the existing receipt).
    #   Case B: receipt creation itself FAILED (no standard_receipt_id
    #           yet) — a real gap the original version of this condition
    #           missed entirely, since it required standard_receipt_id to
    #           be set. The wrong rate can very plausibly be WHY creation
    #           failed, so there needs to be a way to fix it and retry
    #           BEFORE any receipt exists, not only after.
    if not r.is_cross_ledger:
        return False
    if r.standard_receipt_id:
        return r.reference_status != "success"
    return r.oracle_post_status == "failed"


def _cond_settlement_override_eligible(r: LineItem) -> bool:
    # Settlement Override ("treat as customer payment") — only offered
    # once, on a Needs Distribution row not already overridden. See
    # hitl/service.py's override_settlement_as_customer_payment().
    return r.settlement_type is not None and r.settlement_override_at is None


# Fixed, known set of extra eligibility checks an ActionDefinition row can
# reference by name (condition_key). Deliberately not free-form/evaluated
# code — see db/models.py's ActionDefinition docstring for why.
CONDITION_CHECKS = {
    "not_rejected": _cond_not_rejected,
    "reference_status_failed": _cond_reference_status_failed,
    "not_processed": _cond_not_already_mapped_terminal,
    "receipt_eligibility_undecided": _cond_receipt_eligibility_undecided,
    "gl_rate_editable": _cond_gl_rate_editable,
    "settlement_override_eligible": _cond_settlement_override_eligible,
}


def _category_matches(action: ActionDefinition, category: str) -> bool:
    if not action.applicable_categories:
        return True  # no restriction -> valid for any category
    return category in action.applicable_categories


def get_available_actions(
    db: Session,
    line_item: LineItem,
    user_permission_codes: set[str],
) -> list[dict]:
    """Returns the actions this user can take on this row RIGHT NOW --
    already filtered by both row state and permission, ready to render
    directly (see components/row-detail/ActionBar.tsx on the frontend)."""
    from ..bff.metrics import _category_for_row, GROUP_DISTRIBUTED  # local import: avoids a
    # circular import at module load time (bff.metrics imports from this
    # package's siblings elsewhere in the app).

    category = _category_for_row(line_item)

    # A "distributed" parent has no row-level actions of its own anymore --
    # every Approve & Post / Reject / Edit GL Rate happens per-entry inside
    # DistributedSummaryCard (see hitl/distribution_actions.py), not at the
    # header. Short-circuiting here (rather than relying on every
    # ActionDefinition row's applicable_categories being configured
    # correctly) guarantees the header never shows a stale row-level
    # action for this category, regardless of DB seed data.
    if category == GROUP_DISTRIBUTED:
        return []

    defs = (
        db.query(ActionDefinition)
        .filter(ActionDefinition.is_active.is_(True))
        .order_by(ActionDefinition.sort_order)
        .all()
    )

    available = []
    for action in defs:
        if not permission_set_has(user_permission_codes, action.permission_code):
            continue
        if not _category_matches(action, category):
            continue
        if action.condition_key:
            check = CONDITION_CHECKS.get(action.condition_key)
            if check is not None and not check(line_item):
                continue
        available.append({
            "code": action.code,
            "label": action.label,
            "icon": action.icon,
            "confirm_required": bool(action.confirm_required),
            "is_danger": bool(action.is_danger),
        })
    return available