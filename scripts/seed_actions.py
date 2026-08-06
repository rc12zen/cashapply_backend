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
        ["ready_for_oracle"], None, True, False, 10,
    ),
    "reject": (
        "Reject", "x-circle", "hitl:reject",
        None, "not_rejected", True, True, 20,
    ),
    "map_invoice": (
        "Map Invoice", "link", "hitl:map",
        ["unidentified", "needs_remittance", "conflict_exception", "post_failed", "rejected"],
        "not_processed", False, False, 30,
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

        print("Action seed complete:")
        for code, (label, *_rest) in ACTIONS.items():
            print(f"  {code:20s} -> {label}")


if __name__ == "__main__":
    seed_actions()