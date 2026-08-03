-- ============================================================================
-- CashApply — manual schema migration (no Alembic/migration tool in use)
-- ============================================================================
-- Run this ONCE against the shared Postgres database both App1 (backend) and
-- App2 (remittance agent) point at. Safe to re-run: every statement is
-- idempotent (guarded with IF NOT EXISTS / existence checks), so running it
-- twice is a no-op the second time.
--
-- Covers every schema change made to db/models.py in this conversation:
--   1. RowState enum        -> new value 'needs_distribution'
--   2. line_items           -> new columns settlement_type, settlement_provider
--   3. settlement_identifiers -> new table (+ new enum settlementidentifiertype)
--   4. invoice_applications -> new table (duplicate-invoice ledger)
--   5. remittance_extractions -> new column document_type
--      (SHARED table — App2's agent/db/models.py must already match this;
--      see that file's own docstring on why the two must stay identical)
--   6. remittance_invoice_lines -> new column customer_name
--      (SHARED table — same note as #5)
--
-- Usage:
--   psql "$DATABASE_URL" -f apply_schema_changes.sql
-- or, from either app's own environment (same DATABASE_URL for both):
--   psql "postgresql://cashapply:cashapply@localhost:5432/cashapply" -f apply_schema_changes.sql
--
-- IMPORTANT: statements are deliberately run as separate top-level statements,
-- not wrapped in one big BEGIN/COMMIT — Postgres does not allow a newly added
-- enum value (ALTER TYPE ... ADD VALUE) to be used later in the SAME
-- transaction it was added in. Running this file straight through psql (its
-- default autocommit-per-statement behavior) avoids that entirely.
-- ============================================================================


-- ── 1. RowState enum: add 'needs_distribution' ─────────────────────────────
-- Backs LineItem.current_state. Type name is the lowercased Python class
-- name (SQLAlchemy default — no explicit `name=` was given), so it's
-- literally "rowstate" in Postgres.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'rowstate' AND e.enumlabel = 'needs_distribution'
    ) THEN
        ALTER TYPE rowstate ADD VALUE 'needs_distribution';
    END IF;
END
$$;


-- ── 2. line_items: settlement_type / settlement_provider ───────────────────
-- Set by rule_engine/evaluator.py's R16/R17/R18 (credit card / cheque /
-- third-party provider identity) — see LineItem's own column comments.
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS settlement_type     VARCHAR;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS settlement_provider VARCHAR;


-- ── 3. settlement_identifiers (new table) ──────────────────────────────────
-- Configured on the Accounts & OU's page; read live by
-- bank_statement/settlement_identifier.py at classification time.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'settlementidentifiertype') THEN
        CREATE TYPE settlementidentifiertype AS ENUM (
            'third_party_provider', 'card_narrative', 'cheque_narrative'
        );
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS settlement_identifiers (
    id               SERIAL PRIMARY KEY,
    identifier_type  settlementidentifiertype NOT NULL,
    pattern          VARCHAR,
    provider_name    VARCHAR,
    sub_customers    JSON,
    active           BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMP,
    created_by       VARCHAR,
    updated_at       TIMESTAMP,
    updated_by       VARCHAR
);

CREATE INDEX IF NOT EXISTS ix_settlement_identifiers_identifier_type
    ON settlement_identifiers (identifier_type);
CREATE INDEX IF NOT EXISTS ix_settlement_identifiers_active
    ON settlement_identifiers (active);


-- ── 4. invoice_applications (new table) ────────────────────────────────────
-- The real duplicate-invoice-mapping ledger — see rule_engine/
-- invoice_ledger.py's module docstring for what this replaces (a permanent
-- always-False stub). line_item_id references line_items.id.

CREATE TABLE IF NOT EXISTS invoice_applications (
    id               BIGSERIAL PRIMARY KEY,
    line_item_id     BIGINT NOT NULL REFERENCES line_items (id),
    invoice_number   VARCHAR NOT NULL,
    ou_number        VARCHAR,
    customer_name    VARCHAR,
    applied_amount   NUMERIC(18, 2) NOT NULL,
    invoice_currency VARCHAR(10),
    status           VARCHAR NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMP,
    updated_at       TIMESTAMP,
    CONSTRAINT uq_invoice_application_line_invoice UNIQUE (line_item_id, invoice_number)
);

CREATE INDEX IF NOT EXISTS ix_invoice_applications_line_item_id
    ON invoice_applications (line_item_id);
CREATE INDEX IF NOT EXISTS ix_invoice_applications_invoice_number
    ON invoice_applications (invoice_number);
CREATE INDEX IF NOT EXISTS ix_invoice_applications_ou_number
    ON invoice_applications (ou_number);


-- ── 5. remittance_extractions: document_type (SHARED table) ───────────────
-- App1 (backend) and App2 (remittance agent) both point at this same
-- physical table — agent/db/models.py's RemittanceExtraction MUST already
-- carry the identical column, or the two codebases drift apart. This
-- migration only touches the database; the Python model files were already
-- updated in both repos in this conversation.
ALTER TABLE remittance_extractions
    ADD COLUMN IF NOT EXISTS document_type VARCHAR(30) DEFAULT 'customer_remittance';


-- ── 6. remittance_invoice_lines: customer_name (SHARED table) ─────────────
-- Same shared-table note as #5. Only populated when the parent
-- extraction's document_type is 'card_breakdown' or 'cheque_scan'.
ALTER TABLE remittance_invoice_lines
    ADD COLUMN IF NOT EXISTS customer_name VARCHAR;


-- ============================================================================
-- Done. Sanity-check queries (optional, safe to run):
--
--   SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
--     WHERE t.typname = 'rowstate';
--   \d line_items
--   \d settlement_identifiers
--   \d invoice_applications
--   \d remittance_extractions
--   \d remittance_invoice_lines
-- ============================================================================