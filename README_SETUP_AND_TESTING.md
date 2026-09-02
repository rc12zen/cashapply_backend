# CashApply Backend — Setup & Testing Guide

FastAPI backend for CashApply: bank statement ingestion, invoice matching,
Oracle Fusion receipt posting, RBAC/auth, and a Postgres-backed background
task queue (procrastinate — no Redis, no Celery). Full architecture
rationale in `cashapply-platform-hardening-design.md`; RBAC role/permission
detail in `RBAC_AND_LOGGING.md`; API payload encryption detail in
`API_PAYLOAD_ENCRYPTION.md`.

Follow this top to bottom on a clean checkout and you'll have a working
local instance you can log into and upload a statement against.

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Python | **3.14** | `python --version`. 3.11+ should work, but 3.14 is what this checkout's `venv` is built against — if something version-specific breaks, try 3.14 first. |
| PostgreSQL | **16** | 14+ required (the design assumes `SELECT ... FOR UPDATE SKIP LOCKED` and advisory locks, both available since well before 14). |
| Docker | any recent | Only needed if you want Postgres via Docker instead of a native install (§2, option A). |
| git | any | |

You do **not** need Redis, RabbitMQ, or any other queue broker — the task
queue lives inside Postgres.

## 1. Clone and create a virtual environment

```bash
git clone <repo-url> cashapply_backend
cd cashapply_backend

python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS/Linux
```

## 2. Get a Postgres instance running

Pick **one**.

**Option A — Docker (fastest):**

```bash
docker run -d --name cashapply-pg \
  -e POSTGRES_USER=cashapply -e POSTGRES_PASSWORD=cashapply -e POSTGRES_DB=cashapply \
  -p 5432:5432 postgres:16
```

**Option B — native install:**

1. Install PostgreSQL 16 from https://www.postgresql.org/download/ (or your
   OS package manager — `brew install postgresql@16`, `apt install
   postgresql-16`, etc.).
2. Start the service (installer does this on Windows/Mac; `sudo service
   postgresql start` on Linux).
3. Create the database and role:
   ```bash
   psql -U postgres
   CREATE USER cashapply WITH PASSWORD 'cashapply' CREATEDB;
   CREATE DATABASE cashapply OWNER cashapply;
   \q
   ```

Either way, you should end up able to connect to
`postgresql://cashapply:cashapply@localhost:5432/cashapply`.

## 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

Note: the Postgres driver is **psycopg3** (`psycopg[binary]`), not
`psycopg2` — your `DATABASE_URL` must use the `postgresql+psycopg://`
scheme (§4), not `postgresql+psycopg2://`.

## 4. Configure environment

```bash
cp backend.env.local.example .env
```

Then edit `.env`:

- **`DATABASE_URL`** — point at whatever you created in §2. The example
  file ships `postgresql+psycopg2://cashapply2:cashapply2@localhost:5432/cashapply2`
  — that's the wrong driver scheme for this checkout's dependencies; use:
  ```
  DATABASE_URL=postgresql+psycopg://cashapply:cashapply@localhost:5432/cashapply
  ```
  (adjust user/password/db name to match whatever you actually created).
- **`API_ENCRYPTION_KEY`** — **required**, not present in the example file.
  Encryption of API request/response bodies is on by default in every
  environment (see `API_PAYLOAD_ENCRYPTION.md`); the server refuses to
  start without a valid key. Generate one:
  ```bash
  python -m scripts.gen_api_key
  ```
  Paste the printed `API_ENCRYPTION_KEY=...` line into `.env`, and keep the
  matching `NEXT_PUBLIC_API_ENCRYPTION_KEY=...` line for the frontend setup
  (frontend README §2). If you'd rather skip encryption for local curl/pytest
  work, set `API_ENCRYPTION_ENABLED=false` instead and skip the key.
- **`AZURE_TENANT_ID` / `AZURE_CLIENT_ID`** — leave blank for local dev.
  `APP_ENV=local` enables the `X-Dev-User` bypass (§8) instead, so real
  Azure tokens aren't needed to run and test locally.
- **`ANTHROPIC_API_KEY`** — needed for the AI extraction fallback (Layer
  2B) to do anything. Get one from https://console.anthropic.com/. Without
  it, set `AI_EXTRACTION_ENABLED=false` — analysis still runs (regex-only),
  unresolved rows just land as "unidentified" and the Home page shows "AI
  Extraction Off" instead of failing.
