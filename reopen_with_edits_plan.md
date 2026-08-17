# Reopen with edits + bucket recomputation — implementation plan

**Status:** ready to implement. All decisions settled.
**Date:** 2026-08-17
**Approach doc:** `cashapply_backend/reopen_with_edits_approach.md` — read §2 and
§3 first if picking this up cold; they explain *why* the shape is what it is.

---

## Scope

A SPOC reopening a rejected row (or a parked overpayment) gets an edit screen,
sees the resulting rule/bucket previewed, and only then is the row reopened with
its bucket recomputed from the edits.

**Settled decisions** (full reasoning in the approach doc §6):

| | Decision |
|---|---|
| Editable | Customer + invoice mapping. Amounts never typed — always read from aging |
| Modes | One path: every reopen goes through edit + re-evaluation |
| Preview | Preview then confirm |
| Ready for Oracle | Still requires an explicit Approve & Post |
| Customer field | Locked once `standard_receipt_id` exists (invoice edit only there) |
| Classifier | Routed by what was edited |
| `overpayment_parked` | Covered by the same flow |
| Reject comment | Added |
| `processed` rows | No work — precedence already excludes them from Rejected |
| `post_failed` rows | Keep Reopen (recovery matters where money is stuck) |
| Duplicate flags | **Dropped** — premise was wrong, see approach §4c |

---

## Slice order

```
1  shared rule-input builder      (no user-visible change; de-risks 4 & 5)
2  schema: audit fields           (independent, tiny)
3  reject comment                 (independent; feeds slice 6's "why")
4  preview endpoint               (needs 1)
5  confirm endpoint               (needs 1, 2, 4)
6  frontend modal                 (needs 4, 5; nicer with 3)
7  cutover + retire old reopen    (needs 6)
```

1 → 2 and 3 in parallel → 4 → 5 → 6 → 7.

---

## Slice 1 — One shared rule-engine input builder

**Why.** `evaluator.py:280-302` documents an invariant that exactly three
`evaluate_row()` call sites exist and that any fourth must supply
`credit_memos_lookup` (it raises `KeyError` otherwise, deliberately). Slice 4
would be that fourth. Three hand-written builders already drift from each other —
adding a fourth by hand makes a known problem worse.

**Files**
- New: `app/rule_engine/rule_input.py` — `build_rule_engine_input(db, line_item,
  aging_map, *, customer_name=None, remittance_view=None, ou_status=None)`
  returning the input dict.
- Edit: `app/rule_engine/orchestrator.py` (~:380-430), `remittance_recheck.py`
  (~:62-120), `customer_name_correction.py` (~:169-216) — all three call the new
  helper.
- Edit: `evaluator.py:280-302` — update the documented call-site invariant.

**Carry forward, do not "clean up"**: the hardcoded
`duplicate_invoice_across_customers=False` / `already_processed_match=False` stay
exactly as they are (approach §4c — changing them is a separate feature). Keep
`customer_name_correction`'s deliberate `customer_match_pct=100.0` /
`customer_text_match=True` as explicit overrides, not as new defaults.

**Also fix here** (cheap, same file, real gap): pass `settlement_type` /
`settlement_provider` through, which the two re-eval paths currently drop.

**Verification** — zero-diff discipline, as used for the aging-negatives work:
- Script that builds the input dict via the old inline code and the new helper for
  a real sample of LineItems, asserting the dicts are key-for-key identical
  (callables compared by identity of what they return for sample args).
- Re-run a real analysis against a known statement; assert every row's `rule_id`,
  `reason_code`, `current_state`, `target_total`, `shortfall_pct` is unchanged.
- `python scripts/check_schema_drift.py` clean (no schema change expected).

**Risk.** Touches the main analysis path. Highest-risk slice in the plan, which is
why it is first and verified by equality rather than by inspection.

---

## Slice 2 — Audit fields

**Files:** `app/db/models.py` — three nullable columns on `LineItem`, beside the
existing `manually_mapped` / `customer_name_corrected` sets:

```
reopen_edited_at      DateTime  nullable
reopen_edited_by      String    nullable   # SPOC email
reopen_edit_summary   JSON      nullable   # the before/after diff actually applied
```

`reopen_edit_summary` holds the committed diff (customer before/after, invoice
numbers before/after, rule_id before/after) so the row itself records that its
outcome came from a human edit at reopen, not from analysis —
`RowStatusHistory`'s free-text comment is not queryable for this.

**Apply:** `python scripts/check_schema_drift.py --apply` (no Alembic in this
project), then re-run without `--apply` to confirm clean.

---

## Slice 3 — Reject comment

Backend already supports it: `reject_row(db, id, comment, ...)`
(`app/hitl/service.py:337`) persists it to `RowStatusHistory`, and
`api.ts:406` already accepts a `comment` argument. **No caller passes one.**

**Files**
- New: `cashapply_frontend/components/row-detail/RejectRowModal.tsx` — follow
  `EditGlRateModal.tsx` (there is no generic dialog primitive; `components/ui`
  has only `EmptyState`, `LoadingSpinner`, `PageHeader`).
