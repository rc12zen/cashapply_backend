# Reopen with edits + bucket recomputation — approach

**Status:** approach agreed — all decisions taken (§6). Not a plan, not
implemented. One residual call outstanding in §6a (`post_failed` rows).
**Date:** 2026-08-17

---

## 1. What was asked

When a row is rejected from any bucket it lands in Rejected. On reopen, the SPOC
should be able to edit the relevant fields, see the effect assessed, have the
bucket recomputed from the edits, and only then have the row reopened.

Decisions taken up front:

| Question | Decision |
|---|---|
| Which fields are editable | Customer + invoice mapping. Amounts stay non-typeable (always read from the aging report) |
| Reopen modes | **One path.** Every reopen goes through the edit screen and re-evaluation — today's pure undo is replaced, not kept alongside |
| Preview | Preview then confirm — show the resulting rule/bucket before anything is written |
| If it lands in Ready for Oracle | Still needs an explicit Approve & Post. Two-gate model preserved |

---

## 2. Why the bucket doesn't currently move — the mechanical core

`_category_for_row()` (`app/bff/metrics.py:206-271`) decides the bucket by
first-match precedence:

| # | Condition | Bucket |
|---|---|---|
| 1 | `reference_status == "success"` | `processed` |
| 2 | `hitl_status == "rejected"` | `rejected` |
| 3 | `reference_status == "failed"` | `post_failed` |
| 4 | `receipt_eligibility == "discarded"` | `discarded` |
| 5 | `current_state == "distributed"` | `distributed` |
| 6 | `current_state == "overpayment_parked"` | `overpayment_parked` |
| 7 | settlement override + `needs_distribution` | derived |
| 8 | *else* | `RULE_ID_TO_GROUP[rule_id]` |

`reject_row()` (`app/hitl/service.py:337-385`) writes **only** `hitl_status`,
`current_state`, `status`, `pre_reject_state`, and releases the invoice claim. It
never touches `rule_id`, `matched_invoices`, `target_total`, or `shortfall_pct`.

So rejection is a *hat* worn on top of an otherwise intact row, and reopen just
takes the hat off — which mechanically returns the row to whatever bucket
`rule_id` mapped to before. **This is the whole reason a reopened
Ready-for-Oracle row comes back identical and unedited.** Recomputing the bucket
therefore means rewriting `rule_id`, which means re-running a classifier.

### 2a. Consequence: for some rows the bucket cannot be recomputed at all

Precedence steps 1–6 outrank `rule_id`. A row rejected from `processed`
(`reference_status == "success"`) or `post_failed` (`"failed"`) is **pinned by
its Oracle outcome, not by its rule**, so no amount of re-evaluation will move it
— re-running the engine changes `rule_id`, and step 8 never gets consulted.

Any row can be rejected today: `reject_row` has no category gate, and the action
registry seeds Reject with `applicable_categories=None`
(`scripts/seed_actions.py:36-39`), so it shows on every bucket including
`processed`. So this is a real population, not a theoretical one.

**Scoping decision needed** (§6, Q1): either the edit-and-recompute flow is
restricted to rows whose bucket derives from `rule_id`, or those rows get a
different, honest treatment — the one thing that must not happen is a screen
promising recomputation that silently does nothing.

---

## 3. Two blockers

### 🔴 Blocker A — the Oracle receipt already exists and encodes the customer

A bare receipt is created for **every credit row** right after analysis,
regardless of category and with no SPOC approval
(`build_receipt_creation_payload`, `app/oracle/fusion_client.py:379-481`). Its
payload includes:

```
"CustomerAccountNumber":  _resolve_customer_account_number(line_item)   # :467
"Currency":               invoice_currency                             # :469
"Amount":                 amount_in_invoice_ccy                        # :470
```

Reject never voids it — Oracle is never told about the rejection at all — which
is exactly why today's pure-undo reopen can safely reuse it: *nothing changed.*

Editing breaks that invariant:

- **Change the customer** → the existing receipt is registered in Oracle against
  the **wrong customer**. Approving afterwards applies cash to the wrong
  account.
