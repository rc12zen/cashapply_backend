# CashApply — Context File: Credit Card, Cheque, Third-Party Provider, Short Payments

This is a reference document for the four scenarios discussed and partially built
in this conversation. Use it to pick the work back up, hand it to another
developer, or re-orient after a break.

**Repos involved:**
- `app` — backend (FastAPI), referred to below as **App1**
- `ssss` — frontend (Next.js)
- `agent` — remittance-reading agent, referred to below as **App2**. Shares the
  same Postgres database as App1 for two tables — see the shared-table note
  under Scenario 1/2 below.

**Migration:** all schema changes below are captured in `apply_schema_changes.sql`
(delivered separately) — run it once against the shared database; it's
idempotent (safe to re-run).

---

## 1. Credit Card Payments

**Source:** PRD — CashApply Edge Case: Credit Card Payments (Gaurav Aggarwal, 27 Jul 2026)

**Business shape:** Bank shows one consolidated deposit per day (4–5
transactions/month, clustered around month-end) = sum of every card
processed that day. A separate 3% processing fee is a distinct debit line.
Finance emails a manual breakdown (Zensar invoice numbers, customer names,
per-transaction amounts) after each batch.

**Identification:** fixed bank narration reference ending `526221017886`.

### What's built
| Piece | File | Status |
|---|---|---|
| Row identity / short-circuit rule | `app/rule_engine/evaluator.py` — rule `R16`, reason `CARD_SETTLEMENT_DETECTED`, category `needs_distribution` | ✅ Built |
| Detection config + classifier | `app/bank_statement/settlement_identifier.py` (reads `SettlementIdentifier` table, type `card_narrative`) | ✅ Built |
| DB table for the pattern list | `app/db/models.py` — `SettlementIdentifier` | ✅ Built |
| Management UI (add/remove pattern) | `ssss/components/bank-accounts/SettlementIdentifiersCard.tsx`, mounted on Accounts & OU's page | ✅ Built |
| Row Detail badge | `ssss/components/row-detail/specialFlags.tsx` — "Credit Card Settlement" badge | ✅ Built |
| Ledger tab / dashboard bucket | `Needs Distribution` tab, its own KPI card, included in Executive Summary's non-posted pills | ✅ Built |
| Remittance agent recognizes the finance breakdown email | `agent/extractors/claude_extractor.py` — `document_type: "card_breakdown"`, per-line `customer_name` on `RemittanceInvoiceLine` | ✅ Built (shared-table columns added to **both** App1 and App2 `db/models.py`) |
| **Split & Map screen** (enter/confirm the actual customer/invoice breakdown, create the receipts) | — | ❌ **Not built** — this is the next piece |
| 3% fee handling as a non-shortfall debit | — | ❌ **Not built** |

**Open item from the PRD itself:** exact subject-line/sender pattern for
finance's breakdown email — needed before the agent can auto-detect it
reliably (currently detection logic exists but needs that pattern registered).

---

## 2. Cheque Payments

**Source:** PRD — CashApply Edge Case: Cheque Payments (Gaurav Aggarwal, 27 Jul 2026)

**Business shape:** US customers mail physical cheques; staff deposit them
(3–5 day lag); multiple cheques deposited together = one consolidated bank
line with no invoice-level detail. Staff separately email scanned cheque
copies (with invoice numbers) to the shared AR mailbox. No fee, no penalty.
Low volume (1–2/month).

**Identification:** bank narration reads *"Cash Letter Pre-Encoded Dep CR"*.
**Bank Reference** is the unique per-transaction ID — **not** Customer
Reference, which is a fixed placeholder (e.g. `0000000001`).

### What's built
Same shape as Scenario 1, mirrored:

| Piece | File | Status |
|---|---|---|
| Row identity rule | `evaluator.py` — `R17`, `CHEQUE_SETTLEMENT_DETECTED`, `needs_distribution` | ✅ Built |
| Detection config (type `cheque_narrative`) | `settlement_identifier.py` / `SettlementIdentifier` table | ✅ Built |
| Management UI | `SettlementIdentifiersCard.tsx` | ✅ Built |
| Row Detail badge | "Cheque Settlement" | ✅ Built |
| Agent recognizes scanned-cheque email | `document_type: "cheque_scan"` | ✅ Built |
| **Split & Map screen** | — | ❌ Not built |

No fee-handling needed for this one (per PRD).

---

## 3. Third-Party / Broker Payments

**Source:** discussed in-chat (no PRD yet) — e.g. Accurant pays on behalf of
its own customers SITA, Kig, Lament.

**Business shape:** a broker/aggregator remits a lump sum covering several
of *its own* customers' invoices. Per your explicit instruction: **no
receipt is auto-created** — the row is just tagged, and a SPOC enters the
per-customer split manually (no email to wait on, unlike Scenario 1/2).