- **`ORACLE_BASIC_USERNAME` / `ORACLE_BASIC_PASSWORD`** — only needed if
  you intend to actually post receipts to Oracle Fusion's test instance.
  Everything else (upload, analyze, HITL mapping) works without these.

Every setting is commented in the example file — read through it once.

**UAT/prod:** use `backend.env.uat.example` as the starting point instead.
The key differences from local: `APP_ENV=uat` (disables the `X-Dev-User`
bypass entirely — real Azure AD tokens required), `AZURE_TENANT_ID` /
`AZURE_CLIENT_ID` are mandatory, `CORS_ALLOWED_ORIGINS` must be the real
frontend origin (not `*`), and `API_ENCRYPTION_KEY` must be a **freshly
generated** key — never reuse the local sample value outside local dev.

## 5. Initialize the database

```bash
python -c "from app.db.session import init_db; init_db()"
```

This runs `Base.metadata.create_all()` — creates every table (statements,
receipts, auth/RBAC, audit log, procrastinate's own tables are separate,
see §6).

## 6. Seed RBAC roles and a first admin user

The 5 fixed roles (Administrator, Analyst, Oracle Operator, Auditor,
Viewer) aren't created through the UI — they're seeded once per
environment. You also need at least one Administrator to log in with,
since the Users tab itself requires an existing Administrator to call it.

```bash
# Seeds the 5 roles, and creates you as a local dev Administrator
python -m scripts.seed_rbac --dev-user you@example.com --dev-role Administrator
```

Or seed 4 ready-made demo users covering every role (handy for exercising
RBAC without onboarding each one by hand):

```bash
python -m scripts.seed_rbac --demo-users
# creates: muni@zensar.com (Administrator), viewer@example.com (Viewer),
#          auditor@example.com (Auditor), multi@example.com (Analyst + Oracle Operator)
```

Re-running `seed_rbac` is safe — every insert is get-or-create.

## 7. Register procrastinate's schema

One-time per database — creates procrastinate's own job-queue tables
(separate from your business tables above), needed before the worker or
any `.defer()` call will work:

```bash
python -m scripts.apply_procrastinate_schema
```

(This is a wrapper around procrastinate's own `schema --apply` CLI — the
CLI itself hits a Windows-specific asyncio bug, so use this script instead
of the raw `procrastinate ... schema --apply` command.)

## 8. Run the app

```bash
uvicorn app.main:app --reload --port 8000
```

On startup the app also **automatically** starts (in-process, no extra
terminal needed):
- the aging-report folder watcher (`AGING_WATCH_FOLDER`)
- the GL Daily Rates folder watcher (`GL_RATES_WATCH_FOLDER`)
- the AR Receipt Methods folder watcher (`RECEIPT_METHODS_WATCH_FOLDER`)
- the daily Oracle file-pull scheduler (09:30, pulls aging/GL-rates/receipt-methods over SSH — only relevant if you've configured that jump chain; otherwise it's a harmless no-op)

Visit `http://localhost:8000/docs` to confirm it's up — you should see
~110+ routes across `/api/run`, `/api/results`, `/api/hitl`, `/api/config`,
`/api/auth`, `/api/admin`, `/api/activity-log`, `/api/bank-accounts`, etc.

Also start the background worker in a **second terminal** — this is the
one process that isn't automatic. It's what actually executes queued
analysis/ingestion jobs (polls the same Postgres via `SELECT ... FOR
UPDATE SKIP LOCKED`, no separate infra):

```bash
venv\Scripts\activate           # or source venv/bin/activate
python -m app.tasks.worker
```

And a **third terminal** for the remittance recheck loop (periodically
rescans rows waiting on a remittance advice — see
`app/tasks/remittance_recheck_worker.py`):

```bash
python -m app.tasks.remittance_recheck_worker
```

The API will run without terminals 2 and 3, but uploads will sit stuck in
"Processing" forever without the worker, and remittance-dependent rows
won't retry without the recheck loop.

## 9. Sanity check

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

## 10. Testing auth (dev bypass — no real Azure needed)

Every protected route reads the `X-Dev-User` header when `APP_ENV=local`.
Use one of the users you seeded in §6.