- **Change the invoice selection to a different currency** → `invoice_currency`
  changes, so `Currency` and `Amount` on the live receipt are now wrong too.

`patch_standard_receipt` exists and works, but is only ever used for
`ConversionRate` (`app/hitl/service.py:840-843`,
`app/hitl/distribution_actions.py:551-552`). Whether Oracle permits patching
`CustomerAccountNumber` on an existing receipt is **unverified, and unlikely** —
that is normally a reverse-and-recreate operation. The `edit_gl_rate` docstring
says as much for the analogous case: *"needs a reverse-and-recreate correction,
not a field edit, and isn't handled here"* (`service.py:744-745`).

**DECIDED — A1.** Allow a customer change only while no receipt exists
(`standard_receipt_id is None`). Where one exists, offer the invoice-mapping edit
only, and say plainly on screen why the customer is locked. No Oracle probe.

Rejected alternatives, kept for the record: A2 — probe whether Oracle accepts a
PATCH of `CustomerAccountNumber` (needs test-pod access and a write test);
A3 — reverse-and-recreate the receipt on a customer change (largest scope, real
money-movement risk, needs Finance sign-off).

### 🔴 Blocker B — there are two classifiers, and they disagree by design

| Path | Classifier | Rule IDs it can produce |
|---|---|---|
| Customer edit (`customer_name_correction.py:218`) | `evaluate_row()` | the full automatic set |
| Invoice edit (`manual_mapping.py::_classify`) | its own, mirroring R9 | R9a/R9b/**R9d**/**R9e**/R12/R13 |

They are deliberately different. `_classify` caps every selected invoice at its
own outstanding (`stated_amount = outstanding_amount`,
`manual_mapping.py:596`), which is what makes SPOC-picked overpayments (R9e) and
beyond-tolerance short payments (R9d) safe to record — outcomes the automatic
engine must never produce, because `_resolve_matched_invoices` assigns a single
invoice the whole received amount and would over-apply
(`manual_mapping.py:400-411`).

So "recompute the bucket" is ambiguous: which classifier runs?

**DECIDED —** route by what was actually edited.

- Invoices edited → `manual_mapping`'s classifier, and the row becomes
  `manually_mapped = True`.
- Customer only → `evaluate_row()`.
- Nothing edited → `evaluate_row()` (see the risk in §4c).

This also stays consistent with an existing invariant: `manually_mapped == True`
already exempts a row from every re-evaluation path
(`remittance_recheck.py:177-181`, `customer_name_correction.py:52-60`), so a
human-chosen mapping is never silently overwritten later.

---

## 4. Traps in the existing precedent that must not be inherited

`customer_name_correction.py` is the closest working precedent and it carries
four defects. Building on it as-is would propagate all four.

### 4a. `apply_transition()` must be called while `current_state` is still an Enum

`state_machine.py:36` reads `from_state` via `.value`, then replaces
`current_state` with a **plain string**. A second call in the same session
raises `AttributeError` — the bug documented at
`customer_name_correction.py:228-235`.

`reopen_row` already assigns a plain string (`service.py:513`). **Ordering is
therefore load-bearing:** re-evaluate and `apply_transition` first, clear
`hitl_status` after. Getting this backwards is an immediate crash, not a subtle
bug.

### 4b. Stale invoice claims leak when a row leaves a claiming category

`apply_transition` calls `record_application(status="pending")` only for
`ready_to_post` / `acceptable_short_payment` (`state_machine.py:111-112`), and
nothing releases the old claim when the new outcome is a non-claiming category.
`customer_name_correction` never calls `release_applications` either — so an
R9a → R9c re-evaluation leaves the invoice claimed by a row that no longer
claims it, silently blocking another payment from matching it.

The new path must `release_applications` before re-staking, unconditionally.

### 4c. ~~Re-evaluation is not faithful to the original run~~ — CORRECTED, not a risk

**An earlier revision of this document claimed** that
`duplicate_invoice_across_customers` and `already_processed_match` were real
computed values on the original run and only shortcut to `False` on the
re-evaluation paths, so a no-change reopen could flip a row out of R5 and lose
duplicate detection. **That was wrong.**

