"""
scripts/seed_actions.py
==========================
Seeds the row-level action definitions used by the Row Detail page's
action bar (see hitl/actions_registry.py). Idempotent — safe to re-run.

Run once per environment, alongside seed_rbac.py:

    python -m scripts.seed_actions

PATCH: folded in the 4 rows that previously existed ONLY as raw INSERTs in
Apply_schema_changes_round2.sql (mark_eligible, discard, edit_gl_rate,
settlement_override) — those were data, not schema, and had no Python seed
path at all. Keeping them here means one seed script covers every action
row instead of splitting "most actions" (this file) from "4 actions" (SQL).
"""
from __future__ import annotations

from app.db.models import ActionDefinition
from app.db.session import session_scope

# code -> (label, icon, permission_code, applicable_categories, condition_key,
#          confirm_required, is_danger, sort_order)
ACTIONS: dict[str, tuple] = {
    "approve": (
        "Approve & Post", "check-circle", "oracle:post",
        # short_payment was split out of ready_for_oracle (see bff/metrics.py
        # GROUP_SHORT_PAYMENT) but is the identical Approve -> Oracle POST path,
        # so both categories must offer this action. overpayment (R9e) is the
        # same again — a SPOC-confirmed capped mapping, where each reference is
        # capped at its own invoice's outstanding and the excess stays unapplied
        # on the receipt. Must stay in step with hitl/service.py's approve_row()
        # gate, which admits exactly these three groups.
        ["ready_for_oracle", "short_payment", "overpayment"], None, True, False, 10,
    ),
    # confirm_required is now False: Reject opens RejectRowModal, which collects
    # the REASON and carries its own confirm button. The inline "Sure?" on top of
    # a modal was a double prompt for one decision.
    "reject": (
        "Reject", "x-circle", "hitl:reject",
        None, "not_rejected", False, True, 20,
    ),
    # Reopen (undo a rejection, or un-park an overpayment) — see
    # hitl/actions_registry.py's category gate. Same permission tier as reject.
    #
    # confirm_required is now False for the same reason as reject: this opens
    # ReopenAndReviewModal (hitl/reopen_with_edits.py), where the SPOC edits the
    # customer/invoice mapping, sees the resulting rule and bucket previewed, and
    # confirms there. It is no longer the one-click pure undo that
    # service.py's reopen_row() implements — that endpoint still exists and still
    # works, it just no longer has a caller in the UI.
    #
    # applicable_categories deliberately UNCHANGED. It is already correct for
    # both decisions taken: a row rejected from `processed` never reaches
    # category "rejected" at all (reference_status == "success" outranks
    # hitl_status in _category_for_row's precedence), so Reopen is already hidden
    # there; a row rejected from `post_failed` DOES land in "rejected" and keeps
    # its Reopen button, which is wanted — that is where cash is sitting on a
    # created-but-unmapped Oracle receipt, so it must stay recoverable.
    "reopen": (
        "Reopen", "rotate-ccw", "hitl:reject",
        ["rejected", "overpayment_parked"], None, False, False, 15,
    ),
    # THE single entry point for an overpaid row. Deliberately neutral: it
    # promises a decision, not an outcome. The dialog it opens states the
    # arithmetic once and offers the two real outcomes side by side —
    #   "Apply & Post"     -> routes to the Manual Invoice Mapping card
    #   "Explain & Close"  -> calls POST /api/hitl/park-overpayment/{id}
    # so the SPOC picks a consequence rather than decoding a button name.
    #
    # This replaces the old "Resolve Overpayment" label, which read as though it
    # fixed the problem when it actually meant "post nothing, just record why" —
    # close to the opposite. confirm_required is False because the dialog itself
    # is the confirmation step.
    #
    # Offered on any conflict_exception row; narrowed to R11 by the condition,
    # and re-checked server-side in park_overpayment().
    "handle_overpayment": (
        "Handle Overpayment", "scale", "hitl:map",
        ["conflict_exception"], "is_overpayment", False, False, 25,
    ),
    "map_invoice": (
        "Map Invoice", "link", "hitl:map",
        # NOTE: "rejected" removed — manual mapping refuses when hitl_status is
        # set (see hitl/manual_mapping.py), so a Map-Invoice button on a rejected
        # row always errored. Rejected rows are recovered via "reopen" above,
        # which clears the flag; the row then re-surfaces Map Invoice through
        # its own restored category if it needs mapping. ("post_failed" left as
        # a separate known issue — same guard, out of scope for this change.)
        ["unidentified", "needs_remittance", "conflict_exception", "post_failed"],
        # Was "not_processed". Broadened to also hide this button on an OPEN
        # OVERPAYMENT (R11): those rows get one entry point instead, "Handle
        # Overpayment", whose dialog routes here when the SPOC chooses to apply.
        # conflict_exception covers a dozen unrelated rules, so the exclusion has
        # to be a condition rather than a category change.
        "mappable_directly", False, False, 30,
    ),
    "recheck_remittance": (
        "Recheck Remittance", "refresh-cw", "hitl:map",
        ["needs_remittance"], None, False, False, 40,
    ),
    "retry_oracle": (
        "Retry Oracle Post", "rotate-cw", "oracle:post",
        ["post_failed"], "reference_status_failed", True, False, 50,
    ),
    # ── PATCH: previously SQL-only rows (round2.sql), now seeded here ──────
    "settlement_override": (
        "Treat as Customer Payment", "arrow-right-left", "hitl:map",
        ["needs_distribution"], "settlement_override_eligible", True, False, 45,
    ),
    "mark_eligible": (
        "Mark Eligible for Receipt", "check-circle", "hitl:map",
        ["unidentified"], "receipt_eligibility_undecided", False, False, 50,
    ),
    "discard": (
        "Discard", "trash-2", "hitl:reject",
        ["unidentified"], "receipt_eligibility_undecided", True, True, 51,
    ),
    "edit_gl_rate": (
        "Edit GL Rate", "edit-3", "oracle:post",
        None, "gl_rate_editable", False, False, 60,
    ),
}


def seed_actions() -> None:
    with session_scope() as db:
        existing = {a.code: a for a in db.query(ActionDefinition).all()}
        for code, (label, icon, perm, categories, condition, confirm, danger, sort_order) in ACTIONS.items():
            action = existing.get(code)
            if action is None:
                action = ActionDefinition(code=code)
                db.add(action)
            action.label = label
            action.icon = icon
            action.permission_code = perm
            action.applicable_categories = categories
            action.condition_key = condition
            action.confirm_required = confirm
            action.is_danger = danger
            action.sort_order = sort_order
            action.is_active = True

        # Retire codes that are no longer in ACTIONS. Without this, renaming an
        # action left the OLD row active in the DB and the frontend kept
        # rendering both — which is exactly how "Resolve Overpayment" would have
        # survived alongside its replacement, "Handle Overpayment".
        # Deactivated rather than deleted so RowStatusHistory keeps resolving.
        retired = [a for code, a in existing.items() if code not in ACTIONS and a.is_active]
        for a in retired:
            a.is_active = False

        print("Action seed complete:")
        for code, (label, *_rest) in ACTIONS.items():
            print(f"  {code:20s} -> {label}")
        for a in retired:
            print(f"  {a.code:20s} -> RETIRED (is_active=False)")


if __name__ == "__main__":
    seed_actions()