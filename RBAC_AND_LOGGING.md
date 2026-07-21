# Error Codes, Logging & RBAC — Backend Changes

## 1. Error codes

Every user-facing error is now defined once in `app/common/error_codes.py`
(`ErrorCode.XXX`), grouped by the numbering scheme from the requirements:

| Range     | Domain                          |
|-----------|----------------------------------|
| 1000-1999 | Auth / RBAC                      |
| 2000-2999 | Upload / Ingestion                |
| 3000-3999 | Config / Config Builder           |
| 4000-4999 | Run / Analysis                    |
| 5000-5999 | HITL / Approval                   |
| 6000-6999 | Oracle / Fusion                   |
| 7000-7999 | Results / Metrics / Aging         |
| 8000-8999 | Admin / Users                     |
| 9000-9999 | System / Validation / Unexpected (also the fallback bucket — see below) |

Every response looks like:

```json
{ "code": 5000, "title": "Not ready for approval", "message": "This row isn't eligible for approval yet.", "request_id": "a1b2c3d4e5f6" }
```

### Migration status: COMPLETE

**Every `raise HTTPException(...)` in the codebase has been migrated** to
`raise AppError(ErrorCode.XXX, ...)` with a specific, meaningful code —
verified with `grep -rn "raise HTTPException" app/` returning zero results.
This covers: `auth/dependencies.py`, `auth/permissions.py`,
`bff/run_routes.py`, `bff/storage_routes.py`, `bff/config_routes.py`,
`bff/config_builder_routes.py`, `bff/hitl_routes.py`, `bff/admin_routes.py`.

~25 new `ErrorDef` entries were added across the 2000/3000/4000/5000/8000
ranges to cover cases that previously had no specific code (aging-source
lookup, config-builder file reading, run-start guards, mapping/remittance
failures, multi-role admin validation, etc).

