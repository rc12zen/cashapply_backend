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


def _cond_rejectable(r: LineItem) -> bool:
    """
    Reject is offered only while there is still something to reject.

    Not already rejected (the old `not_rejected` rule), AND the row's invoice
    references have NOT posted successfully to Oracle.

    WHY THE SECOND HALF EXISTS -- a real incident, 2026-08-18:
    Reject had no category or state gate of any kind, so the button appeared on
    a `processed` row. Rejecting one did three harmful things and no useful one:

      1. It could not undo the Oracle posting. The references are applied in
         Oracle and this system has no reversal path, so the cash stayed
         applied while our row claimed to be rejected.
      2. reject_row() calls release_applications(), freeing the internal ledger
         claim -- so our ledger advertised an invoice as available that Oracle
         had already settled. Another bank row could then be mapped onto it.
      3. The row became unreachable. `reference_status == "success"` outranks
         `hitl_status == "rejected"` in _category_for_row()'s precedence, so it
         still displayed as processed, never appeared in the Rejected bucket,
         and Reopen (gated to rejected/overpayment_parked) was not offered.
         Zero actions available.

    A genuinely wrong posted receipt needs an Oracle-side reversal or a credit
    memo, neither of which this app performs. Hiding Reject is therefore the
    honest outcome -- see hitl/service.py's reject_row(), which enforces the
    same rule server-side rather than trusting this to be a UI-only gate.
    """
    if r.hitl_status == "rejected":
        return False
    return r.reference_status != "success"


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


def _cond_receipt_editable(r: LineItem) -> bool:
    # Edit Receipt (account number / OU / receipt method / rate / dates) —
    # hitl/service.py's edit_receipt_fields(). Same two eligible
    # situations as GL rate above, but WITHOUT requiring is_cross_ledger:
    # account number, OU, receipt method, and dates are correctable
    # regardless of currency — only the GL conversion rate field inside
    # that unified edit is itself gated to cross-ledger rows (checked
    # inside edit_receipt_fields(), not here), since a same-ledger row
    # genuinely has no rate to edit.
    if r.standard_receipt_id:
        return r.reference_status != "success"
    return r.oracle_post_status == "failed"


def _cond_mappable_directly(r: LineItem) -> bool:
    # Map Invoice is offered on its own for every category EXCEPT an open
    # overpayment. An overpaid row has a single entry point — "Handle
    # Overpayment" — because the two things a SPOC can do to it (apply what is
    # owed, or post nothing and record why) are outcomes of one decision, and
    # showing them as two sibling buttons made the SPOC compare two labels and
    # guess which one moved money. That dialog routes to this same mapping card
    # when they choose to apply.
    #
    # Keeps the old not_processed check: mapping never makes sense on a row that
    # already posted (guards the rejected-then-reopened edge case, since the
    # category gate alone does not cover it).
    if r.rule_id == "R11":
        return False
    return r.current_state != "processed"


def _cond_is_overpayment(r: LineItem) -> bool:
    # Handle Overpayment — the action is seeded against conflict_exception,
    # which covers a dozen unrelated rules, so this narrows it to the one that
    # actually means "more money arrived than was owed". hitl/overpayment.py's
    # park_overpayment() re-checks the same thing server-side; this is only
    # what makes the button appear.
    return r.rule_id == "R11"


def _cond_settlement_override_eligible(r: LineItem) -> bool:
    # Settlement Override ("treat as customer payment") — only offered
    # once, on a Needs Distribution row not already overridden. See
    # hitl/service.py's override_settlement_as_customer_payment().
    return r.settlement_type is not None and r.settlement_override_at is None


def _cond_discardable(r: LineItem) -> bool:
    # Discard — two independent eligible situations, both handled by
    # hitl/service.py's discard_row() (which loosens its own category gate
    # to match — see that function's docstring):
    #   Case A (original): a genuinely undecided Unidentified row — same
    #           check as receipt_eligibility_undecided above.
    #   Case B (NEW): a row whose Oracle receipt was just explicitly
    #           Deleted (see hitl/service.py's delete_receipt()) and the
    #           SPOC is choosing "Discard" over "Create New Receipt" in
    #           the post-delete follow-up. receipt_deleted_at is set and
    #           there's no live receipt (delete_receipt() always clears
    #           standard_receipt_id on success, so this can't be
    #           accidentally true for a row that has since had a NEW
    #           receipt created).
    if r.receipt_eligibility is None and not r.standard_receipt_id and r.receipt_deleted_at is None:
        # Mirrors the original unidentified-only case exactly (see
        # receipt_eligibility_undecided) — kept explicit rather than just
        # falling through, since receipt_eligibility is only ever set on
        # unidentified rows in the first place.
        return True
    return bool(r.receipt_deleted_at) and not r.standard_receipt_id


def _cond_has_reversible_invoice(r: LineItem) -> bool:
    # Reverse (per-invoice SOAP unapply) — gates whether the row has ANY
    # invoice worth offering a reversal control for at all. The frontend
    # renders one Unapply control PER applied invoice (see
    # components/row-detail's invoice breakdown), not a single row-level
    # button — this just decides whether that section of the UI has
    # anything to show. Mirrors hitl/service.py's
    # reverse_receipt_invoice() eligibility gate: something must actually
    # be applied (reference_status == "success") for an unapply call to
    # mean anything to Oracle.
    return bool(r.matched_invoices) and r.reference_status == "success"


def _cond_receipt_deletable(r: LineItem) -> bool:
    # Delete Receipt — same predicate as receipt_editable (Edit Receipt is
    # already state-based/category-unrestricted and stays correct for the
    # new receipt_reversed bucket with zero changes; Delete needs to match
    # it exactly): a receipt exists and has no active application. Kept as
    # its own named function (not a reuse of _cond_receipt_editable)
    # per this file's convention of one condition per business rule, even
    # when two happen to coincide today (see gl_rate_editable vs
    # receipt_editable for precedent).
    if r.standard_receipt_id:
        return r.reference_status != "success"
    return False


# Fixed, known set of extra eligibility checks an ActionDefinition row can
# reference by name (condition_key). Deliberately not free-form/evaluated
# code — see db/models.py's ActionDefinition docstring for why.
CONDITION_CHECKS = {
    "not_rejected": _cond_not_rejected,
    # Reject's actual gate. `not_rejected` is kept registered because older
    # ActionDefinition rows in a database that has not been re-seeded still
    # reference it by name -- removing the key would make Reject vanish
    # entirely on those until seed_actions.py runs.
    "rejectable": _cond_rejectable,
    "reference_status_failed": _cond_reference_status_failed,
    "not_processed": _cond_not_already_mapped_terminal,
    "receipt_eligibility_undecided": _cond_receipt_eligibility_undecided,
    # Kept registered for the same reason "not_rejected" is above: an
    # un-reseeded DB may still have an "edit_gl_rate" ActionDefinition row
    # referencing this key by name. The seeded set now uses "edit_receipt"
    # / "receipt_editable" instead — see scripts/seed_actions.py.
    "gl_rate_editable": _cond_gl_rate_editable,
    "receipt_editable": _cond_receipt_editable,
    "settlement_override_eligible": _cond_settlement_override_eligible,
    "is_overpayment": _cond_is_overpayment,
    "mappable_directly": _cond_mappable_directly,
    "discardable": _cond_discardable,
    "has_reversible_invoice": _cond_has_reversible_invoice,
    "receipt_deletable": _cond_receipt_deletable,
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