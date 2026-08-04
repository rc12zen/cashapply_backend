-- ============================================================================
-- CashApply — migration, round 3: AI-reuse learned-pattern cache
-- ============================================================================
-- Run this AFTER apply_schema_changes.sql and apply_schema_changes_round2.sql.
-- Same rules: idempotent, run plainly through psql.
--
-- Usage:
--   psql "$DATABASE_URL" -f apply_schema_changes_round3.sql
--
-- Covers:
--   1. ai_extraction_patterns (new table) -- see extraction/pattern_cache.py
--      and db/models.py's AiExtractionPattern for the full design.
-- ============================================================================

CREATE TABLE IF NOT EXISTS ai_extraction_patterns (
    id                    BIGSERIAL PRIMARY KEY,
    template_fingerprint  VARCHAR NOT NULL UNIQUE,
    regex_pattern         TEXT NOT NULL,
    sample_narrative      TEXT,
    hit_count             INTEGER NOT NULL DEFAULT 1,
    applied_count         INTEGER NOT NULL DEFAULT 0,
    active                BOOLEAN NOT NULL DEFAULT FALSE,
    last_used_at          TIMESTAMP,
    created_at            TIMESTAMP,
    deactivated_at        TIMESTAMP,
    deactivated_by        VARCHAR
);

CREATE INDEX IF NOT EXISTS ix_ai_extraction_patterns_fingerprint
    ON ai_extraction_patterns (template_fingerprint);
CREATE INDEX IF NOT EXISTS ix_ai_extraction_patterns_active
    ON ai_extraction_patterns (active);

-- ============================================================================
-- Sanity check (optional):
--   \d ai_extraction_patterns
--   SELECT id, template_fingerprint, active, hit_count, applied_count
--     FROM ai_extraction_patterns ORDER BY applied_count DESC LIMIT 20;
-- ============================================================================