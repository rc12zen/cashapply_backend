"""
app.gl_rates
=============
File-based ingestion of Oracle GL Daily Rates — there is no live Oracle GL
REST API in this environment. Finance drops a GL Daily Rates extract file
into a watched folder (see watcher.py); it gets parsed (see parser.py) and
UPSERTED into the gl_daily_rates table (db/models.py's GlDailyRate).

Deliberately modeled after app.aging (same watch-folder pattern), with one
key difference: aging stays in-memory only (aging_store), whereas GL rates
are persisted to the DB — rate history needs to accumulate across files
(a new day's rates each morning), not be replaced wholesale like the aging
report is.

rule_engine/fx_service.py is the only consumer — it queries this table
instead of calling Oracle's REST API.
"""