-- ============================================================================
-- CashApply — migration, round 4: AI-reuse cache redesign (fingerprint→customer)
-- ============================================================================
-- Run this AFTER apply_schema_changes_round3.sql. If round3 was never
-- applied yet, this one alone is sufficient — it's written to work either
-- way (IF NOT EXISTS / IF EXISTS guards throughout).
--
-- Usage:
--   psql "$DATABASE_URL" -f apply_schema_changes_round4.sql
--
-- Covers the design correction to extraction/pattern_cache.py: this is no
-- longer a cross-customer template learner (positional regex with named
-- groups) — it's a simpler per-customer fingerprint cache. See
-- db/models.py's AiExtractionPattern docstring for the full design.
-- ============================================================================

-- Table may not exist yet if round3 was skipped — create it fresh with the
-- CURRENT (simplified) shape in that case.
CREATE TABLE IF NOT EXISTS ai_extraction_patterns (
    id                    BIGSERIAL PRIMARY KEY,
    template_fingerprint  VARCHAR NOT NULL UNIQUE,
    customer_name         VARCHAR,
    confidence_score      DOUBLE PRECISION,
    regex_pattern         TEXT,
    sample_narrative      TEXT,
    hit_count             INTEGER NOT NULL DEFAULT 1,
    applied_count         INTEGER NOT NULL DEFAULT 0,
    active                BOOLEAN NOT NULL DEFAULT FALSE,
    last_used_at          TIMESTAMP,
    created_at            TIMESTAMP,
    deactivated_at        TIMESTAMP,
    deactivated_by        VARCHAR
);

-- If the table already existed from round3 (positional-regex design),
-- bring it up to the new shape:
ALTER TABLE ai_extraction_patterns ADD COLUMN IF NOT EXISTS customer_name    VARCHAR;
ALTER TABLE ai_extraction_patterns ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION;
-- regex_pattern was NOT NULL under the old design -- the new design never
-- populates it, so it must be relaxed to nullable or every new insert fails.
ALTER TABLE ai_extraction_patterns ALTER COLUMN regex_pattern DROP NOT NULL;

CREATE INDEX IF NOT EXISTS ix_ai_extraction_patterns_fingerprint
    ON ai_extraction_patterns (template_fingerprint);
CREATE INDEX IF NOT EXISTS ix_ai_extraction_patterns_active
    ON ai_extraction_patterns (active);

-- ============================================================================
-- Sanity check (optional):
--   \d ai_extraction_patterns
--   SELECT id, template_fingerprint, customer_name, active, applied_count
--     FROM ai_extraction_patterns ORDER BY applied_count DESC LIMIT 20;
-- ============================================================================