- Edit: `app/analysis-history/row/[id]/page.tsx` — route the `reject` action to
  the modal instead of `ActionBar`'s inline confirm; pass the comment through.
- Edit: `scripts/seed_actions.py:36-39` — `confirm_required` → `False` for
  `reject` (the modal is the confirmation; keeping both double-prompts).
- Also covers the list-page and drawer Reject paths
  (`app/analysis-history/page.tsx:890-895`, `components/RowDetailDrawer.tsx:204-226`)
  — either route them through the same modal or leave them comment-less
  deliberately, but decide rather than drift.

**Comment stays optional.** Making it mandatory would change an existing,
working action's contract for every bucket.

**Verification:** reject a row with and without a comment; confirm the
`RowStatusHistory` row carries it and the row-detail history renders it.

---

## Slice 4 — Preview endpoint (read-only)

`POST /api/hitl/{id}/reopen-preview`, permission `hitl:reject` (same tier as
reopen today). Body: `{ customer_name?: str, invoice_numbers?: list[str] }`.
Persists **nothing**.

**Files**
- New: `app/hitl/reopen_with_edits.py` — `preview_reopen(db, line_item_id,
  customer_name=None, invoice_numbers=None)`.
- Edit: `app/bff/hitl_routes.py` — the route, mirroring the error-mapping style
  of the existing reopen route (`:141-168`).

**Eligibility.** Its own check — do *not* relax
`customer_name_correction._is_correctable()`'s `_LOCKED_STATES`, which guards a
different endpoint with different guarantees. Eligible when
`hitl_status == "rejected"` **or** `current_state == "overpayment_parked"`.

**Guards, all reported rather than raised** (the preview's job is to explain, not
to fail): reuse `reopen_row`'s three — version, every claimed invoice still in the
*current* aging map, no conflicting claim via `check_duplicate` — plus:

- **Customer locked** when `standard_receipt_id is not None`. Returns a
  `customer_locked` flag with the reason, so the UI can disable the field and say
  why rather than failing on confirm (approach §3, Blocker A).
- Cross-currency selections without a resolvable rate → surfaces as the R13
  outcome, exactly as manual mapping's `_classify` already does.

**Classification routing** (approach §3, Blocker B):

| Edited | Classifier |
|---|---|
| invoice_numbers present | `manual_mapping._classify` (via a refactor of `preview_manual_mapping`'s validation, reused not copied) |
| customer only | `evaluate_row()` through slice 1's builder |
| nothing | `evaluate_row()` — an unchanged row should land on its existing `rule_id` |

**Response**
```
{ eligible, customer_locked, customer_locked_reason,
  from: { rule_id, reason_code, category, customer_name, invoice_numbers,
          target_total, shortfall_pct },
  to:   { ...same shape... },
  bucket_pinned_by,        # non-null when reference_status pins the bucket (post_failed)
  blockers: [ {code, message} ],
  changed: bool }
```

`bucket_pinned_by` is the honest-preview requirement from approach §2a: a
`post_failed` row keeps Reopen, but re-evaluation cannot move its bucket
(`reference_status == "failed"` outranks `rule_id`). The screen must say so
instead of implying a move.

**Verification:** drive `preview_reopen` against real rows for each case — no
edit, customer-only edit, invoice edit, invoice-gone-from-aging,
invoice-claimed-elsewhere, receipt-exists (customer locked), parked overpayment,
`post_failed` (pinned bucket), cross-currency with no rate.

---

## Slice 5 — Confirm endpoint

`POST /api/hitl/{id}/reopen-confirm`, same permission and body as preview, plus
`comment` and `expected_version`.

**Re-validates from scratch** — never trusts the client's preview (the aging map
or the row can move in between), exactly as `confirm_manual_mapping` does.

**Ordering is load-bearing.** Two traps from approach §4a/§4b:

```
1. Guards (all of slice 4's, now hard failures). Nothing mutated yet.
2. release_applications(db, r)                  ← §4b: prevents a stale claim leak
3. Apply edits. Preserve the original AI guess once:
   ai_extracted_customer_name = extracted_customer_name  (only if not already
   customer_name_corrected — mirrors customer_name_correction.py:156-157)
4. Classify (routed per slice 4)
5. apply_transition(...)   ← §4a: MUST run while current_state is still an Enum
6. Clear diagnosis fields no longer in force: overpayment_reason/_evidence,
   shortage_reason/_evidence; for a parked row also overpayment_disposition/_at/_by
7. hitl_status = None; pre_reject_state = None; pre_park_state = None;
   version += 1
8. record_application(status="pending") if the new outcome claims invoices;
   rebuild oracle_payload in try/except; write RowStatusHistory
   (trigger="spoc_reopen_with_edits") + reopen_edit_* audit fields
```