### What's built
| Piece | File | Status |
|---|---|---|
| Provider registry (name + customer roster) | `SettlementIdentifier` table, type `third_party_provider` (`provider_name`, `sub_customers`) | ✅ Built |
| Row identity rule | `evaluator.py` — `R18`, `THIRD_PARTY_PROVIDER_DETECTED`, `needs_distribution` | ✅ Built |
| Management UI | `SettlementIdentifiersCard.tsx` (third column, provider name + comma-separated customer list) | ✅ Built |
| Row Detail badge | "Third-Party Provider" (shows the matched provider name) | ✅ Built |
| **Payment Distribution table** (SPOC enters % or amount per customer, picks invoices per customer, confirms → creates one receipt per customer/invoice) | — | ❌ **Not built** — this is the core still-missing piece for this scenario specifically, since there's no email/agent path to lean on |

**Open decision (asked earlier, deferred):** should the distribution split be
percentage-only, amount-only, or SPOC's choice? Needed before building the
input form.

---

## 4. Short Payments

**Business shape (as it stands today):**

| Shortfall size | What happens |
|---|---|
| Exact match | `R9a`, auto, category `ready_for_oracle` |
| Within auto-tolerance | `R9b`, auto, category **`short_payment`** |
| **Beyond tolerance, unresolved** | `R9c`, lands in `conflict_exception` — same as before, still needs a human |
| **Beyond tolerance, manually mapped by a SPOC** | **`R9d`**, category **`short_payment`** — *(this is the new part)* |
| Overpayment (any amount) | `R11`, `conflict_exception` — **unchanged, never auto-qualifies**, per "until it is not overpaid" |

**Key design decision made:** `short_payment` is its **own bucket**, split
out from `ready_for_oracle` — `ready_for_oracle` now means exact-match
(R9a) only. Both buckets are still one-click-Approve eligible; they're just
counted/displayed separately everywhere (ledger tabs, run-detail KPI cards,
run-list columns, Executive Summary non-posted pills, CSV/PDF exports).

**Duplicate-invoice protection (the second half of this request):**

| Piece | File | What it does |
|---|---|---|
| Ledger table | `app/db/models.py` — `InvoiceApplication` | One row per (line item, invoice), `applied_amount`, status `pending`/`confirmed`/`released` |
| Ledger logic | `app/rule_engine/invoice_ledger.py` | `check_duplicate()` — blocks a new mapping if it would push an invoice's active applications past its outstanding amount. `record_application()` / `confirm_applications()` / `release_applications()` |
| Wired into manual mapping | `hitl/manual_mapping.py` — checked on every preview/confirm, before shortfall classification | ✅ Built |
| Wired into automatic matching | `rule_engine/state_machine.py` — automatic R9a/R9b matches are checked too; a duplicate hit overrides the row to a new rule `R19` / reason `INVOICE_ALREADY_APPLIED` / category `conflict_exception` | ✅ Built |
| Approve → confirm ledger | `hitl/service.py::approve_row` | ✅ Built |
| Reject → release ledger | `hitl/service.py::reject_row` | ✅ Built |

**Answers to your earlier direct questions:**
- *"Will the system recognize a re-mapped invoice as a duplicate?"* — Yes, now. Previously `already_processed_match` was a permanent stub (always `False`) — this is the real implementation.
- *"Move short payment to a different category"* — done; it's `short_payment`, distinct from `ready_for_oracle`.

---

## Full rule-ID reference (new IDs added this conversation)

| Rule ID | Reason code | Category | Meaning |
|---|---|---|---|
| `R16` | `CARD_SETTLEMENT_DETECTED` | `needs_distribution` | Credit card batch identified |
| `R17` | `CHEQUE_SETTLEMENT_DETECTED` | `needs_distribution` | Cheque batch identified |
| `R18` | `THIRD_PARTY_PROVIDER_DETECTED` | `needs_distribution` | Broker payment identified |
| `R9d` | `SHORT_PAYMENT_RECORDED` | `short_payment` | Manually confirmed short payment beyond auto-tolerance |
| `R19` | `INVOICE_ALREADY_APPLIED` | `conflict_exception` | Automatic match overridden — invoice already claimed elsewhere |

## Category/group taxonomy (current, full list)

`unidentified` · `needs_remittance` · `needs_distribution` · `ready_for_oracle`
(exact match only) · `short_payment` (within-tolerance + manually-recorded
beyond-tolerance) · `conflict_exception` · `processed` · `rejected` ·
`post_failed`

---

## What's NOT built yet (the real remaining work)

1. **Split & Map screen** — the actual UI + backend action that takes a
   `needs_distribution` row (any of the three types) and turns it into real
   receipts + invoice mappings, in one confirm click. This is the biggest
   remaining piece across Scenarios 1–3.
2. **3% credit card fee handling** as a recognized, non-shortfall debit line.
3. **Broker distribution input format** decision (% vs amount vs either).
4. **Card breakdown email subject/sender pattern** — needed for reliable
   auto-detection (PRD's own open item).