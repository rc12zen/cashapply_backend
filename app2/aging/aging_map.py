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

Lookups:
  O(1) by invoice_number (+ optional OU filter)
  O(1) by OU (new)
  O(1) fuzzy by customer_name via rapidfuzz, global or OU-restricted
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


class AgingMap:
    """
    Built once per run from the aging_invoices table.
    All lookups are pure reads — thread-safe for parallel chunk processing.
    """

    def __init__(
        self,
        by_invoice: dict[str, list[AgingInvoiceView]],
        customer_names: list[str],
        name_to_rows: dict[str, list[AgingInvoiceView]],
        by_ou: dict[str, list[AgingInvoiceView]],
    ):
        self._by_invoice = by_invoice
        self._customer_names = customer_names
        self._name_to_rows = name_to_rows
        self._by_ou = by_ou

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
            )
            key = view.invoice_number.upper()
            by_invoice.setdefault(key, []).append(view)

            name_key = view.customer_name.upper()
            name_to_rows.setdefault(name_key, []).append(view)

            by_ou.setdefault(view.ou_number, []).append(view)

        return cls(
            by_invoice=by_invoice,
            customer_names=list(name_to_rows.keys()),
            name_to_rows=name_to_rows,
            by_ou=by_ou,
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

    # ── Utility ───────────────────────────────────────────────────────────────

    @property
    def invoice_count(self) -> int:
        return sum(len(v) for v in self._by_invoice.values())

    @property
    def customer_count(self) -> int:
        return len(self._name_to_rows)

    @property
    def ou_numbers(self) -> list[str]:
        """All distinct OU numbers present in the aging data."""
        return list(self._by_ou.keys())