The legacy `AppError(status, "Title", "message")` call style (a handful of
spots in `config_builder_routes.py`'s exception handlers) still works
unchanged — mapped onto a generic-but-defined code for that status
(`ErrorCode.GENERIC_*`) — but every call site that had a natural home in
the registry now uses it directly.

### Adding a new one

```python
from ..common.errors import AppError
from ..common.error_codes import ErrorCode

raise AppError(ErrorCode.ACCOUNT_UNRESOLVED, detail=f"account '{acct}' has no OU mapping")
```

`detail` is optional, short, and non-technical — it's appended to the
code's canned message in parentheses.

## 2. Logging

`app/common/logging_config.py`:

- `LOG_LEVEL` env var — `DEBUG` or `INFO` (default `INFO`).
- One plain-text file per calendar day in `LOG_DIR` (default `./logs`),
  e.g. `logs/app.txt.2026-07-17`, rotating at midnight. `LOG_RETENTION_DAYS`
  controls retention (default 365).
- Every log line is tagged with a trace reference: `[a1b2c3d4e5f6]` for an
  HTTP request (matches the `request_id` the user sees), or
  `[job:run:482]` for a background job — see `app/common/request_context.py`.
- **Severity split**: `LIGHT` errors (bad input, "not found", a business-
  rule rejection) log one short INFO line, no traceback. `HEAVY` errors
  (unhandled exceptions, failed integrations) always log the full
  traceback via `logger.exception()`, regardless of `LOG_LEVEL`.
- Best-effort secret redaction on every log line, console or file.

## 3. Request tracing

`RequestIdMiddleware` (registered in `main.py`) assigns a short id to every
request, echoes it back as a response header and inside every error body
as `request_id`. Background jobs (`tasks/analysis_tasks.py`,
`tasks/ingestion_tasks.py`, `tasks/remittance_recheck_worker.py`) call
`set_job_context(...)` for the same traceability.

## 4. RBAC — the 5-role model, now with MULTI-ROLE support

**An Administrator can assign a user any number of roles at once** (e.g.
both Analyst and Oracle Operator) — this required a real data-model
change:

- `db/models.py`: replaced the old `User.role_id` single foreign key with
  a `UserRole` join table (`User.roles`, many-to-many). `User.role_names`
  is a convenience property.
- `auth/permissions.py::get_user_permission_codes()` computes the
  **union** of every assigned role's permissions — holding a wildcard
  ("*", Administrator) role satisfies every check regardless of what
  else is assigned.
- `auth/role_priority.py` (new) — display-only ordering ("Administrator"
  before "Analyst" in a badge list) — never used for permission logic.
- `bff/auth_routes.py`'s `/me` now returns both `roles` (the full list)
  and `role` (back-compat singular, = highest-priority role for display).
- `bff/admin_routes.py`: `onboard_user` / `update_user` now take
  `role_names: list[str]` (the COMPLETE set — replaces, not merges).
  Lockout guards ("can't demote/deactivate the last active administrator")
  now check across a user's full role set, not a single role.
- `scripts/seed_rbac.py`'s role/permission matrix is unchanged — only how
  roles attach to a *user* changed.

| Role            | Permissions                                          |
|-----------------|-------------------------------------------------------|
| Administrator   | `*` (everything)                                       |
| Analyst         | `run:view`, `run:start`, `statement:upload`, `hitl:map` |
| Oracle Operator | `run:view`, `hitl:map`, `hitl:reject`, `oracle:post`    |
| Auditor         | `run:view`, `activity_log:view`                        |
| Viewer          | *(none — restricted to the Welcome page)*              |

**Tested end-to-end** with a `TestClient` against a throwaway sqlite DB:
seeded all 5 roles, created a user holding BOTH Analyst + Oracle Operator
at once, confirmed their `/me` permission list was the correct union, used
the admin API to change a user's role set from `["Analyst"]` to
`["Analyst", "Auditor"]` and back, and confirmed the DB and in-memory
state both reflected it correctly (a real bug — a stale in-memory
relationship cache — was found and fixed during this testing; see
`bff/admin_routes.py::_set_user_roles`'s docstring).

## 5. Bank Accounts info page + multi-Business-Unit support

New nav item "Bank Accounts" — viewable by everyone with `run:view`
(same tier as Config/Overview), lists every onboarded bank account and
the Business Unit(s) it belongs to. An Administrator (`config:manage`)
can reassign an account's Business Unit(s) inline.

**Data model**: most accounts belong to exactly one Business Unit
(`BankAccount.ou_id`, unchanged). A new `BankAccountOU` join table holds
ADDITIONAL Business Units for the accounts that genuinely receive
payments for more than one — `BankAccount.all_organization_units` /
`all_ou_numbers` gives the full set (primary first).

**"Changes only affect new runs"**: this required a real fix, not just a
UI note. Previously, `business_unit`/`ou_number` for every row came from a
snapshot frozen at ingestion time (`raw_row_json`) — changing an account's
Business Unit via Config didn't even affect NEW runs, only a re-ingest.
`rule_engine/orchestrator.py` now resolves the account's CURRENT primary
Business Unit fresh (one live DB lookup per source file, not per row) each
time a new run starts, so:
- A run that already completed keeps whatever Business Unit was current
  when it ran — `LineItem.business_unit` is a permanent snapshot.
- The next run started after an admin's change picks up the new value
  automatically, no re-ingestion needed.

**Multi-BU cross-OU detection**: `rule_engine/ou_resolver.py`'s
`resolve_ou_status()` now accepts the account's FULL Business Unit set
(`bank_ou_numbers`), not just one. A payment is only flagged cross-OU if
the customer's invoice Business Unit has NO overlap with any Business
Unit the account is linked to — so a multi-BU account isn't wrongly
flagged just because the customer's invoice happens to be in its second
linked Business Unit rather than its primary one. Verified with a unit
test covering all 4 cases (single-BU match/mismatch, multi-BU match on
the secondary BU, multi-BU still-cross-OU on a genuinely unrelated BU).

New endpoints (`bff/bank_accounts_routes.py`):
- `GET /api/bank-accounts` — list (run:view)
- `GET /api/bank-accounts/business-units` — Business Unit options for the picker (run:view)
- `PUT /api/bank-accounts/{id}/business-units` — reassign primary + additional Business Units (config:manage)

## 7. Row actions — now data-driven

Per request, "which actions can be taken on a row" moved out of scattered
frontend JSX conditions + backend gates into one seed-defined table.

**New `ActionDefinition` table** (`db/models.py`) — one row per possible
action (Approve, Reject, Map Invoice, Recheck Remittance, Retry Oracle),
each with: the permission it needs, which row categories it applies to,
an optional extra state condition (e.g. Retry only when
`reference_status == "failed"`), whether it needs a confirm dialog, and
danger/sort-order for display. Seeded via `python -m scripts.seed_actions`
(idempotent, mirrors `seed_rbac.py`'s pattern).

**`hitl/actions_registry.py::get_available_actions(db, line_item, user_permission_codes)`**
resolves the row's category (reusing the same `_category_for_row()`
dashboard/ledger use, so it can never drift from what the UI calls
"Ready for Oracle"), checks the row against each definition's category +
condition, checks the permission set, and returns only the actions that
pass **both** — one list, ready to render.

Wired into `GET /api/results/row-detail/{id}` — the response now includes
`available_actions: [{code, label, icon, confirm_required, is_danger}]`,
computed from the ACTUAL signed-in user's permissions.

**Adding a future action** (the stated plan) is now: add one entry to
`scripts/seed_actions.py`, and — if it needs a genuinely new state check —
one function in `CONDITION_CHECKS`. No frontend conditional, no new
backend permission-check-in-three-places problem.

**Verified end-to-end**: seeded 3 rows (ready_for_oracle, post_failed,
conflict_exception) and 4 users (one per non-Viewer role), called the
resolver directly, and confirmed the exact expected action list for every
row × role combination — e.g. Analyst gets `map_invoice` on a post_failed
row but not `retry_oracle` (needs `oracle:post`, which Analyst doesn't
hold); Oracle Operator gets both.

**Now wired to the frontend**: `app/analysis-history/row/[id]/page.tsx`'s
header renders `<ActionBar>` (new, `components/row-detail/ActionBar.tsx`)
driven entirely by `detail.available_actions` — the old hand-rolled
`canApprove`/`canReject`/`canRetry`/`canRecheckRemittance` booleans are
gone. "Map Invoice" scrolls to the existing manual-mapping card rather
than firing an API call directly (mapping needs the SPOC to pick an
invoice first). Confirmed end-to-end with a live HTTP call returning the
exact JSON both new components expect.

**Also found and fixed while wiring this up**: `is_cross_ou_currency` was
being computed on `RuleResult` (evaluator.py) but never actually copied
onto the `LineItem` row in `rule_engine/state_machine.py::apply_transition`
— it stayed permanently `False` in the database regardless of what the
rule engine decided. The frontend had been silently working around this
by deriving cross-OU status from `reason_code`/`WRONG_OU_*` instead. Fixed
alongside adding the new evidence field, since both belong together.

**Cross-OU now shows its evidence, not just its verdict.** Previously the
Row Detail page's "Why this status" card showed only the bank's OU vs. the
FIRST matched invoice's OU. `rule_engine/ou_resolver.py` now captures —
and `LineItem.ou_evidence` (new JSON column) persists — every OU the
customer was actually found in, the exact matched customer name, the
fuzzy match confidence, and the outstanding amount/invoice count there.
The new `CrossOUEvidencePanel` component renders this as a table, with the
bank account's own OU(s) highlighted — including an explicit note when the
account is multi-BU. This is a real historical record (computed once, at
evaluation time), not a live re-guess against today's aging map.

**One dead-code finding along the way**: `components/RowDetailDrawer.tsx`
is not imported anywhere in the app — an earlier iteration superseded by
the full `analysis-history/row/[id]/page.tsx`, left behind. Not touched
further since it isn't live; worth deleting in a future cleanup pass.

## 8. Known follow-up

- Azure AD SSO on the backend (`auth/azure_validator.py`,
  `auth/dependencies.py`, `auth/onboarding.py`) was already built and left
  untouched, per the original request.
- `auth/jit_provision.py` remains intentionally unwired (separate,
  already-discussed follow-up).

