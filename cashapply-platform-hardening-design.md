# CashApply — Auth, Duplicate Detection, RBAC & Audit Logging
### System Design — Integration into Existing FastAPI/SQLAlchemy/PostgreSQL Backend

This design bolts onto the current codebase (`app/db/models.py`, `app/bff/*`, `app/bank_statement/uploader.py`,
`app/rule_engine/orchestrator.py`) rather than replacing it. `AnalysisRun`, `SourceFile`, and `LineItem`
stay as-is; new tables reference them by FK. Everything below is additive and can ship incrementally.

---

## 0. What changes structurally

Today, ingestion and analysis are one step: `run_analysis_background()` re-parses the raw file from
selected_files on every run (`bank_statement/parser.py` → `CreditRowSchema`, in-memory only, never
persisted independently of a run). That's why duplicate-row detection across separate uploads isn't
possible today — there's no durable, run-independent record of "this row was already ingested."

**The one real architectural change in this design:** split ingestion from analysis.

```
BEFORE:  Upload → SourceFile record → [Run] → parse file fresh → extract → rule-engine → LineItem

AFTER:   Upload → hash check → SourceFile record → background parse+normalize
                → StatementTransactionRow (durable, hashed, deduped)  ← NEW
                → [Run] → pull only unconsumed rows → extract → rule-engine → LineItem
```

`StatementTransactionRow` becomes the row-level dedup ledger. A `Run` no longer re-parses a file; it
selects rows from this table that haven't been consumed by a prior successful run. This is what makes
"upload today's statement, tomorrow's statement has 80% overlapping rows" actually cheap and correct.

---

## 1. Authentication — Microsoft SSO (Azure Entra ID)

### 1.1 Flow

Use the **Authorization Code flow with PKCE**, terminated at the frontend, validated at the backend.
Do **not** have FastAPI mint its own long-lived session token that's independent of Azure — that's a
second identity system to keep in sync and a second thing that can be stolen.

```
Next.js (MSAL.js)                     Azure Entra ID                  FastAPI backend
   |--- redirect to /authorize ------------->|
   |<-- auth code -----------------------------|
   |--- exchange code (PKCE) ----------------->|
   |<-- id_token + access_token ---------------|
   |--- API calls, Authorization: Bearer <access_token> ------------------------->|
   |                                                                    validates token via JWKS
   |                                                                    (issuer, audience, exp, nbf)
   |                                                                    maps oid -> local User row
   |                                                                    (JIT-provision if first login)
```