Both flags are hardcoded `False` on **every** path, the main analysis run
included (`orchestrator.py:424-425`), whose own comment states there is *"no live
cross-row duplicate check ... today"* (`orchestrator.py:929-932`).

Therefore:

- Re-evaluation is **already faithful** on these two flags — all four call sites
  pass an identical `False`, so re-running the engine cannot change an outcome
  through them.
- **R0 and R5 are unreachable rules today.** Each gates solely on one of these
  flags (`evaluator.py:366`, `:393`), so neither can ever fire, despite both
  carrying frontend labels in `lib/constants.ts`. Out of scope here; flagged
  separately.
- Decision 3 is consequently **dropped from this feature's scope** — making the
  flags real means building cross-row duplicate detection that does not exist,
  which is a separate and much larger change.
- Real duplicate protection is unaffected and already covers this flow: it lives
  in `invoice_ledger.check_duplicate`, invoked by `apply_transition`'s pre-step
  (→ R19) and by manual mapping's preview. The reopen path inherits it for free.

Still genuine, lower stakes: `settlement_type` / `settlement_provider`
are never passed into the re-evaluation input, and
`overpayment_reason`/`_evidence`, `shortage_reason`/`_evidence`,
`invoice_match_pct`, `confidence_score` are never cleared — so an R11 → R9a
correction leaves a stale overpayment diagnosis attached to a row that is no
longer overpaid.

### 4d. A fourth `evaluate_row()` call site

`evaluator.py:280-302` documents an invariant that exactly three call sites
exist, and that any fourth must supply `credit_memos_lookup` (it raises
`KeyError` otherwise, by design). Adding a fourth independent input-builder makes
the known silent-divergence risk worse.

**Recommendation:** extract `build_rule_engine_input(db, line_item, aging_map,
**overrides)` into one shared helper and move all four call sites onto it, rather
than writing a fourth by hand. This reduces existing risk instead of adding to
it, and is the reason 4c exists at all.

---

## 5. Proposed shape

### Backend

**New module** `app/hitl/reopen_with_edits.py`, composing existing pieces rather
than reimplementing them.

Two endpoints, mirroring the proven preview/confirm pattern of manual mapping:

- `POST /api/hitl/{id}/reopen-preview` — read-only. Body carries the proposed
  customer and/or invoice numbers. Returns: resulting `rule_id`, resulting
  bucket, `target_total` / `shortfall_pct`, the before→after diff, and any
  blocking validation.
- `POST /api/hitl/{id}/reopen-confirm` — re-validates from scratch (never trusts
  the client preview), then commits in one transaction.

Confirm sequence, in this order:

1. Guards, before any mutation — reuse `reopen_row`'s three (version, invoice
   still in current aging, no conflicting claim) plus a receipt-invalidation
   guard per Blocker A.
2. `release_applications(db, r)`.
3. Apply the edits, preserving the original AI guess once
   (`ai_extracted_customer_name`, as `customer_name_correction.py:156-157`
   does).
4. Classify — routed per Blocker B.
5. `apply_transition(...)` **while `current_state` is still an Enum**.
6. Clear stale diagnosis fields for the outcome no longer in force.
7. Clear `hitl_status`, `pre_reject_state`; bump `version`.
8. Re-stake the claim; rebuild `oracle_payload`; write `RowStatusHistory` with a
   diff-bearing comment.

**Gate to relax:** `_is_correctable()` (`customer_name_correction.py:52-60`)
currently refuses `rejected` outright via `_LOCKED_STATES`. The new path needs
its own eligibility check rather than a blanket relaxation of that one — it
guards a different endpoint with different guarantees.

**New audit fields** on `LineItem`, following the established
`manually_mapped`/`customer_name_corrected` shape:
`reopen_edited_at` / `reopen_edited_by` / `reopen_edit_summary`, so the row
itself records that its outcome came from a human edit at reopen and not from
analysis. No Alembic in this project — apply via
`scripts/check_schema_drift.py --apply`.

### Frontend

