# Reopen with edits — test guide

**Test file:** `storage/bank-statements/Reopen_Test_HSBC_914031244.xlsx`
**Aging report it was built from:** `Debtors ageing 31 Mar 1.xlsx` — every amount
below was read out of that file, not invented. If you load a different aging
export, regenerate the statement (§8) or the figures will not line up.
**Target account:** 914031244 · HSBC Bank USA NA · USD · OU 111
**Date:** 2026-08-18

---

## 1. What is actually being tested

Reject writes only `hitl_status`. It never touches `rule_id` — and the bucket you
see is derived from `rule_id`. So the old reopen, a pure undo, always handed the
row back in the bucket it was rejected from, with the same mapping and no way to
change it.

Reopen now opens a modal where you can change the **customer** and/or the
**invoice mapping**, see the resulting rule and bucket assessed live, and confirm.
The outcome is recomputed from those edits.

Three things must hold in every scenario below:

1. **Preview and confirm never disagree.** If the modal enables *Reopen Row*, the
   confirm must succeed. If it blocks, it must say why. Two real bugs of exactly
   this kind were found and fixed during the build — do not assume this is safe.
2. **Nothing reaches Oracle.** A row landing in Ready for Oracle still needs an
   explicit Approve & Post afterwards.
3. **No invoice is left falsely claimed.** If you remap away from invoice A onto
   invoice B, A must become claimable by another payment again.

---

## 2. Setup