Key points:
- **Frontend**: `@azure/msal-react`. Silent token refresh handled by MSAL, not custom code.
- **Backend**: never re-implements token issuance. Every request is authenticated by validating the
  Azure-issued **access token** against Entra's JWKS endpoint (cached, short TTL) — checking `iss`,
  `aud` (your app's client ID), `exp`, and signature. Use `python-jose` or `msal`'s validation helpers.
- **No backend-issued session cookie.** The frontend holds the MSAL token in memory (not localStorage —
  MSAL's default cache is fine); every API call carries the bearer token. This avoids building a second
  session store and keeps logout/revocation authoritative in Azure.

### 1.2 User onboarding (JIT provisioning)

First successful token validation for an unseen `oid` (Azure's stable user GUID) auto-creates a `users`
row with a default low-privilege role (`Viewer`), **not** an admin. An actual Administrator then assigns
the real role via the User Management screen. This avoids a manual pre-provisioning step for every hire
while never accidentally granting elevated access on first login.

```python
# app/auth/dependencies.py
async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    claims = validate_azure_token(token)  # raises 401 on any validation failure
    user = db.query(User).filter(User.azure_oid == claims["oid"]).first()
    if user is None:
        user = User(
            azure_oid=claims["oid"],
            email=claims.get("preferred_username") or claims.get("upn"),
            display_name=claims.get("name"),
            role_id=DEFAULT_ROLE_ID,   # Viewer
            provisioned_at=dt.datetime.utcnow(),
        )
        db.add(user); db.commit(); db.refresh(user)
        log_activity(db, user, action="user.jit_provisioned", entity="User", entity_id=user.id)
    if not user.is_active:
        raise HTTPException(403, "Account disabled")
    return user
```

### 1.3 Dev/test SSO bypass — safe by construction, not by config discipline

The requirement is "easy to disable in production." The way to guarantee that isn't a `.env` flag someone
forgets to unset — it's making the bypass code path **not exist** in the production build/import graph.

```python
# app/auth/bypass.py — only ever imported when settings.ENVIRONMENT == "local"
BYPASS_ALLOWED_EMAILS: set[str] = set(settings.DEV_SSO_BYPASS_EMAILS or [])

def get_bypass_user(x_dev_user: str | None, db: Session) -> User | None:
    if settings.ENVIRONMENT == "azure":          # hard stop, not a soft check
        return None
    if not x_dev_user or x_dev_user not in BYPASS_ALLOWED_EMAILS:
        return None
    return db.query(User).filter(User.email == x_dev_user).first()
```

```python
# app/auth/dependencies.py
async def get_current_user(
    token: str | None = Depends(optional_oauth2_scheme),
    x_dev_user: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if settings.ENVIRONMENT == "local" and x_dev_user:
        from .bypass import get_bypass_user     # import gated inside the branch
        user = get_bypass_user(x_dev_user, db)
        if user:
            return user
    if not token:
        raise HTTPException(401, "Missing credentials")
    return await _validate_azure_and_load_user(token, db)
```

Two independent guards, not one: (1) the bypass module is only imported under `ENVIRONMENT == "local"`,
so it's dead code in a prod deployment even if the header is somehow sent; (2) `ENVIRONMENT` is read from
`Settings` the same way the rest of the app already switches storage backends (`local` vs `azure` in
`db/settings.py`) — reuse that existing switch rather than inventing a new flag. CI should include a test
that asserts `get_bypass_user` is unreachable / returns `None` unconditionally when `ENVIRONMENT=azure`.

### 1.4 Session/JWT handling summary

| Concern | Approach |
|---|---|
| Token issuance | Azure Entra ID only |
| Token validation | Backend validates on every request via JWKS (cached ~1h, keyed by `kid`) |
| Token storage (frontend) | MSAL in-memory cache, refreshed silently |
| Backend session state | None — stateless resource server |
| Logout | MSAL `logoutRedirect()`; backend has nothing to invalidate (no server session) |
| Revocation | Handled by Azure (disable account / revoke sessions in Entra) — reflected within token TTL |

---

## 2. Duplicate Detection

### 2.1 Exact duplicate file

```sql
CREATE TABLE statement_file_hashes (
    id              BIGSERIAL PRIMARY KEY,
    file_hash       CHAR(64) NOT NULL,        -- SHA-256 hex
    source_file_id  INTEGER NOT NULL REFERENCES source_files(id),
    uploaded_by     INTEGER NOT NULL REFERENCES users(id),
    uploaded_at     TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (file_hash)
);
CREATE INDEX ix_statement_file_hashes_hash ON statement_file_hashes(file_hash);
```

Hash is computed **before** the file touches Blob Storage — reject on the hot path, don't upload-then-check:

```python
def handle_statement_upload(db: Session, filename: str, data: bytes, uploaded_by: User) -> dict:
    file_hash = hashlib.sha256(data).hexdigest()
    existing = db.query(StatementFileHash).filter_by(file_hash=file_hash).first()
    if existing:
        prior_run = (db.query(AnalysisRun)
                       .join(SourceFile, SourceFile.id == existing.source_file_id)
                       .filter(SourceFile.id.in_(  # runs that selected this file
                           select_run_ids_for_source_file(existing.source_file_id)
                       )).first())
        return {
            "duplicate": True,
            "uploaded_by": existing.uploaded_by_user.display_name,
            "uploaded_at": existing.uploaded_at.isoformat(),
            "existing_source_file_id": existing.source_file_id,
            "existing_run_id": prior_run.run_id if prior_run else None,
            "history_link": f"/analysis-history/row/{prior_run.run_id}" if prior_run else "/analysis-history",
        }
    # not a duplicate — proceed with existing storage.save(...) + SourceFile creation
    ...
```

Frontend: `uploadStatement()` in `lib/api.ts` gets a `duplicate: true` short-circuit branch — surface it as
a dismissable banner with the link, no toast-and-forget, since the whole point is the user needs to *act*
on it (go look at the existing run) not just be told.

### 2.2 Duplicate statement **data** (row-level, cross-file)

This is the harder, more valuable one, and it's why ingestion needs to be decoupled from analysis (§0).

**Row hash = deterministic hash of normalized fields, not the raw row.** Two exports of the same
transaction from the bank rarely byte-match (column order, whitespace, date format differ), so hash the
*normalized* representation:

```python
def compute_row_hash(bank_account_id: int, statement_date: date, amount: Decimal,
                      currency: str, bank_reference: str | None, narrative: str) -> str:
    normalized = "|".join([
        str(bank_account_id),
        statement_date.isoformat(),
        f"{amount:.2f}",
        currency.upper().strip(),
        (bank_reference or "").strip().upper(),
        re.sub(r"\s+", " ", narrative.strip().upper())[:200],  # cap + collapse whitespace
    ])
    return hashlib.sha256(normalized.encode()).hexdigest()
```

Scope the hash to `bank_account_id` (not global) — the same amount/date/reference combination on two
different accounts is not a duplicate.

```sql
CREATE TABLE statement_transaction_rows (
    id                  BIGSERIAL PRIMARY KEY,
    source_file_id      INTEGER NOT NULL REFERENCES source_files(id),
    bank_account_id     INTEGER NOT NULL REFERENCES bank_accounts(id),
    row_hash            CHAR(64) NOT NULL,
    statement_date      DATE,
    credit_amount       NUMERIC(18,2),
    currency            VARCHAR(10),
    narrative           TEXT,
    bank_reference      VARCHAR,
    raw_row_json        JSONB,                 -- original parsed row, for audit/replay
    ingested_at         TIMESTAMP NOT NULL DEFAULT now(),
    consumed_by_run_id  INTEGER REFERENCES analysis_runs(run_id),   -- NULL until analyzed
    UNIQUE (bank_account_id, row_hash)
);
CREATE INDEX ix_str_unconsumed ON statement_transaction_rows(bank_account_id) WHERE consumed_by_run_id IS NULL;
```

**Bulk dedupe insert** — batch-hash the whole parsed file in memory, then let Postgres do the set
comparison in one round trip instead of N existence-checks:

```python
def ingest_rows(db: Session, source_file_id: int, bank_account_id: int, rows: list[CreditRowSchema]) -> dict:
    payload = [{
        "source_file_id": source_file_id,
        "bank_account_id": bank_account_id,
        "row_hash": compute_row_hash(bank_account_id, r.statement_date, r.credit_amount,
                                      r.currency, r.bank_reference, r.narrative),
        "statement_date": r.statement_date, "credit_amount": r.credit_amount,
        "currency": r.currency, "narrative": r.narrative,
        "bank_reference": r.bank_reference, "raw_row_json": r.dict(),
    } for r in rows]

    stmt = pg_insert(StatementTransactionRow).values(payload)
    stmt = stmt.on_conflict_do_nothing(index_elements=["bank_account_id", "row_hash"])
    result = db.execute(stmt.returning(StatementTransactionRow.id))
    inserted_ids = [row.id for row in result]
    db.commit()
    return {"total_rows": len(rows), "new_rows": len(inserted_ids), "duplicate_rows": len(rows) - len(inserted_ids)}
```

`INSERT ... ON CONFLICT DO NOTHING` with the unique constraint doing the dedup work is the right call at
this scale (not `SELECT` then filter then `INSERT`) — one statement, no N+1, no race between the check and
the insert under concurrent uploads.

**A run then consumes only unconsumed rows:**

```python
def get_unconsumed_rows(db: Session, bank_account_ids: list[int]) -> list[StatementTransactionRow]:
    return (db.query(StatementTransactionRow)
              .filter(StatementTransactionRow.bank_account_id.in_(bank_account_ids),
                      StatementTransactionRow.consumed_by_run_id.is_(None))
              .all())
```

`_run_analysis()` in `orchestrator.py` changes its Step 2 from "parse the file" to "pull unconsumed rows
for the accounts implied by `selected_files`", and stamps `consumed_by_run_id = run_id` on each row it
processes, inside the same transaction as the `LineItem` insert (so a failed run doesn't silently
"consume" rows it never actually produced a `LineItem` for).

---

## 3. Database Design

Extending, not replacing, the existing schema. Existing tables (`analysis_runs`, `source_files`,
`line_items`, `row_status_history`, `aging_invoices`, etc.) are unchanged. New tables:

```
organization_units ──< bank_accounts ──< statement_file_hashes
                                      └─< statement_transaction_rows ──(consumed_by)──> analysis_runs
                                      └─< source_files (existing, gains bank_account_id FK)

users ──< user_role (or single role_id) ──> roles ──< role_permissions ──> permissions
users ──< activity_logs
```

```sql
-- Organization structure (formalizes what's currently free-text ou_number strings
-- scattered across SourceFile / LineItem / ou_functional_currency.json)
CREATE TABLE organization_units (
    id                  SERIAL PRIMARY KEY,
    ou_number           VARCHAR(20) NOT NULL UNIQUE,
    ou_name             VARCHAR(200) NOT NULL,
    functional_currency VARCHAR(10) NOT NULL,
    active              BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE bank_accounts (
    id                  SERIAL PRIMARY KEY,
    ou_id               INTEGER NOT NULL REFERENCES organization_units(id),
    account_number      VARCHAR(50) NOT NULL,
    bank_name           VARCHAR(200) NOT NULL,
    bank_config_key     VARCHAR(100),           -- links to existing bank_ou_mapping.json entries
    currency            VARCHAR(10),
    active              BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (account_number, bank_name)
);

-- Users / RBAC
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    azure_oid       VARCHAR(64) NOT NULL UNIQUE,   -- Azure Entra stable object id
    email           VARCHAR(320) NOT NULL UNIQUE,
    display_name    VARCHAR(200),
    role_id         INTEGER NOT NULL REFERENCES roles(id),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    provisioned_at  TIMESTAMP NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMP
);

CREATE TABLE roles (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(50) NOT NULL UNIQUE,     -- Administrator, Analyst, Oracle Operator, Auditor, Viewer
    description TEXT
);

CREATE TABLE permissions (
    id      SERIAL PRIMARY KEY,
    code    VARCHAR(100) NOT NULL UNIQUE          -- e.g. "statement:upload", "run:start", "oracle:post"
);

CREATE TABLE role_permissions (
    role_id       INTEGER NOT NULL REFERENCES roles(id),
    permission_id INTEGER NOT NULL REFERENCES permissions(id),
    PRIMARY KEY (role_id, permission_id)
);

-- Duplicate detection (§2)
CREATE TABLE statement_file_hashes ( ... );          -- see §2.1
CREATE TABLE statement_transaction_rows ( ... );      -- see §2.2

-- Audit log (§7)
CREATE TABLE activity_logs (
    id            BIGSERIAL PRIMARY KEY,
    user_id       INTEGER REFERENCES users(id),       -- nullable: system/background actions
    action        VARCHAR(100) NOT NULL,               -- "statement.upload", "run.start", "oracle.post" ...
    entity_type   VARCHAR(50),                          -- "SourceFile", "AnalysisRun", "LineItem", "User"
    entity_id     VARCHAR(50),
    status        VARCHAR(20) NOT NULL,                 -- "success" | "failure"
    ip_address    INET,
    metadata      JSONB,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);

CREATE TABLE activity_logs_2026_07 PARTITION OF activity_logs
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
-- one partition per month, created ahead-of-time by a scheduled job or pg_partman

CREATE INDEX ix_activity_logs_user_time ON activity_logs (user_id, created_at DESC);
CREATE INDEX ix_activity_logs_entity ON activity_logs (entity_type, entity_id);
```

**Existing table changes (additive, nullable — safe migration):**

```sql
ALTER TABLE source_files ADD COLUMN bank_account_id INTEGER REFERENCES bank_accounts(id);
ALTER TABLE source_files ADD COLUMN uploaded_by_user_id INTEGER REFERENCES users(id);
ALTER TABLE analysis_runs ADD COLUMN triggered_by_user_id INTEGER REFERENCES users(id);
```
`triggered_by` (existing `String` column on `AnalysisRun`) and `ou_number`/`business_unit` (existing
string columns on `SourceFile`/`LineItem`) stay as-is for backward compatibility with everything already
reading them; the new FK columns are populated going forward and existing string data is backfilled by a
one-time migration script, not a hard cutover.

**Indexing / constraints already called out inline above:**
- `statement_file_hashes.file_hash` — `UNIQUE`, and the sole index needed (point lookups only).
- `statement_transaction_rows (bank_account_id, row_hash)` — composite `UNIQUE`, does double duty as the
  dedup constraint *and* the lookup index.
- Partial index on `statement_transaction_rows(bank_account_id) WHERE consumed_by_run_id IS NULL` — this
  is the one every run's Step 2 hits, keep it small by only indexing the unconsumed rows.
- `activity_logs` — monthly range partitioning. This table grows forever and is almost always queried by
  a recent time window (activity log page) or a specific entity (audit trail on a row) — partitioning
  keeps both fast without the table-size problem biting you at 1M+ rows.

---

## 4. Upload Processing Flow

```
POST /api/run/upload
   |
   |-- 1. Read bytes, compute SHA-256                         (sync, <50ms)
   |-- 2. Check statement_file_hashes                          (sync, indexed lookup)
   |       duplicate? -> return 200 {duplicate: true, ...} immediately, stop here
   |-- 3. Not a duplicate:
   |       - storage.save(...)                                 (sync)
   |       - INSERT source_files + statement_file_hashes       (sync, same txn)
   |       - enqueue background job: ingest_and_parse(source_file_id)
   |       - return 202 {source_file_id, status: "processing"}
   |
Background worker (ingest_and_parse):
   |-- detect_config() -> parse rows
   |-- compute row hashes, bulk INSERT ... ON CONFLICT DO NOTHING into statement_transaction_rows
   |-- update source_files.ingest_status = "ready" | "error", new_row_count, duplicate_row_count
   |-- log_activity(action="statement.ingest_complete", ...)

Frontend:
   |-- POST upload -> 202 -> shows "Upload successful. Processing..."
   |-- poll GET /api/run/files/{id}/ingest-status every ~2s (same polling pattern already used
   |       for run status in app/home/page.tsx's runStatus polling)
   |-- ingest_status flips to "ready" -> "You can now start Analysis." + new/duplicate row counts shown
```

This mirrors the polling pattern the frontend already uses for `getStatus()` on `AnalysisRun` — no new
frontend pattern needed, just a second poll target scoped to `SourceFile.ingest_status`.

### Concurrency: two users (or the same user, two tabs) triggering the same statement at once

Two distinct races to close:

**(a) Same file uploaded twice concurrently, before either has committed its hash row.** The
`UNIQUE(file_hash)` constraint on `statement_file_hashes` is the real guard — the "check then insert" in
application code is an optimization for the common case (fast, friendly duplicate response), but the
constraint is what's actually race-safe. Catch the `IntegrityError` on insert and turn it into the same
duplicate-response path rather than a 500.

**(b) Same file (or same set of selected files) run through `/api/run/start` twice concurrently** — the
scenario you're asking about. Two analysis runs would both try to consume the same
`statement_transaction_rows`, double-processing everything. Use a **Postgres advisory lock** keyed by a
stable hash of the bank account IDs involved, held for the duration of `_run_analysis()`:

```python
def _run_analysis(run_id: int, selected_files: list[str]) -> None:
    lock_key = advisory_lock_key(selected_files)   # stable hash -> bigint
    with session_scope() as db:
        got_lock = db.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
        ).scalar()
        if not got_lock:
            run = db.query(AnalysisRun).get(run_id)
            run.status = RunStatus.ERROR
            run.error_message = "Another analysis run is already processing these files. Try again shortly."
            db.commit()
            return
        try:
            ... existing steps 1-5 ...
        finally:
            db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
```

`pg_try_advisory_lock` is non-blocking — the second run fails fast with a clear message instead of
queuing invisibly or, worse, silently double-consuming rows. This needs a dedicated connection held for
the lock's lifetime (advisory locks are session-scoped), so grab it on its own short-lived session, not
the same session used for the long-running analysis transaction — the design above should actually pin
one connection: use `db.connection()` and keep it open for the `try/finally`, don't let the connection
pool hand that session's underlying connection back mid-run.

Belt-and-suspenders at the UI layer: disable the "Start Analysis" button optimistically once clicked, and
have `/api/run/start` check `AnalysisRun.status == RUNNING` for overlapping `selected_files` before even
attempting the lock, so the common case (impatient double-click) never gets past a friendly 409 with a
clear message, and the advisory lock is the last-resort correctness guarantee for true concurrent
requests from different tabs/users.

---

## 5. Background Processing

Current state: `run_analysis_background()` spawns a bare `threading.Thread` — explicitly marked in the
code as PoC-only. Two workloads now need a real queue: analysis runs (existing) and file
ingestion/parsing (new, §4).

**Recommendation: Celery + Redis**, not because it's fancier, but because you already have:
- a **CPU/IO-bound, potentially long-running** job (Layer 2B AI batching, `CHUNK_MAX_WORKERS=4` /
  `AI_BATCH_MAX_CONCURRENCY=4` — this is real concurrent external API traffic, not a quick task)
- a need for **retry with backoff** (Oracle posting failures, transient AI rate limits)
- a need for **job status visibility** across page reloads / multiple users watching the same run

A `threading.Thread` PoC can't survive an app restart mid-run, doesn't retry, and doesn't give you a
task registry to build the activity log's "Oracle Posting Failure" / "Finish Analysis" events off of
cleanly. Redis is a small, well-understood piece of infra to add, and doubles as the JWKS cache (§1) and
rate-limit counter store if you want it later.

If you want to avoid adding Redis specifically: **`procrastinate`** (Postgres-backed task queue) gets you
the same retry/visibility semantics using infra you already run, at the cost of somewhat lower throughput
ceilings than Celery+Redis — reasonable if the AI batch concurrency stays in the current ballpark
(≤16 concurrent calls) and you'd rather not operate a second datastore.

Either way: **ingestion and analysis become separate queues/task types**, so a slow analysis run never
blocks a fast file upload's "processing → ready" turnaround, and each can be scaled/monitored independently.

---

## 6. Activity Logging

Single append-only table (§3), written via a plain service call at each action site — not purely via
middleware, because several required events (`Oracle Posting Failure`, `Finish Analysis`) carry domain
context (which run, which Oracle error) that generic request/response middleware can't see.

```python
# app/audit/service.py
def log_activity(
    db: Session, user: User | None, action: str,
    entity_type: str | None = None, entity_id: str | None = None,
    status: str = "success", ip_address: str | None = None,
    metadata: dict | None = None,
) -> None:
    db.add(ActivityLog(
        user_id=user.id if user else None,
        action=action, entity_type=entity_type, entity_id=str(entity_id) if entity_id else None,
        status=status, ip_address=ip_address, metadata=metadata or {},
    ))
    # Deliberately does NOT call db.commit() — rides on the caller's existing transaction so a
    # log entry never exists for an action that then rolled back, and vice versa.
```

Two call sites cover almost everything:
1. **A thin FastAPI middleware** logs every authenticated request generically (`action = f"{method} {path}"`,
   `ip_address` from `request.client.host`, `status` from response code) — catches login/logout/view/download
   for free.
2. **Explicit calls at domain-significant points** for anything middleware can't contextualize: inside
   `apply_transition()` (state changes), `hitl/service.py`'s `approve_row`/`reject_row`, `fusion_client.py`'s
   post result handling, and User Management mutations (role/permission changes — these specifically should
   log **both** the before and after role in `metadata`, since "role changed" without "from what to what"
   is close to useless for an audit).

IP address: pull from `request.client.host`, but if this ever sits behind a load balancer/App Gateway,
read `X-Forwarded-For` (first entry) instead — worth deciding this explicitly now rather than discovering
every logged IP is the load balancer's address after the fact.

---

## 7. Authorization — RBAC

### 7.1 Model

Flat role → permission mapping (§3 schema) — no need for per-resource ACLs given the five roles specified.
One row per user is enough (`users.role_id`); multi-role-per-user can be added later via a join table
without touching the permission-check call sites if you design the dependency around "does this user have
permission X" rather than "what role is this user."

| Role | Representative permissions |
|---|---|
| Administrator | `*` (all) |
| Analyst | `statement:upload`, `run:start`, `run:view` |
| Oracle Operator | `oracle:post`, `oracle:retry`, `run:view` |
| Auditor | `run:view`, `report:download`, `activity_log:view` (read-only, no mutations) |
| Viewer | `dashboard:view` |

### 7.2 FastAPI implementation

```python
# app/auth/permissions.py
def require_permission(permission_code: str):
    def dependency(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        has_perm = (
            permission_code == "*" or
            db.query(RolePermission)
              .join(Permission)
              .filter(RolePermission.role_id == user.role_id,
                      Permission.code.in_([permission_code, "*"]))
              .first() is not None
        )
        if not has_perm:
            raise HTTPException(403, f"Missing permission: {permission_code}")
        return user
    return dependency
```

```python
# app/bff/hitl_routes.py — applied at the route, not buried in service logic
@router.post("/approve/{id}")
def approve(id: int, body: ApproveBody,
            user: User = Depends(require_permission("oracle:post")),
            db: Session = Depends(get_db)):
    ...
```

Cache the role→permission set per request (it's read once per `require_permission` call already scoped to
the request's DB session — no extra caching needed at this scale; revisit only if `role_permissions`
lookups show up in profiling).

This slots directly into the existing gap flagged in `hitl_routes.py`'s own docstring — the Oracle-POST
gate today is enforced only by category (`ready_for_oracle`), not by *who* is calling; `require_permission
("oracle:post")` adds the missing identity check without touching the existing `not_approvable` logic.

---

## 8. Project Structure

Extends the existing `app/app/` layout — new top-level packages alongside `bank_statement/`, `rule_engine/`,
`oracle/`, etc., following the same one-concern-per-package convention already in place:

```
app/app/
├── auth/                     # NEW
│   ├── dependencies.py       # get_current_user, require_permission
│   ├── azure_validator.py    # JWKS fetch/cache, token validation
│   ├── bypass.py             # local-only, gated by ENVIRONMENT
│   └── jit_provision.py
├── audit/                    # NEW
│   ├── service.py            # log_activity()
│   └── middleware.py         # generic request logging
├── ingestion/                 # NEW — the ingestion/analysis split from §0
│   ├── file_hash.py
│   ├── row_hash.py
│   └── ingest_service.py      # ingest_and_parse() background job
├── db/
│   ├── models.py               # existing — gains User/Role/Permission/ActivityLog/
│   │                            # StatementFileHash/StatementTransactionRow/BankAccount/OrgUnit
│   └── ...                     # unchanged
├── bff/
│   ├── auth_routes.py          # NEW — /api/auth/me, session-adjacent endpoints
│   ├── admin_routes.py         # NEW — user/role/permission management
│   ├── run_routes.py           # existing, gains permission deps + ingest-status endpoint
│   ├── hitl_routes.py          # existing, gains require_permission("oracle:post") etc.
│   └── ...                     # unchanged
├── rule_engine/                # unchanged
├── oracle/                     # unchanged
└── ...
```

Everything net-new is isolated in `auth/`, `audit/`, and `ingestion/` — the existing pipeline packages
(`rule_engine`, `oracle`, `extraction`) get permission dependencies added at their route layer only,
no internal changes.

---

## 9. Performance

| Concern | Recommendation |
|---|---|
| Batch inserts | `INSERT ... ON CONFLICT DO NOTHING` for row dedup (§2.2) — one round trip per file, not per row |
| Hash generation | Compute all row hashes in-process before the DB call; SHA-256 of a short normalized string is cheap even at 1M rows (~seconds, not minutes) |
| Blob uploads | Stream via `save_stream()` (already exists in `storage/client.py`) for large files instead of buffering full bytes in memory — currently `handle_statement_upload` takes `data: bytes`; switch large-file paths to the stream variant |
| PostgreSQL indexing | Composite unique index doing double duty as dedup constraint *and* lookup path (§3); partial index for "unconsumed rows only" |
| Optimistic locking | Add a `version` column (`Integer`, incremented on update) to `LineItem` for the HITL approve/reject path — two SPOCs approving the same row concurrently should get a conflict, not a silent overwrite. `AnalysisRun` concurrency is handled by the advisory lock instead (§4), since that's a "don't even start" case, not an "detect after the fact" case |
| Transaction boundaries | Row ingestion (§2.2) and its `SourceFile.ingest_status` update commit together; a run's `LineItem` insert and `consumed_by_run_id` stamp commit together (§0) — never leave a row marked consumed without a corresponding `LineItem` |
| Parallel processing | Existing `CHUNK_MAX_WORKERS` / `AI_BATCH_MAX_CONCURRENCY` pattern is sound — extend it to ingestion (parse+hash can chunk the same way extraction already does) rather than inventing a second parallelism model |
| Large files (100k–1M rows) | Chunked reads for CSV/Excel parsing (don't load 1M rows into a single DataFrame if avoidable — `bank_statement/extractor/*` should stream where the format allows); batch the `ON CONFLICT` insert itself (e.g. 5k-row batches) rather than one 1M-row `INSERT` statement, to keep lock duration and memory bounded |

---

## 10. Security note (adjacent to this design, worth fixing alongside it)

`db/settings.py` currently has real credentials as **default values** in the `Settings` class
(`ANTHROPIC_API_KEY`, `ORACLE_BASIC_PASSWORD`) rather than only in `.env`/environment/a secrets store.
Anyone importing `Settings()` without an `.env` file gets working production credentials. Given this
design is introducing real auth/RBAC/audit boundaries, it's worth removing the hardcoded defaults at the
same time — set them to `None`/required-with-no-default and fail startup loudly if missing, rather than
silently falling back to embedded secrets.

---

## Summary of new/changed tables

**New:** `organization_units`, `bank_accounts`, `users`, `roles`, `permissions`, `role_permissions`,
`statement_file_hashes`, `statement_transaction_rows`, `activity_logs`.

**Changed (additive, nullable FKs — no breaking migration):** `source_files` gains `bank_account_id`,
`uploaded_by_user_id`; `analysis_runs` gains `triggered_by_user_id`; `line_items` gains `version`
(optimistic locking).

**Unchanged:** `line_items` core columns, `rule_definitions`, `app_config`, `aging_invoices`,
`remittance_extractions`, `remittance_invoice_lines`, `row_status_history`.