- Replace the inline "Sure?" confirm on Reopen (`ActionBar.tsx:92-116`) with a
  **Reopen & Review modal**, following `HandleOverpaymentModal.tsx` /
  `EditGlRateModal.tsx` (there is no generic dialog primitive to reuse).
- Modal contents: the rejection reason and the state it was rejected from,
  read-only bank facts, the customer picker (reuse `IdentifiedCard.tsx`'s
  aging-backed dropdown — never free text), the invoice picker (reuse
  `ManualInvoiceMappingCard.tsx`'s), and a live preview panel.
- Preview must state the move in plain words — *"Rejected → Ready for Oracle
  (R9c → R9a)"* — and, where §2a applies, say plainly that the bucket is fixed
  by the Oracle outcome and will not change.

---

## 6. Decisions taken

All resolved 2026-08-17 by Yash.

1. **Rows pinned by Oracle outcome** (§2a) — **Reopen is hidden.** See §6a below:
   this is free for `processed` but has a consequence for `post_failed` that
   needs one more call.
2. **Blocker A** — **A1.** Lock the customer field once a receipt exists
   (`standard_receipt_id is not None`) and offer only the invoice-mapping edit
   there. No Oracle probe, no reverse-and-recreate.
3. **No-change reclassification** (§4c) — decided as "fix the two flags", then
   **dropped as unnecessary** once the premise was found to be wrong: the flags
   are hardcoded `False` everywhere including the main run, so re-evaluation is
   already faithful and R0/R5 are unreachable rules. Nothing to fix here for this
   feature. See §4c.
4. **`overpayment_parked`** — **covered by the edit flow too**, not left on the
   pure undo.
5. **Reject comments** — **add a comment option** on reject, so the reopen modal
   can show why the row was rejected.
6. **Blocker B** — **route by what was edited** (invoices → manual-mapping
   classifier + `manually_mapped = True`; customer only → `evaluate_row()`).

### 6a. Consequence of decision 1 — one call still needed

`reopen` is gated to `applicable_categories = ["rejected", "overpayment_parked"]`
(`scripts/seed_actions.py:47-50`). Crossed with the precedence table in §2, the
decision splits in two:

- **Rejected from `processed`** — `reference_status == "success"` wins at step 1,
  so the row displays as `processed`, never enters the Rejected bucket, and
  Reopen is already not offered. **Decision 1 requires no work here** — it is
  already the behaviour.
- **Rejected from `post_failed`** — `hitl_status == "rejected"` (step 2) beats
  `reference_status == "failed"` (step 3), so the row **does** display as
  `rejected` and Reopen **is** offered today.

Hiding Reopen on the second case **strands the row permanently in Rejected.**
That matters more than it sounds: `post_failed` means the Oracle receipt was
created but its invoice references failed to attach, so real cash is sitting
unapplied in Oracle on a row that would then have no recovery path at all.

There is already an acknowledged gap in the same area — `map_invoice` excludes
rejected rows and its comment calls post_failed *"a separate known issue — same
guard, out of scope for this change"* (`seed_actions.py:75-76`).

**DECIDED — keep Reopen for `post_failed`.** Recovery matters most precisely
where money is stuck. Net effect: no action-registry change is needed for
decision 1 at all. `processed` rows are already excluded by precedence (they
never reach category `rejected`), and `post_failed` rows keep the button they
have today.

---

## 7. Reference

- Reject: `app/hitl/service.py:337-385` · route `app/bff/hitl_routes.py:123-138`
- Reopen (pure undo today): `app/hitl/service.py:388-542` · route `:141-168`
- Bucket derivation: `app/bff/metrics.py:206-271`, `RULE_ID_TO_GROUP` at `:115-159`
- Re-evaluation precedent: `app/rule_engine/customer_name_correction.py`
- Manual-mapping classifier: `app/hitl/manual_mapping.py:316-460`
- Transition: `app/rule_engine/state_machine.py:34-122`
- Receipt payload: `app/oracle/fusion_client.py:379-481`
- Modal patterns: `components/row-detail/HandleOverpaymentModal.tsx`,
  `EditGlRateModal.tsx`