Step 5 before step 7 is not stylistic: `state_machine.py:36` reads `from_state`
via `.value` and then writes `current_state` as a plain **string**, so calling it
after the state has been reassigned raises `AttributeError` — the bug documented
at `customer_name_correction.py:228-235`.

`apply_transition` also brings its own protection for free: for
`ready_to_post` / `acceptable_short_payment` it re-runs `check_duplicate` per
invoice and overrides to R19 / `INVOICE_ALREADY_APPLIED` if anything is already
claimed (`state_machine.py:51-63`). That is the live duplicate protection — worth
knowing it is inherited, not re-implemented.

**Two-gate model:** if the new outcome is `ready_to_post`, the row lands in Ready
for Oracle awaiting Approve & Post. This endpoint never posts to Oracle.

**Invoice edits set `manually_mapped = True`**, which keeps the existing
invariant that a human-chosen mapping is exempt from later automatic
re-evaluation (`remittance_recheck.py:177-181`).

**Verification:** for each preview case, confirm and assert the full field set,
that the bucket recomputes as predicted, that `InvoiceApplication` rows end in
the right status (specifically: an outcome that no longer claims invoices leaves
**no** `pending` claim behind — the §4b regression), that a second confirm on the
same row is refused, and that `import app.main` still succeeds.

---

## Slice 6 — Frontend modal

**New:** `cashapply_frontend/components/row-detail/ReopenAndReviewModal.tsx`,
following `HandleOverpaymentModal.tsx`'s shape.

Contents:
- **Why it was rejected** — the reject comment (slice 3) and the state it was
  rejected from (`pre_reject_state`), so the SPOC has the context they need.
- **Read-only bank facts** — amount, date, narrative, account. Never editable.
- **Customer picker** — reuse `IdentifiedCard.tsx`'s aging-backed dropdown
  (`getCustomerNameOptions`); never free text. Disabled with the returned
  `customer_locked_reason` when a receipt exists.
- **Invoice picker** — reuse `ManualInvoiceMappingCard.tsx`'s picker and its
  `CustomerCreditsPanel`. Amounts display-only, from aging.
- **Live preview panel** — debounced call to slice 4 on every change. States the
  move in plain words: *"Rejected → Ready for Oracle (R9c → R9a)"*. Renders
  `blockers` inline and disables Confirm while any exist. When
  `bucket_pinned_by` is set, says plainly that the bucket is fixed by the Oracle
  outcome and will not move.

**Edit:** `app/analysis-history/row/[id]/page.tsx` — `handleAction("reopen")`
opens this modal instead of calling `reopenEntry` directly (`:137-146`).
**Edit:** `lib/api.ts` — `previewReopen`, `confirmReopen`.
**Edit:** `scripts/seed_actions.py:47-50` — `confirm_required` → `False` for
`reopen` (the modal confirms).

`applicable_categories` stays `["rejected", "overpayment_parked"]` — unchanged,
and correct for both decisions: `processed` rows never reach category `rejected`,
and `post_failed` rows that were rejected do, so they keep the button.

**Verification:** `npx tsc --noEmit` clean, then walk each case in the UI —
including a parked overpayment and a receipt-exists row (customer field visibly
disabled with its reason).

---

## Slice 7 — Cutover

Once slice 6 is live and exercised, `POST /api/hitl/reopen/{id}` has no remaining
caller. Keep it working through slices 4–6 (safe migration, nothing stranded
mid-change), then in this slice either delete it or leave it as an explicitly
deprecated pure-undo path.

**Recommendation: delete it.** Two reopen paths with different semantics is
exactly the drift this feature exists to remove, and `reopen_row`'s guard logic
should already have been extracted and reused by slice 5 rather than duplicated.

**Verification:** grep for `reopenEntry` / `/reopen/` across both apps to confirm
no caller survives; full analysis run + reopen round-trip.

---

## Cross-cutting risks

| Risk | Mitigation |
|---|---|
| Slice 1 changes automatic-run behaviour | Equality-verified against the old builders on real rows before anything else is built |
| `apply_transition` crashes on ordering | Ordering fixed and commented in slice 5; a test asserts confirm succeeds on a row whose `current_state` was already reassigned once |
| Stale invoice claim leak (§4b) | Explicit `release_applications` in step 2 + a verification asserting no orphan `pending` claim |
| Customer changed under a live receipt | Locked server-side in slices 4/5, not only disabled in the UI |
| Preview and confirm disagree | Confirm re-validates from scratch; preview is advisory only |
| SPOC reads a pinned `post_failed` bucket as movable | `bucket_pinned_by` surfaced and stated in the modal |

---

## Out of scope

- Making `duplicate_invoice_across_customers` / `already_processed_match` real,
  and the R0/R5 dead-rule question (approach §4c — flagged as its own task).
- Editing OU, currency, or FX rate on reopen. FX already has its own path
  (`edit_gl_rate`); OU has no row-level edit UI at all today.
- Oracle receipt reverse-and-recreate (approach §3, options A2/A3).
- Applying credit memos in Oracle — separate work, see
  `credit_memo_application_plan.md`.
