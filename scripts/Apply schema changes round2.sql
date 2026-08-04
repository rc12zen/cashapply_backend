-- ============================================================================
-- CashApply — migration, round 2: receipt eligibility (discard) + GL rate edit
-- ============================================================================
-- Run this AFTER apply_schema_changes.sql (round 1). Same rules apply: safe
-- to re-run, no Alembic/migration tool in use, run plainly through psql
-- (not wrapped in your own BEGIN/COMMIT — see the ALTER TYPE note below).
--
-- Usage:
--   psql "$DATABASE_URL" -f apply_schema_changes_round2.sql
--
-- Covers:
--   1. RowState enum        -> new value 'discarded'
--   2. line_items           -> receipt_eligibility, receipt_eligibility_at,
--                              receipt_eligibility_by
--   3. line_items           -> gl_rate_original, gl_rate_edited_at,
--                              gl_rate_edited_by, gl_rate_edit_reason
--   4. action_definitions   -> three new seed rows (mark-eligible, discard,
--                              edit-gl-rate) so they actually appear in
--                              available_actions — this repo has no
--                              scripts/seed_actions.py, so these are plain
--                              INSERTs here instead.
-- ============================================================================


-- ── 1. RowState enum: add 'discarded' ──────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'rowstate' AND e.enumlabel = 'discarded'
    ) THEN
        ALTER TYPE rowstate ADD VALUE 'discarded';
    END IF;
END
$$;


-- ── 2. line_items: receipt eligibility (unidentified rows) ─────────────────
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS receipt_eligibility     VARCHAR;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS receipt_eligibility_at  TIMESTAMP;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS receipt_eligibility_by  VARCHAR;


-- ── 3. line_items: GL rate manual-edit audit trail ─────────────────────────
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS gl_rate_original     DOUBLE PRECISION;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS gl_rate_edited_at    TIMESTAMP;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS gl_rate_edited_by    VARCHAR;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS gl_rate_edit_reason  TEXT;


-- ── 4. action_definitions: new seed rows ───────────────────────────────────
-- Only inserted if a row with that `code` doesn't already exist — safe to
-- re-run. applicable_categories/condition_key match hitl/actions_registry.py
-- exactly (RULE_ID_TO_GROUP's "unidentified" / CONDITION_CHECKS keys).

INSERT INTO action_definitions
    (code, label, icon, permission_code, applicable_categories, condition_key,
     confirm_required, is_danger, sort_order, is_active)
SELECT 'mark_eligible', 'Mark Eligible for Receipt', 'check-circle', 'hitl:map',
       '["unidentified"]'::json, 'receipt_eligibility_undecided', FALSE, FALSE, 50, TRUE
WHERE NOT EXISTS (SELECT 1 FROM action_definitions WHERE code = 'mark_eligible');

INSERT INTO action_definitions
    (code, label, icon, permission_code, applicable_categories, condition_key,
     confirm_required, is_danger, sort_order, is_active)
SELECT 'discard', 'Discard', 'trash-2', 'hitl:reject',
       '["unidentified"]'::json, 'receipt_eligibility_undecided', TRUE, TRUE, 51, TRUE
WHERE NOT EXISTS (SELECT 1 FROM action_definitions WHERE code = 'discard');

INSERT INTO action_definitions
    (code, label, icon, permission_code, applicable_categories, condition_key,
     confirm_required, is_danger, sort_order, is_active)
SELECT 'edit_gl_rate', 'Edit GL Rate', 'edit-3', 'oracle:post',
       NULL, 'gl_rate_editable', FALSE, FALSE, 60, TRUE
WHERE NOT EXISTS (SELECT 1 FROM action_definitions WHERE code = 'edit_gl_rate');


-- ============================================================================
-- Sanity checks (optional):
--   SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
--     WHERE t.typname = 'rowstate';
--   \d line_items
--   SELECT code, label, permission_code, applicable_categories, condition_key
--     FROM action_definitions WHERE code IN ('mark_eligible','discard','edit_gl_rate');
-- ============================================================================