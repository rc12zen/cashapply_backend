# CashApply Backend — Setup & Testing Guide

This adds Azure Entra ID auth, RBAC, duplicate detection, activity logging,
and a Postgres-backed background task queue (procrastinate — no Redis, no
Celery) on top of the existing pipeline. Full rationale in
`cashapply-platform-hardening-design.md`.

Everything below has been smoke-tested at the code level (import graph,
SQLAlchemy model creation, RBAC permission logic, hash normalization,
advisory-lock key, dev-bypass safety guard). You'll need a real Postgres to
run the app itself — that part hasn't been run end-to-end in this sandbox
(no Postgres package mirror available here), so budget time for the usual
first-run debugging.

## 1. Prerequisites

- Python 3.11+
- PostgreSQL 14+ (local install or Docker)

## 2. Get a Postgres running

```bash
docker run -d --name cashapply-pg \
  -e POSTGRES_USER=cashapply -e POSTGRES_PASSWORD=cashapply -e POSTGRES_DB=cashapply \
  -p 5432:5432 postgres:16
```

## 3. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Configure environment

```bash
cp .env.example .env
```

For local testing you can leave `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` blank —
you'll use the dev SSO bypass instead (see §6). `DEV_SSO_BYPASS_EMAILS` in
`.env.example` already includes `admin@example.com` and `analyst@example.com`.

If you want real Oracle/Anthropic calls to work, fill in
`ORACLE_BASIC_USERNAME` / `ORACLE_BASIC_PASSWORD` / `ANTHROPIC_API_KEY` —
these no longer have hardcoded defaults (see design doc §10 — a previous
revision shipped real credentials as code defaults; removed).

## 5. Initialize the database + seed RBAC

```bash
# Creates all tables (existing + new auth/RBAC/dedup/audit tables)
python3 -c "from app.db.session import init_db; init_db()"

# Seeds the 5 roles + permission set, and creates two dev-bypass users
python -m scripts.seed_rbac --dev-user admin@example.com --dev-role Administrator
python -m scripts.seed_rbac --dev-user analyst@example.com --dev-role Analyst
```

Re-running `seed_rbac` is safe — every insert is get-or-create.

## 6. Register procrastinate's schema

Procrastinate needs its own tables/functions in the same database (job
queue, not your business data):

```bash
procrastinate --app=app.tasks.app.procrastinate_app schema --apply
```

## 7. Run the app + worker (two terminals)

```bash
# Terminal 1 — API
uvicorn app.main:app --reload --port 8000

# Terminal 2 — background worker (this is the "one new process" —
# no new infra, it just polls the same Postgres via SELECT ... FOR UPDATE SKIP LOCKED)
python -m app.tasks.worker
```

Visit `http://localhost:8000/docs` — you should see ~60 routes including the
new `/api/auth/*`, `/api/admin/*`, `/api/activity-log`, and
`/api/run/files/{id}/ingest-status`.

## 8. Testing auth (dev bypass — no real Azure needed)

Every protected route reads the `X-Dev-User` header when `ENVIRONMENT=local`:

```bash
curl -s http://localhost:8000/api/auth/me -H "X-Dev-User: admin@example.com" | python3 -m json.tool
```

Expect `role: "Administrator"`, `permissions: ["*"]`.

```bash
curl -s http://localhost:8000/api/auth/me -H "X-Dev-User: analyst@example.com" | python3 -m json.tool
```

Expect `role: "Analyst"`, a scoped permission list (no `oracle:post`).

Try a route with no header at all — should 401:
```bash
curl -i http://localhost:8000/api/run/files
```

Try a permission you don't have (Analyst calling an Oracle-Operator-only route):
```bash
curl -i -X POST http://localhost:8000/api/hitl/approve/1 \
  -H "X-Dev-User: analyst@example.com" -H "Content-Type: application/json" -d '{}'
```
Expect `403 Missing permission: oracle:post`.

## 9. Testing duplicate detection

```bash
curl -s -X POST http://localhost:8000/api/run/upload \
  -H "X-Dev-User: analyst@example.com" \
  -F "file=@/path/to/some_statement.xlsx"
```

Upload the exact same file again — expect `"duplicate": true` with
`uploaded_by`, `uploaded_at`, and a `history_link`, and a `statement.upload_rejected_duplicate`
row in `activity_logs`.

Check ingestion status (row-level dedup) — replace `1` with the returned `source_file_id`:
```bash
curl -s http://localhost:8000/api/run/files/1/ingest-status -H "X-Dev-User: analyst@example.com"
```
Poll until `ingest_status` flips from `"processing"` to `"ready"`, then check
`new_row_count` / `duplicate_row_count`. Upload a second statement with
overlapping rows and confirm `duplicate_row_count > 0` on the second one.

## 10. Testing the concurrent-run guard

Fire two `/api/run/start` calls back to back with the same `selected_files`
(e.g. two terminal tabs, or `xargs -P2`). One should succeed; the second
should either get the `409 "A run is already in progress"` fast-fail, or —
if both slipped past that check at nearly the same instant — the losing
run's `AnalysisRun.error_message` should read *"Another analysis run is
already processing these files."* rather than both runs double-processing
the same rows.

## 11. Testing RBAC admin routes

```bash
curl -s http://localhost:8000/api/admin/users -H "X-Dev-User: admin@example.com" | python3 -m json.tool

curl -s -X PUT http://localhost:8000/api/admin/users/2/role \
  -H "X-Dev-User: admin@example.com" -H "Content-Type: application/json" \
  -d '{"role_name": "Oracle Operator"}'
```

Then check `/api/activity-log?action=user.role_changed` — the entry's
`metadata` should show both `from_role` and `to_role`.

## 12. What's unchanged / still works as before

- All original `/api/config/*`, `/api/results/*`, `/api/executive-summary/*`
  routes are untouched.
- `rule_engine`, `oracle`, `extraction`, `aging` packages are untouched
  except for the two additive orchestrator changes described in the design
  doc §0 (row sourcing) and §4 (advisory lock wrapper).
- Any `SourceFile` uploaded through the OLD code path (or already in your
  DB before this change) has `ingest_status = NULL` — `_run_analysis` falls
  back to the original direct-file-parse behavior for those, so nothing
  existing breaks. Only new uploads through `/api/run/upload` get the new
  hash-deduped path.

## 13. Known gaps / next steps (being upfront about what this delivery does NOT include)

- **No Alembic migrations.** `init_db()` still uses `Base.metadata.create_all()`
  (matches the existing PoC pattern) — fine for a fresh DB, but if you're
  applying this to a database that already has data, review the new nullable
  columns/tables manually or set up Alembic before running against prod data.
- **`activity_logs` partitioning** (design doc §3) isn't applied — it's a
  single table via `create_all()`. Apply the monthly range-partitioning DDL
  from the design doc via a real migration before relying on this at high
  volume.
- **Frontend (`ss2/`) is unchanged.** It doesn't yet send `X-Dev-User` /
  `Authorization` headers, doesn't poll `ingest-status`, and doesn't show the
  duplicate-file banner or `version_conflict` handling. Say the word and
  I'll wire `lib/api.ts` + MSAL next.
- **No automated test suite** beyond the smoke tests described above. Given
  the scope, I'd recommend at minimum a pytest suite around
  `ingestion/row_hash.py`, `auth/bypass.py`'s environment guard, and
  `rule_engine/orchestrator.py`'s advisory-lock key — happy to write these
  next if useful.
