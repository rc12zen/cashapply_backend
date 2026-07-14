-- migration_add_manually_mapped.sql
-- ====================================
-- This project uses SQLAlchemy's Base.metadata.create_all() (see
-- db/session.py's init_db()) rather than a versioned migration tool
-- (no Alembic in this repo). create_all() only creates MISSING TABLES —
-- it will NOT add new columns to a table that already exists. So the
-- three new columns added to LineItem (manually_mapped,
-- manually_mapped_at, manually_mapped_by) need to be added by hand to
-- any database that already has a line_items table.
--
-- Run this once, against your existing database, after deploying the
-- updated models.py/manual_mapping.py/row_detail.py:
--
--   psql -U <user> -d <dbname> -f migration_add_manually_mapped.sql
--
-- Safe to re-run — IF NOT EXISTS guards make it idempotent.

ALTER TABLE line_items
  ADD COLUMN IF NOT EXISTS manually_mapped    BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS manually_mapped_at TIMESTAMP NULL,
  ADD COLUMN IF NOT EXISTS manually_mapped_by VARCHAR NULL;

-- Backfill: rows that were manually mapped in the past (before this
-- column existed) have no way to be identified retroactively — there's
-- no reliable signal on LineItem itself distinguishing "matched via
-- manual mapping" from "matched automatically" prior to this change.
-- The only historical trace is RowStatusHistory.trigger = 'manual_mapping'
-- (an audit log entry, not a flag on the row). If you want best-effort
-- backfill for rows still sitting in a non-terminal state, uncomment
-- and run this — it will NOT correctly backfill rows that have since
-- moved to processed/rejected/post_failed and been re-approved/retried
-- since, since RowStatusHistory only records the state AT THE TIME of
-- that historical manual-mapping event, not necessarily the row's
-- current matched_invoices.
--
-- UPDATE line_items li
-- SET manually_mapped = TRUE
-- FROM row_status_history h
-- WHERE h.line_item_id = li.id
--   AND h.trigger = 'manual_mapping'
--   AND li.manually_mapped IS NOT TRUE;