```bash
curl -s http://localhost:8000/api/auth/me -H "X-Dev-User: you@example.com" | python -m json.tool
```

Expect `role: "Administrator"`, `permissions: ["*"]`.

Try a route with no header at all — should 401:
```bash
curl -i http://localhost:8000/api/run/files
```

Try a permission you don't have (an Analyst calling an Oracle-Operator-only
route — seed one first with `--dev-role Analyst`):
```bash
curl -i -X POST http://localhost:8000/api/hitl/approve/1 \
  -H "X-Dev-User: analyst@example.com" -H "Content-Type: application/json" -d '{}'
```
Expect `403 Missing permission: oracle:post`.

> Note: if `API_ENCRYPTION_KEY` is set (the default), response bodies are
> AES-256-GCM sealed, not readable JSON — see §12 for how to decrypt a
> captured response. For quick plaintext curl testing, set
> `API_ENCRYPTION_ENABLED=false` in `.env` and restart.

## 11. Testing statement upload + duplicate detection

```bash
curl -s -X POST http://localhost:8000/api/run/upload \
  -H "X-Dev-User: you@example.com" \
  -F "file=@/path/to/some_statement.xlsx"
```

Upload the exact same file again — expect `"duplicate": true` with
`uploaded_by`, `uploaded_at`, and a `history_link`, plus a
`statement.upload_rejected_duplicate` row in `activity_logs`.

Check ingestion status (row-level dedup) — replace `1` with the returned
`source_file_id`:
```bash
curl -s http://localhost:8000/api/run/files/1/ingest-status -H "X-Dev-User: you@example.com"
```
Poll until `ingest_status` flips from `"processing"` to `"ready"` (needs
the worker from §8 running), then check `new_row_count` /
`duplicate_row_count`.

## 12. Testing the concurrent-run guard

Fire two `/api/run/start` calls back to back with the same
`selected_files` (two terminal tabs, or `xargs -P2`). One should succeed;
the other should either get a `409 "A run is already in progress"`
fast-fail, or — if both slipped past that check at nearly the same
instant — the losing run's `error_message` should read *"Another analysis
run is already processing these files."*

## 13. Testing RBAC admin routes

```bash
curl -s http://localhost:8000/api/admin/users -H "X-Dev-User: you@example.com" | python -m json.tool

curl -s -X PUT http://localhost:8000/api/admin/users/2/role \
  -H "X-Dev-User: you@example.com" -H "Content-Type: application/json" \
  -d '{"role_name": "Oracle Operator"}'
```

Then check `/api/activity-log?action=user.role_changed` — the entry's
`metadata` should show both `from_role` and `to_role`.

## 14. Decrypting a captured response (for debugging)

With `API_ENCRYPTION_KEY` set, every JSON response is an opaque `{"d":
"..."}` blob. Decrypt one straight from curl, or from a file saved out of
browser devtools:

```bash
curl -s http://localhost:8000/api/auth/me -H "X-Dev-User: you@example.com" \
    | python -m scripts.decrypt_payload

python -m scripts.decrypt_payload captured.json

# a capture from a different environment's key:
python -m scripts.decrypt_payload --key "<base64 key>" captured.json
```

It reads the key from this checkout's own `.env` by default.

## 15. What's unchanged / still works as before

- All `/api/config/*`, `/api/results/*`, `/api/executive-summary/*` routes
  work exactly as documented in the OpenAPI docs (`/docs`).
- Any `SourceFile` uploaded through an older code path (or already in your
  DB) has `ingest_status = NULL` — the analysis run falls back to a direct
  file-parse for those, so nothing existing breaks.

## 16. Known gaps / next steps

- **No Alembic migrations.** `init_db()` uses `Base.metadata.create_all()`
  — fine for a fresh DB; if applying this to a DB that already has data,
  review new nullable columns/tables manually or set up Alembic first.
- **`activity_logs` partitioning** (design doc) isn't applied — it's a
  single table via `create_all()`. Apply the monthly range-partitioning DDL
  from the design doc via a real migration before relying on this at high
  volume.
- **No automated pytest suite** currently checked in (`pytest`/
  `pytest-asyncio` are in `requirements.txt` for when one is added). What
  exists today is the manual smoke-test flow in this document.
