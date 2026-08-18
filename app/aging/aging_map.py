"""
app.aging.aging_map  (UPDATED)
=================================
AgingMap — the central in-memory lookup structure built once from the
aging_invoices DB table and shared (read-only) across the entire run.

CHANGES vs original:
  - Added `_by_ou` index, built once at construction time alongside the
    existing by_invoice / name_to_rows indexes.
  - Added invoices_for_ou(), customers_for_ou(), fuzzy_customer_in_ou() —
    needed by the updated Layer 2B AI grounding logic.
  - Existing methods (lookup_invoice, fuzzy_customer) are UNCHANGED —
    fuzzy_customer() remains global (all OUs) by design; it now doubles as
    the cross-OU fallback check in Layer 2B, no separate "global" method needed.

  - 2026-08: NEGATIVE rows are no longer discarded. They are diverted into
    a SEPARATE credit pool (CreditMemoView, credit_memos_for()) which the
    matcher cannot reach. See is_payable() for why that separation is the
    whole point, and CreditMemoView for why it's a distinct type rather
    than a flag.

Two pools, deliberately not interchangeable:
  MATCHABLE  — positive rows with a usable document number. What the rule
               engine searches. Reachable by document number.
  CREDIT     — negative rows: credit memos and unapplied receipts. Only
               reachable by customer. Never consulted by the matcher, never
               contributes to target_total.

Lookups:
  O(1) by invoice_number (+ optional OU filter)
  O(1) by OU (new)
  O(1) fuzzy by customer_name via rapidfuzz, global or OU-restricted
  O(1) credits by customer_number or customer_name
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz, process


@dataclass(frozen=True)
class AgingInvoiceView:
    """
    Read-only snapshot of one AgingInvoice row.
    This is the only aging type that crosses layer boundaries —
    never pass raw SQLAlchemy AgingInvoice objects downstream.
    """
    invoice_number: str
    customer_number: str
    customer_name: str
    invoice_type: str
    invoice_amount: float
    outstanding_amount: float
    invoice_currency: str
    ou_number: str
    invoice_description: str = ""
    invoice_date: str = ""


KIND_CREDIT_MEMO = "credit_memo"
KIND_UNAPPLIED_RECEIPT = "unapplied_receipt"


@dataclass(frozen=True)
class CreditMemoView:
    """
    Read-only snapshot of one NEGATIVE aging row — money sitting on the
    customer's side rather than ours.

    DELIBERATELY A SEPARATE TYPE from AgingInvoiceView, not a flag on it.
    The bug this whole pool is designed around (see is_payable() below) was
    caused by a credit memo being indistinguishable from an invoice once it
    reached the matcher. Giving the two pools different types means a credit
    memo cannot be returned anywhere an AgingInvoiceView is expected — the
    separation is enforced by the type, not by remembering to check a flag.

    `amount` is the POSITIVE magnitude of credit available. The source row
    is negative; every consumer wants to compare it against a shortfall, so
    the sign is flipped once here rather than in each caller.
    """
    document_number: str
    customer_number: str
    customer_name: str
    ou_number: str
    currency: str
    amount: float                 # positive magnitude; source row is negative
    kind: str                     # KIND_CREDIT_MEMO | KIND_UNAPPLIED_RECEIPT
    description: str = ""
    document_date: str = ""
    document_type: str = ""       # raw INV TYPE, for display/debug only


def classify_negative(invoice_description: str) -> str:
    """
    Which of the two kinds of negative row this is.

    Finance's rule, confirmed against the 31-Mar-2026 export: a negative row
    is either an unapplied receipt or a credit memo, nothing else, and the
    INV DESC column separates them exactly — populated on all 392 credit
    memos, blank on all 391 unapplied receipts.

    Note the deliberate absence of any INV TYPE string matching. The type
    column carries values like "211-Contract Inv", "Customer CM",
    "InterCoCM", "Conversion" and "payment", and an earlier design tried to
    classify on those. It isn't needed: the sign alone proves the row is a
    credit, and the description alone proves which kind. No label parsing,
    so no "is DM a CM?" class of bug.
    """
    return KIND_CREDIT_MEMO if (invoice_description or "").strip() else KIND_UNAPPLIED_RECEIPT


def is_payable(outstanding_amount: float) -> bool:
    """
    A document only belongs in the matchable pool if the customer actually
    owes us something on it.

    A real aging export is not just open invoices. Roughly 10% of the rows in
    a production export carry a NEGATIVE outstanding balance — credit memos,
    unapplied receipts sitting on the customer's account, credit notes keyed
    on the invoice form, and legacy conversion balances. The majority of them
    carry a perfectly normal 14-digit Oracle document number, so lookup_invoice
    could not tell them apart from an invoice and returned them like any other
    candidate. Two things went wrong when one got matched:

      1. It DRAGGED target_total DOWN, so a correct payment read as an
         overpayment (evaluator.py's R11) — a pure false positive.
      2. Worse, when the matched set happened to NET TO ZERO, evaluator.py
         read shortfall_pct as 0.0 and returned R9a EXACT_MATCH — a *perfect
         match* with a live Approve button, which would have posted a
         zero/negative ReferenceAmount to Oracle.

    Zero is excluded for the same reason: a fully-settled row still present in
    the export contributes nothing and can only distort a total.

    THIS FUNCTION IS UNCHANGED and must stay that way. As of 2026-08 the rows
    it rejects for being negative are no longer discarded — they are diverted
    into the credit-memo pool (see CreditMemoView and credit_memos_for()).
    That pool is a separate index of a separate type, reachable only by
    customer, never by document number, and never consulted by the matcher.
    The guarantee this function provides — nothing negative can ever reach
    target_total by accident — is exactly as strong as it was before.
    """
    return outstanding_amount > 0


def is_usable_invoice_number(invoice_number: str) -> bool:
    """
    Reject document numbers that cannot identify anything.

    A production export contains free text in the invoice-number column —
    "Scrap Sale", a customer's own name, hand-typed collection references, and
    the bare value "1" repeated across several rows. lookup_invoice() resolves
    a duplicated key by silently returning candidates[0], so a junk key does
    not fail loudly, it quietly matches the wrong row.

    Real Oracle document numbers always contain at least one digit and are
    never one or two characters long, so this is deliberately conservative:
    it drops only what could never have been matched correctly anyway.
    """
    s = (invoice_number or "").strip()
    return len(s) >= 3 and any(ch.isdigit() for ch in s)


class AgingMap:
    """
    Built once per run from the aging_invoices table.
    All lookups are pure reads — thread-safe for parallel chunk processing.

    Only PAYABLE rows with a USABLE document number are indexed into the
    matchable pool — see is_payable() and is_usable_invoice_number() above
    for why. Everything dropped is counted in `build_report` so the
    exclusion is visible rather than silent.

    Negative rows additionally populate the credit pool (credit_memos_for()).
    That is an ADDITIONAL index, not a relaxation: the matchable pool's
    contents are byte-identical to what they were before the pool existed.
    """

    def __init__(
        self,
        by_invoice: dict[str, list[AgingInvoiceView]],
        customer_names: list[str],
        name_to_rows: dict[str, list[AgingInvoiceView]],
        by_ou: dict[str, list[AgingInvoiceView]],
        by_customer_number: dict[str, list[AgingInvoiceView]] | None = None,
        build_report: dict | None = None,
        credits_by_customer_number: dict[str, list[CreditMemoView]] | None = None,
        credits_by_customer_name: dict[str, list[CreditMemoView]] | None = None,
    ):
        self._by_invoice = by_invoice
        self._customer_names = customer_names
        self._name_to_rows = name_to_rows
        self._by_ou = by_ou
        self._by_customer_number = by_customer_number or {}
        self.build_report = build_report or {}
        # ── The credit pool. Two indexes because the two callers hold
        # different identifiers: the rule engine has customer_number (off
        # MatchedInvoice), the mapping picker has only customer_name. Both
        # point at the SAME CreditMemoView objects — frozen dataclasses, so
        # sharing them between indexes is safe.
        #
        # Note what is deliberately absent: there is no by-document-number
        # index. Nothing can look a credit memo up the way lookup_invoice()
        # looks an invoice up, which is what keeps it out of the matcher.
        self._credits_by_customer_number = credits_by_customer_number or {}
        self._credits_by_customer_name = credits_by_customer_name or {}

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def build(cls, aging_rows: list) -> "AgingMap":
        """
        Build from a list of SQLAlchemy AgingInvoice ORM objects.
        Called once in the orchestrator before chunk dispatch.
        """
        by_invoice: dict[str, list[AgingInvoiceView]] = {}
        name_to_rows: dict[str, list[AgingInvoiceView]] = {}
        by_ou: dict[str, list[AgingInvoiceView]] = {}
        by_customer_number: dict[str, list[AgingInvoiceView]] = {}
        credits_by_customer_number: dict[str, list[CreditMemoView]] = {}
        credits_by_customer_name: dict[str, list[CreditMemoView]] = {}

        dropped_unpayable: list[dict] = []
        dropped_malformed: list[dict] = []
        credit_memo_count = 0
        unapplied_receipt_count = 0

        for r in aging_rows:
            view = AgingInvoiceView(
                invoice_number=str(r.invoice_number).strip(),
                customer_number=str(r.customer_number or ""),
                customer_name=str(r.customer_name or "").strip(),
                invoice_type=str(r.invoice_type or ""),
                invoice_amount=float(r.invoice_amount or 0),
                outstanding_amount=float(r.outstanding_amount or 0),
                invoice_currency=str(r.invoice_currency or ""),
                ou_number=str(r.ou_number or ""),
                invoice_description=str(getattr(r, "invoice_description", "") or "").strip(),
                invoice_date=str(getattr(r, "invoice_date", "") or "").strip(),
            )

            # ── Credit pool intake, BEFORE the malformed-number filter ────
            #
            # Order matters. is_usable_invoice_number() runs first for the
            # matchable pool and would reject these on their own: real
            # unapplied receipts are labelled with free text like
            # "Coll SKY-30-Mar-26", not an Oracle document number. Finance
            # needs them visible on the mapping card, so the credit pool
            # skips that check.
            #
            # That is safe HERE and only here, because the reason the check
            # exists — a junk key silently matching the wrong row through
            # lookup_invoice() — cannot happen to a credit-pool row. Nothing
            # looks these up by number; they are only ever listed by
            # customer. The check stays fully in force for the matchable
            # pool a few lines below.
            if view.outstanding_amount < 0:
                kind = classify_negative(view.invoice_description)
                credit = CreditMemoView(
                    document_number=view.invoice_number,
                    customer_number=view.customer_number,
                    customer_name=view.customer_name,
                    ou_number=view.ou_number,
                    currency=view.invoice_currency.upper().strip(),
                    amount=abs(view.outstanding_amount),
                    kind=kind,
                    description=view.invoice_description,
                    document_date=view.invoice_date,
                    document_type=view.invoice_type,
                )
                if kind == KIND_CREDIT_MEMO:
                    credit_memo_count += 1
                else:
                    unapplied_receipt_count += 1
                if credit.customer_number:
                    credits_by_customer_number.setdefault(credit.customer_number, []).append(credit)
                if credit.customer_name:
                    credits_by_customer_name.setdefault(credit.customer_name.upper(), []).append(credit)
                # Falls through to is_payable() below, which rejects it from
                # the matchable pool exactly as it always has.

            if not is_usable_invoice_number(view.invoice_number):
                dropped_malformed.append({
                    "invoice_number": view.invoice_number,
                    "customer_name": view.customer_name,
                    "invoice_type": view.invoice_type,
                })
                continue

            if not is_payable(view.outstanding_amount):
                dropped_unpayable.append({
                    "invoice_number": view.invoice_number,
                    "customer_name": view.customer_name,
                    "invoice_type": view.invoice_type,
                    "outstanding_amount": view.outstanding_amount,
                    "invoice_currency": view.invoice_currency,
                })
                continue

            key = view.invoice_number.upper()
            by_invoice.setdefault(key, []).append(view)

            name_key = view.customer_name.upper()
            name_to_rows.setdefault(name_key, []).append(view)

            by_ou.setdefault(view.ou_number, []).append(view)

            if view.customer_number:
                by_customer_number.setdefault(view.customer_number, []).append(view)

        # A document number legitimately repeats across OUs, so a duplicate key
        # is not by itself an error — lookup_invoice() disambiguates on
        # ou_number. It IS worth reporting, because a duplicate with no OU
        # preference available still falls back to candidates[0].
        duplicate_keys = sorted(k for k, v in by_invoice.items() if len(v) > 1)

        build_report = {
            "kept": sum(len(v) for v in by_invoice.values()),
            "dropped_unpayable_count": len(dropped_unpayable),
            "dropped_malformed_count": len(dropped_malformed),
            "duplicate_invoice_number_count": len(duplicate_keys),
            # Capped samples — this report is persisted in the aging snapshot
            # and returned by the refresh endpoint, so it must not grow with
            # the size of the export.
            "dropped_unpayable_sample": dropped_unpayable[:50],
            "dropped_malformed_sample": dropped_malformed[:50],
            "duplicate_invoice_numbers_sample": duplicate_keys[:50],
            # The negatives are no longer merely "dropped" — they're retained
            # in the credit pool. Reported so a refresh shows the split
            # rather than leaving it to be inferred from dropped_unpayable.
            "credit_memo_count": credit_memo_count,
            "unapplied_receipt_count": unapplied_receipt_count,
        }

        return cls(
            by_invoice=by_invoice,
            customer_names=list(name_to_rows.keys()),
            name_to_rows=name_to_rows,
            by_ou=by_ou,
            by_customer_number=by_customer_number,
            build_report=build_report,
            credits_by_customer_number=credits_by_customer_number,
            credits_by_customer_name=credits_by_customer_name,
        )

    # ── Invoice lookup — O(1) ─────────────────────────────────────────────────

    def lookup_invoice(
        self, invoice_number: str, ou_number: str | None = None
    ) -> Optional[AgingInvoiceView]:
        """
        Exact lookup by invoice_number. Optionally filter by ou_number.
        Falls back to stripping trailing letter suffixes (e.g. "...2343AP").
        Returns None if not found.
        """
        if not invoice_number:
            return None

        key = invoice_number.strip().upper()
        candidates = self._by_invoice.get(key)

        if not candidates:
            # Try stripping trailing letter suffix (SAP ref artefact)
            stripped = re.sub(r"[A-Z]+$", "", key)
            candidates = self._by_invoice.get(stripped)

        if not candidates:
            return None

        if ou_number:
            for c in candidates:
                if c.ou_number == ou_number:
                    return c

        return candidates[0]

    # ── Customer fuzzy lookup — GLOBAL (all OUs) ──────────────────────────────

    def fuzzy_customer(
        self, text: str, min_pct: float = 40.0
    ) -> tuple[Optional[AgingInvoiceView], float]:
        """
        Fuzzy match raw narrative text against ALL known customer names,
        across every OU. Returns (best_match_row, score) or (None, score)
        below threshold.

        NOTE: this is intentionally unscoped — it doubles as the cross-OU
        fallback check used by Layer 2B when the AI flags a customer as NOT
        present in the OU-restricted candidate list it was given.
        """
        if not text or not self._customer_names:
            return None, 0.0

        match = process.extractOne(
            text.upper(), self._customer_names, scorer=fuzz.token_sort_ratio
        )
        if not match:
            return None, 0.0

        name, score, _ = match
        if score < min_pct:
            return None, score

        rows = self._name_to_rows.get(name, [])
        return (rows[0] if rows else None), score

    # ── Customer fuzzy lookup — OU-RESTRICTED (NEW) ──────────────────────────

    def fuzzy_customer_in_ou(
        self, text: str, ou_number: str, min_pct: float = 40.0
    ) -> tuple[Optional[AgingInvoiceView], float]:
        """
        Same as fuzzy_customer(), but the candidate pool is restricted to
        customer names that actually have at least one invoice in this OU.
        Use this for the PRIMARY in-OU check in Layer 2B grounding.
        """
        rows = self._by_ou.get(ou_number, [])
        names_in_ou = sorted({v.customer_name.upper() for v in rows if v.customer_name})
        if not text or not names_in_ou:
            return None, 0.0

        match = process.extractOne(text.upper(), names_in_ou, scorer=fuzz.token_sort_ratio)
        if not match:
            return None, 0.0

        name, score, _ = match
        if score < min_pct:
            return None, score

        candidates = self._name_to_rows.get(name, [])
        # Prefer the row actually in this OU (name might also exist elsewhere)
        for c in candidates:
            if c.ou_number == ou_number:
                return c, score
        return (candidates[0] if candidates else None), score

    # ── OU-scoped bulk accessors (NEW — for Layer 2B AI grounding) ──────────

    def invoices_for_ou(self, ou_number: str, limit: int = 25) -> list[dict]:
        """
        Open invoices for one OU, shaped for AI prompt grounding:
          [{"invoice_number": ..., "amount": ..., "customer_name": ...}, ...]
        Sorted by outstanding_amount descending.
        """
        rows = self._by_ou.get(ou_number, [])
        rows_sorted = sorted(rows, key=lambda v: v.outstanding_amount, reverse=True)
        return [
            {
                "invoice_number": v.invoice_number,
                "amount": v.outstanding_amount,
                "customer_name": v.customer_name,
            }
            for v in rows_sorted[:limit]
        ]

    def customers_for_ou(self, ou_number: str, limit: int = 25) -> list[str]:
        """Unique display-case customer names with at least one invoice in this OU."""
        rows = self._by_ou.get(ou_number, [])
        seen: dict[str, None] = {}
        for v in rows:
            if v.customer_name and v.customer_name not in seen:
                seen[v.customer_name] = None
        return list(seen.keys())[:limit]

    def invoices_for_customer(self, customer_name: str) -> list[AgingInvoiceView]:
        """All open invoices for an exact customer name (case-insensitive).
        Used by hitl/manual_mapping.py's SPOC invoice picker — once a
        customer is known (either auto-identified or manually searched),
        this is every invoice+outstanding_amount the picker offers."""
        if not customer_name:
            return []
        return self._name_to_rows.get(customer_name.strip().upper(), [])

    def invoices_for_customer_number(
        self, customer_number: str, exclude_ou: str | None = None
    ) -> list[AgingInvoiceView]:
        """
        All open invoices for one customer ACCOUNT, optionally excluding a
        single OU.

        Used by rule_engine/overpayment_reason.py's cross-OU check. The aging
        export spans every OU in one file, so when a payment lands in one OU's
        bank account but the customer also has open invoices in another OU
        (BRD Scenario 13), the evidence for that is already in memory — this
        is the accessor that reaches it.

        Keyed on customer_number rather than name because the same account can
        appear under slightly different display names across OUs.
        """
        if not customer_number:
            return []
        rows = self._by_customer_number.get(str(customer_number), [])
        if exclude_ou is None:
            return list(rows)
        return [v for v in rows if v.ou_number != exclude_ou]

    # ── Credit pool accessors ────────────────────────────────────────────────

    def credit_memos_for(
        self,
        customer_number: str | None = None,
        customer_name: str | None = None,
        ou_number: str | None = None,
        currency: str | None = None,
        kind: str | None = KIND_CREDIT_MEMO,
    ) -> list[CreditMemoView]:
        """
        Negative aging rows belonging to one customer.

        Identify the customer by NUMBER where you have it (the rule engine
        does, off MatchedInvoice) and by NAME otherwise (the mapping picker
        only has a name). Passing both prefers the number, which is the more
        reliable key — the same account appears under slightly different
        display names across OUs.

        Defaults to CREDIT MEMOS ONLY, because that is what Finance netts.
        An unapplied receipt is real money on the customer's account, but
        nobody knows when the customer will come back to it, so it must
        never drive an automatic decision. Pass kind=None to get both (the
        mapping card does, to show them side by side) or
        kind=KIND_UNAPPLIED_RECEIPT for just those.

        ou_number and currency are near-mandatory in practice. Of the 164
        credit memos in the 31-Mar export that name a specific invoice, all
        164 agree with that invoice on customer, OU AND currency — there is
        no counter-example in the data — and BRD Scenario 13 has money
        landing in the wrong OU parked in GL 23213 rather than applied
        across OUs. They are optional here only so a caller can deliberately
        ask a wider question.

        Sorted by amount descending: the mapping card lists largest first,
        and a caller looking for an exact match doesn't care about order.
        """
        rows: list[CreditMemoView] = []
        if customer_number:
            rows = self._credits_by_customer_number.get(str(customer_number), [])
        elif customer_name:
            rows = self._credits_by_customer_name.get(customer_name.strip().upper(), [])

        if not rows:
            return []

        if kind is not None:
            rows = [c for c in rows if c.kind == kind]
        if ou_number:
            rows = [c for c in rows if c.ou_number == ou_number]
        if currency:
            want = currency.upper().strip()
            rows = [c for c in rows if c.currency == want]

        return sorted(rows, key=lambda c: c.amount, reverse=True)

    def has_credit_memos(
        self, customer_number: str | None = None, customer_name: str | None = None,
        ou_number: str | None = None, currency: str | None = None,
    ) -> bool:
        """
        Does this customer hold any open CREDIT MEMO in this OU/currency?

        The question rule_engine/evaluator.py asks before it lets a short
        payment through on tolerance. Deliberately narrower than "has any
        negative row" — unapplied receipts do not count, or a customer like
        Sky.com (which carries unapplied receipts as a matter of course)
        would be pulled into review on every short payment.
        """
        return bool(self.credit_memos_for(
            customer_number=customer_number, customer_name=customer_name,
            ou_number=ou_number, currency=currency, kind=KIND_CREDIT_MEMO,
        ))

    @property
    def credit_memo_count(self) -> int:
        """Total credit memos retained across all customers (not unapplied receipts)."""
        return sum(
            1
            for rows in self._credits_by_customer_number.values()
            for c in rows
            if c.kind == KIND_CREDIT_MEMO
        )

    # ── Utility ───────────────────────────────────────────────────────────────

    @property
    def invoice_count(self) -> int:
        return sum(len(v) for v in self._by_invoice.values())

    @property
    def customer_count(self) -> int:
        return len(self._name_to_rows)

    def all_customer_names(self, limit: int = 5000) -> list[str]:
        """Every unique display-case customer name across the WHOLE aging
        report, any OU — unlike customers_for_ou() above, not scoped to one
        row's OU. Used by the Accounts & OU's page's Third-Party Provider
        picker (see bff/settlement_identifier_routes.py): a provider like
        Accurant isn't tied to a single OU the way one bank row's manual
        mapping is, so the picker needs the global list, sorted for a
        search box rather than ranked by outstanding amount."""
        seen: dict[str, str] = {}
        for rows in self._name_to_rows.values():
            for v in rows:
                if v.customer_name and v.customer_name.upper() not in seen:
                    seen[v.customer_name.upper()] = v.customer_name
        return sorted(seen.values())[:limit]

    @property
    def ou_numbers(self) -> list[str]:
        """All distinct OU numbers present in the aging data."""
        return list(self._by_ou.keys())