> **If you already analysed an earlier version of this file, reset first.**
> The invoices in it are now *claimed* by those rows, so re-running would hit
> duplicate-claim conflicts (R19) instead of the outcomes below, and the upload
> dedupe would reject the file as already-seen. Stop the backend and worker, then:
>
> ```bash
> cd cashapply_backend && venv/Scripts/python.exe -m scripts.reset_transactional
> ```
>
> That wipes runs, rows, claims and upload hashes but keeps accounts, recipes,
> logins and the action buttons. Use `--dry-run` first to see the scope.
>
> **Then two things that are easy to get wrong, both hit on 2026-08-18:**
>
> 1. **Bump `VALUE_DATE` in `build_reopen_test.py` and regenerate the statement
>    BEFORE re-running.** Receipt numbers are
>    `CASHAPPLY-<ou>-<YYYYMMDD>-<line_item_id>`, and the reset restarts that id
>    sequence at 1 — but the receipts already created in Oracle are *not*
>    deleted. Re-running the same statement therefore regenerates numbers Oracle
>    already holds, and receipt creation fails with `AR-857749` ("already a
>    receipt with this number, date, amount, and customer") on any row whose
>    date/amount/customer also line up. Changing the value date changes every
>    receipt number and sidesteps it. Dev/test only — in a real deployment ids
>    never restart.
> 2. **Re-upload the aging report AND call refresh-aging.** The reset clears
>    `app_config`, which holds the aging snapshot. Uploading the file only loads
>    it into the API process's memory; the analysis runs in the *worker*, which
>    reads the snapshot from `app_config`. Without the refresh the run fails
>    immediately with "No aging map loaded":
>
>    ```bash
>    curl -X POST -H "X-Dev-User: admin@example.com" >      -F "file=@storage/aging-reports/<your aging file>.xlsx" >      http://localhost:8000/api/config/upload-aging
>    curl -X POST -H "X-Dev-User: admin@example.com" -H "Content-Type: application/json" >      -d '{}' http://localhost:8000/api/config/refresh-aging
>    ```

1. Confirm the aging report above is the one loaded (Home → Aging Report card).
2. Home → Account Statements → **Upload** → pick
   `Reopen_Test_HSBC_914031244.xlsx`.
   Only one statement can sit in the list at a time now, so remove any existing
   one first.
3. Wait for **Ready (10 pending)**, then **Start Analysis**.
4. Go to Analysis History.

Each row's narrative starts with its test id (`T1 …`, `T2 …`), so you can identify
it at a glance in the list without opening it. The ids are two or three
characters, below the six-character minimum the invoice-number locator matches,
so they cannot be mistaken for an invoice number.

The workbook's second sheet, `EXPECTED (not read by app)`, carries the same
answer key as §3 for reference while you work.

---

## 3. Part 1 — confirm the starting buckets

Ten rows, one per starting bucket, so every reject→reopen path is reachable.
Find each by its **Bank reference**.

| Ref | Amount | Invoice(s) in narrative | Expected start |
|---|---|---|---|
| REOPEN-T1 | 17,550.00 | 11172600006350 Voltaire | **Ready for Oracle** (R9a exact) |
| REOPEN-T2 | 13,756.00 | 11172600003366 SiTime | **Short Payment** (R9b — 5% short, no credit memos) |
| REOPEN-T3 | 232,500.00 | 11172600006255 Assurant | **Conflict / Shortage** (R9c — 7% short, *within* the 12% tolerance, but held back because Assurant holds 2 open credit memos) |
| REOPEN-T4 | 4,294.67 | 11172600002745 Uber | **Conflict** (R11 unexplained overpayment) |
| REOPEN-T5 | 25,000.00 | none — "HACH COMPANY" | **Unidentified** (R8) with AI extraction off — see note |
| REOPEN-T6 | 1,500.00 | none — garbage narrative | **Unidentified** (R8) |
| REOPEN-T7 | 3,160.00 | 11172600003285 + 11172600003286 Cepheid | **Ready for Oracle** (R9a, two invoices) |
| REOPEN-T8 | 1,995.00 | 11172600001566 Muus | **Conflict** (R11) → you will *park* this one |
| REOPEN-T9 | 20,800.00 | 11172600006119 Beckman | **Ready for Oracle** (R9a) — Beckman has 9 open invoices, so this is the remap case |
| REOPEN-T10 | 4,752.00 | 11172600005171 Esko | **Ready for Oracle** (R9a) — the no-edit case |

### Confirmed against a real run (AI extraction DISABLED)

This table was verified end to end on 2026-08-18 with AI extraction off. Totals:

```
Unidentified 2   Ready for Oracle 4   Short Payment 1   Conflict/Exception 3
```

Nine of the ten landed exactly as predicted. The one difference is **T5**: with
AI extraction disabled it lands in **Unidentified**, not Needs Remittance,
because resolving a bare customer name out of narrative text is the AI layer's
job — regex alone finds no customer signal, so the row has nothing to go on.
That is correct behaviour, not a defect, and Unidentified is an equally useful
starting point for the reopen tests (§4.6 treats it as one).

**With AI extraction enabled**, expect T5 to move to **Needs Remittance (R7)**
and the totals to become Unidentified 1 / Needs Remittance 1, everything else
unchanged.

**Re-confirmed on a second clean cycle** (value date `05/08/2026`, AI still off):
identical totals, all ten rows in the same buckets, and this time **8/8 Oracle
receipts created with no failures** — the two AR-857749 collisions from the first
cycle were purely the receipt-number reuse described in §2.

If any other row starts somewhere else, note where it landed and carry on — every
scenario below only needs a *known* starting bucket, not a specific one. The
arithmetic and the credit-memo facts in the table were verified against the aging
report and the live 12% tolerance, so those are solid regardless.

---

## 4. Part 2 — the core matrix

For each: open the row (Analysis History → click it), note its bucket and rule,
then **Reject** → **Reopen**.

### 4.1 T1 — the original complaint: Ready for Oracle, rejected, reopened

1. Reject it. A modal now asks for a **reason** — type "wrong invoice" and reject.
2. Confirm it moves to **Rejected**.
3. Click **Reopen**. The modal must show:
   - your rejection reason, and that it was rejected from `review_approve`
   - received amount, value date, account — all read-only
   - the customer, editable (this row has no Oracle receipt)
   - Voltaire's invoice already ticked, because that is the mapping it has
   - an assessment reading roughly *Exact match → Exact match, no change*
4. Click **Reopen Row** without changing anything.
   **Expect:** back in Ready for Oracle, unchanged. This is the old behaviour and
   must still work.

### 4.2 T9 — remap to a different invoice (the headline case)

The scenario that was impossible before.

1. Reject T9 with a reason.
2. Reopen. Untick Beckman `11172600006119` (20,800.00) and tick a sibling instead
   — e.g. `21172600014272` (156,366.00).
3. Watch the assessment update. Paying 20,800 against a 156,366 invoice is a
   large shortfall, so expect a **recorded short payment (R9d)** — the rule and
   bucket visibly change from where the row started.
4. Confirm.
   **Expect:** the row leaves Rejected and lands in the bucket the preview named.
   Its mapping is the sibling invoice. It is marked manually mapped.
5. **Critical check:** the invoice you moved *away from* (`...6119`) must be free
   again. Open another row and confirm `...6119` is selectable in Manual Invoice
   Mapping. If it is still blocked as "already applied", the claim leaked — that
   is a real bug, report it.

### 4.3 T10 — reopen with a customer change

1. Reject T10 (Esko, 4,752.00) with a reason.
2. Reopen. Change the **customer** dropdown to a different real customer, e.g.
   `Beckman Coulter Inc.` Leave the invoices alone.
3. The invoice list reloads to that customer's invoices, and the assessment
   re-runs the rule engine against the new customer.
4. Confirm.
   **Expect:** confirm **succeeds**. The customer is now the new one, and the
   rule/bucket is whatever the preview said.

> This path was **broken until 2026-08-18** and failed two different ways: a
> crash (`'str' object has no attribute 'value'`), and a silent refusal on rows
> that had been manually mapped. Both are fixed, but this is the scenario to be
> most suspicious of. If the preview enables Confirm and the confirm then errors,
> the bug is back.

### 4.4 T7 — multi-invoice row, drop one invoice

1. Reject T7 (Cepheid, 3,160.00 across two invoices).
2. Reopen. Untick one of the two, leaving only `11172600003285` (1,200.00).
3. Now 3,160.00 is being applied to a 1,200.00 invoice → an **overpayment**. The
   modal must demand a reason for the excess before enabling Confirm.
4. **Try to confirm without picking a reason** — the button must stay disabled and
   a blocker must be visible. This is the preview/confirm agreement test.
5. Pick "Paid in advance", confirm.
   **Expect:** lands in Overpayment — Ready to Post, disposition recorded.

### 4.5 T8 — a *parked* overpayment, reopened

Parked overpayments go through the same flow, not the old undo.

1. Open T8 (Muus, 1,995.00 against a 395.00 invoice → R11).
2. **Handle Overpayment** → **Explain & Close**, pick any reason. The row becomes
   **Overpayment Parked**.
3. Click **Reopen** (offered on parked rows too).
   **Expect:** the modal opens and its context header says *Parked as* rather
   than *Rejected*.
4. Change the invoice selection, confirm.
   **Expect:** the row leaves parked, and its previous park reason is **cleared** —
   it must not still claim to be explained. The row history should show the
   discarded decision.

### 4.6 T2, T3, T4, T5, T6 — the remaining buckets

Reject and reopen each, doing at least one real edit. What matters is that the
modal opens sensibly from every bucket and the bucket recomputes.

- **T2** (Short Payment) — remap to a smaller invoice so it becomes an exact
  match. Expect a move to Ready for Oracle.
- **T3** (Shortage with credit memos) — the modal should show Assurant's two
  credit memos (`11172600006698` = 1,773.00 and `11172600006695` = 1,446.30) as
  context. They are **information only**; nothing here applies a credit memo in
  Oracle. Remap to a smaller invoice and confirm the shortage clears.
- **T4** (Overpayment) — pick enough invoices to absorb the 4,294.67.
- **T5** (Needs Remittance) — this row has *no* mapping, so reopen is the first
  time invoices are chosen at all. Set a customer, pick invoices, confirm.
- **T6** (Unidentified) — same, from nothing whatsoever. Confirms the flow works
  on a row with no prior customer and no prior invoices.

---

## 5. Part 3 — the guards (these must all block)

Each must produce a **visible blocker with Confirm disabled** — never a crash,
and never a failure after clicking Confirm.

| # | How to produce it | Expected |
|---|---|---|
| G1 | Open the reopen modal, then in a second tab approve or reject the same row. Confirm in the first tab. | "modified by another user" version conflict; nothing changes |
| G2 | Reject two rows. Reopen row A and map it to an invoice already claimed by row B. | "now applied to another payment … would double it up" |
| G3 | Reject a row, then load a different aging report that no longer contains its invoice. Reopen. | "no longer in the current aging report" |
| G4 | Reject a row, then unload/replace the aging report entirely. Reopen. | "No aging report is currently loaded" |
| G5 | Pick invoices that overpay, leave the reason blank. | Confirm disabled, plus a blocker naming the excess (§4.4) |
| G6 | Call `/api/hitl/{id}/reopen-preview` directly on a row that is neither rejected nor parked. | `not_reopenable` |

Two browser tabs on the same row is the cleanest way to produce G1.

---

## 6. Part 4 — cases needing Oracle, or a DB nudge

These two cannot be produced locally without Oracle connectivity, because they
depend on a receipt actually existing or a reference post actually failing.

### 6.1 Customer locked by an existing receipt

The customer must become **read-only** once an Oracle receipt exists, because
that receipt is stamped with the current customer and reject never voids it.

Force it locally by setting a fake receipt id on a rejected row:

```sql
UPDATE line_items SET standard_receipt_id = 'TEST-LOCK-1' WHERE id = <row id>;
```

Reopen it. **Expect:** the customer shown as plain text with an explanation, no
dropdown, and the invoice mapping still editable. Confirm that a mapping change
still works. Then clear it:

```sql
UPDATE line_items SET standard_receipt_id = NULL WHERE id = <row id>;
```

Also confirm the **server** enforces it, not just the UI — with the lock set,
POST `/api/hitl/{id}/reopen-preview` with a different `customer_name` must come
back with a `customer_locked` blocker.

### 6.2 A post_failed row keeps Reopen but its bucket cannot move

A row whose Oracle reference post failed keeps its Reopen button — deliberately,
because cash is sitting on a created receipt and it must stay recoverable. But
`reference_status` outranks `rule_id` in the bucket precedence, so re-evaluation
genuinely cannot move it out of `post_failed`.

Force it with `UPDATE line_items SET reference_status = 'failed' WHERE id = <row id>;`
then reject and reopen.

**Expect:** an amber notice in the modal saying the bucket is fixed by the Oracle
outcome and reopening will not move it. Editing the mapping still works; the
bucket stays `post_failed`. If the modal silently promises a move it cannot
deliver, that is the bug.

Note that a row rejected from `processed` (`reference_status = 'success'`) never
appears in Rejected at all — precedence returns it to `processed` — so Reopen is
correctly not offered there. Nothing to test beyond confirming the button is
absent.

---

## 7. Regression — things that must NOT have changed

- **Manual Invoice Mapping** still works on an ordinary unidentified /
  needs-remittance / conflict row, and still refuses on a row that already has a
  recorded decision.
- **Customer-name correction** from the Identified card still works normally.
- **Approve & Post** still works, and is still required after a reopen lands a row
  in Ready for Oracle. Reopening must never post anything by itself.
- **The old `/api/hitl/reopen/{id}` endpoint** still exists and still behaves as
  the pure undo. It simply has no UI caller now.
- **Reject is no longer offered once a row's invoice references have posted
  successfully to Oracle** (`reference_status == "success"`). Rejecting such a row
  cannot reverse the Oracle application, and previously it released the internal
  ledger claim and left the row unreachable. Confirm that a `processed` row shows
  no Reject button, and that `POST /api/hitl/reject/{id}` on one returns 422.
  A `post_failed` row is still rejectable — nothing was applied in Oracle there.
- A full analysis run over any normal statement still produces the same buckets it
  did before — the refactors behind this were behaviour-preserving extractions.

---

## 8. Regenerating the test file

The amounts are tied to the aging export. If you load a different one, rebuild
rather than editing by hand — a stale figure turns a real failure into a
confusing one. Each row is just a customer + one of their invoices + a delta:

| Ref | Customer | Invoice | Amount paid |
|---|---|---|---|
| T1 | Voltaire Inc. | 11172600006350 | exactly outstanding |
| T2 | SiTime Corporation | 11172600003366 | 95% of outstanding |
| T3 | Assurant, Inc. | 11172600006255 | 93% of outstanding |
| T4 | Uber Technologies Inc | 11172600002745 | outstanding + 4,000 |
| T5 | Hach Company | none (name only) | 25,000 |
| T6 | none | none | 1,500 |
| T7 | Cepheid | 11172600003285 + 11172600003286 | sum of both |
| T8 | Muus Collective, Inc. | 11172600001566 | outstanding + 1,600 |
| T9 | Beckman Coulter Inc. | 11172600006119 | exactly outstanding |
| T10 | Esko Graphics BV | 11172600005171 | exactly outstanding |

Statement columns (recipe v2 for this account): `Value date` (DD/MM/YYYY),
`Narrative`, `Credit amount`, `Account number`, `Currency`, `Bank name`,
`Bank reference`, on a sheet named `Sheet1` with the header on row 0. Each
narrative is prefixed with its test id.

The builder script resolves every amount from the live `AgingMap` and fails loudly
if an invoice has gone:

```bash
cd cashapply_backend && PYTHONPATH=. venv/Scripts/python.exe build_reopen_test.py
```

---

## 9. What "100% confident" looks like

- [ ] Reopen with **no edits** returns the row exactly where it was (§4.1)
- [ ] Reopen with an **invoice remap** moves the bucket (§4.2)
- [ ] Reopen with a **customer change** succeeds (§4.3) — the recently-broken path
- [ ] Reopen from **every** starting bucket in §3 opens a sensible modal
- [ ] A **parked overpayment** reopens and loses its stale disposition (§4.5)
- [ ] A row with **no prior mapping** can be mapped for the first time at reopen
- [ ] All six guards in §5 block *before* Confirm, never after
- [ ] The **abandoned invoice is claimable again** after a remap (§4.2 step 5)
- [ ] **Customer is locked** when a receipt exists, server-side too (§6.1)
- [ ] A **post_failed** row says its bucket cannot move (§6.2)
- [ ] Nothing in §7 regressed
