"""
app.extraction.layer_2a_regex  (UPDATED)
==========================================
Layer 2A — Deterministic Identity Extraction.

CHANGES vs original:
  - Three regex sub-passes now run in order (first decisive hit wins),
    ported from Component 3:
      1a. REF NO pattern        — 'REF NO 111726000049 68' -> '11172600004968'
      1b. OU-prefix scan        — digit sequences starting with the row's own
                                   OU prefix (handles numbers split by spaces
                                   across the narrative, e.g. bank line-wrap)
      1c. Generic patterns      — original broad digit/alpha patterns (fallback)
  - All raw candidates are still cross-referenced against AgingMap; ONLY
    aging-confirmed invoices ever reach `confirmed_invoice_numbers`.
  - Multi-invoice rows are fully supported — every aging-confirmed candidate
    is kept (not just the first).
  - Every decision step (raw candidates, aging hits/misses, classification)
    is logged via debug_logger.dbg() to console + per-run log file.

Team contract (unchanged):
  INPUT:  ChunkPayloadSchema          (from schemas.chunk)
  OUTPUT: Layer2AResultSchema         (from schemas.layer2)
"""
from __future__ import annotations

import re

from ..schemas.chunk import ChunkPayloadSchema, CreditRowSchema
from ..schemas.layer2 import Layer2AResultSchema, Layer2ARow
from ..aging.aging_map import AgingMap
from .ou_prefixes import ou_prefixes_for, describe_ou
from .debug_logger import dbg

# ── Generic fallback patterns (Pass 1c) ───────────────────────────────────────
# Tried only if REF NO and OU-prefix passes found nothing.
GENERIC_INVOICE_PATTERNS = [
    r'\bINV[-]?(\d{4,14})[A-Z]{0,3}\b',
    r'\b[A-Z]{2,4}[-]?(\d{6,14})[A-Z]{0,3}\b',
    r'\b(\d{10,15})[A-Z]{0,3}(?=\s|$|,|;)',
    r'\b(\d{7,9})[A-Z]{0,3}(?=\s|$|,|;)',
]

REF_NO_PATTERN = re.compile(r'REF\s*NO\.?\s*([\d][\d\s]{6,22})', re.IGNORECASE)


# ── Cleaning helpers ──────────────────────────────────────────────────────────

def _clean_narrative(text: str) -> str:
    """Collapse line breaks so invoice numbers split across lines rejoin."""
    text = re.sub(r'[\n\r]+', '', str(text or ""))
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def _normalize_invoice(invoice: str) -> str:
    """Strip INV prefix so number matches plain numeric aging_map keys."""
    return re.sub(r'^INV[-]?', '', invoice, flags=re.IGNORECASE)


def _safe_narrative(row: CreditRowSchema) -> str:
    val = row.narrative
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", ""):
        return ""
    return s


# ── Pass 1a: REF NO pattern ───────────────────────────────────────────────────

def _extract_ref_no(narrative: str, ou_prefixes: set[str], run_id: int, row_ref: str) -> str | None:
    for m in REF_NO_PATTERN.finditer(narrative):
        digits = re.sub(r'\s+', '', m.group(1)).rstrip(';,.')
        dbg(run_id, "2A", row_ref, f"[REF NO] raw='{m.group(1).strip()}' -> collapsed='{digits}'")
        if not digits:
            continue
        for prefix in ou_prefixes:
            if digits.startswith(prefix) and len(digits) >= 10:
                dbg(run_id, "2A", row_ref, f"[REF NO] '{digits}' matches OU prefix '{prefix}' (HIT)")
                return digits
        if len(digits) >= 10:
            dbg(run_id, "2A", row_ref, f"[REF NO] '{digits}' (no OU prefix match, using anyway)")
            return digits
    return None


# ── Pass 1b: OU-prefix scan (handles digit-split invoice numbers) ────────────

def _extract_ou_prefixed(narrative: str, ou_number: str | None, run_id: int, row_ref: str) -> list[str]:
    if not ou_number:
        return []
    found: set[str] = set()

    # Pass A: original narrative
    for m in re.finditer(r'\b(\d{10,16})\b', narrative):
        num = m.group(1)
        if num.startswith(ou_number):
            found.add(num)
            dbg(run_id, "2A", row_ref, f"[OU-prefix PassA] '{num}' starts with OU '{ou_number}' (HIT)")

    # Pass B: collapsed copy — rejoins invoice numbers split by spaces
    # e.g. '1117 2 600003523' -> '11172600003523'. Only collapses spaces
    # between two digits, so 'SHELL 11172...' is left untouched.
    collapsed = re.sub(r'(?<=\d) (?=\d)', '', narrative)
    for m in re.finditer(r'\b(\d{10,16})\b', collapsed):
        num = m.group(1)
        if num.startswith(ou_number) and num not in found:
            dbg(run_id, "2A", row_ref, f"[OU-prefix PassB] '{num}' found after digit-collapse (HIT)")
            found.add(num)

    return sorted(found, key=len, reverse=True)


# ── Pass 1c: generic fallback ──────────────────────────────────────────────────

def _extract_generic(narrative: str, run_id: int, row_ref: str) -> list[str]:
    seen, seen_set = [], set()
    for pattern in GENERIC_INVOICE_PATTERNS:
        for m in re.finditer(pattern, narrative, re.IGNORECASE):
            raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            normalized = _normalize_invoice(raw)
            if normalized not in seen_set:
                dbg(run_id, "2A", row_ref, f"[Generic] pattern hit -> '{normalized}'")
                seen.append(normalized)
                seen_set.add(normalized)
    return seen


def _scan_row_for_invoices(row: CreditRowSchema, run_id: int, row_ref: str) -> list[str]:
    """
    Run all three sub-passes against narrative + bank_reference + customer_reference_number.
    Returns a deduplicated, order-preserved list of RAW candidates (pre-AgingMap filter).
    """
    text_parts = [row.narrative or "", row.bank_reference or "", row.customer_reference_number or ""]
    combined = _clean_narrative(" ".join(p for p in text_parts if p)).upper()

    ou_number = row.ou_number
    ou_prefixes = ou_prefixes_for(ou_number)
    dbg(run_id, "2A", row_ref, f"OU={describe_ou(ou_number)} | combined_text='{combined[:120]}'")

    candidates: list[str] = []
    seen: set[str] = set()

    def _add(inv: str):
        n = _normalize_invoice(inv)
        if n and n not in seen:
            candidates.append(n)
            seen.add(n)

    ref = _extract_ref_no(combined, ou_prefixes, run_id, row_ref)
    if ref:
        _add(ref)

    for inv in _extract_ou_prefixed(combined, ou_number, run_id, row_ref):
        _add(inv)

    for inv in _extract_generic(combined, run_id, row_ref):
        _add(inv)

    dbg(run_id, "2A", row_ref, f"Raw candidates (pre-aging-filter) = {candidates}")
    return candidates


def run_layer_2a(
    chunk: ChunkPayloadSchema,
    aging_map: AgingMap,
    customer_fuzzy_min_pct: float = 40.0,
) -> Layer2AResultSchema:
    """Process one chunk through Layer 2A. Returns found_invoices + no_invoice_found split."""
    found: list[Layer2ARow] = []
    not_found: list[Layer2ARow] = []
    run_id = chunk.run_id

    for row in chunk.rows:
        row_ref = row.bank_reference or f"idx={chunk.rows.index(row)}"

        # ── Step 1: multi-pass regex scan ───────────────────────────────────
        raw_candidates = _scan_row_for_invoices(row, run_id, row_ref)

        # ── Step 2: AgingMap filter — THE source of truth ───────────────────
        confirmed: list[str] = []
        for cand in raw_candidates:
            hit = aging_map.lookup_invoice(cand, row.ou_number)
            if hit is not None:
                confirmed.append(cand)
                dbg(run_id, "2A", row_ref, f"[AgingFilter] '{cand}' CONFIRMED in aging_map")
            else:
                dbg(run_id, "2A", row_ref, f"[AgingFilter] '{cand}' NOT in aging_map — discarded")

        # ── Step 3: customer fuzzy match — OU-scoped when available ──────────
        narrative_text = _safe_narrative(row)
        if hasattr(aging_map, "fuzzy_customer_in_ou") and row.ou_number:
            customer_row, score = aging_map.fuzzy_customer_in_ou(
                narrative_text, row.ou_number, min_pct=customer_fuzzy_min_pct
            )
            scope_note = f"OU-scoped to {row.ou_number}"
        else:
            customer_row, score = aging_map.fuzzy_customer(narrative_text, min_pct=customer_fuzzy_min_pct)
            scope_note = "GLOBAL (fuzzy_customer_in_ou not available — apply aging_map.py update)"
        if customer_row:
            dbg(run_id, "2A", row_ref, f"[FuzzyCustomer:{scope_note}] matched '{customer_row.customer_name}' (score={score})")
        else:
            dbg(run_id, "2A", row_ref, f"[FuzzyCustomer:{scope_note}] no match >= {customer_fuzzy_min_pct}% (best score={score})")

        # ── Step 4: classify method ──────────────────────────────────────────
        has_invoice = bool(confirmed)
        has_customer = customer_row is not None

        if has_invoice and has_customer:
            method = "regex+fuzzy"
        elif has_invoice:
            method = "regex"
        elif has_customer:
            method = "fuzzy"
        else:
            method = "none"

        annotated = Layer2ARow(
            original=row,
            regex_candidate_invoices=raw_candidates,
            confirmed_invoice_numbers=confirmed,        # supports MULTIPLE invoices
            customer_fuzzy_match=customer_row.customer_name if customer_row else None,
            customer_match_pct=score,
            extraction_method=method,
        )

        row_type = "MULTI" if len(confirmed) >= 2 else ("SINGLE" if confirmed else "NONE")
        dbg(run_id, "2A", row_ref,
            f"RESULT method={method} row_type={row_type} confirmed_invoices={confirmed} "
            f"customer={annotated.customer_fuzzy_match}")

        if has_invoice:
            found.append(annotated)
        else:
            not_found.append(annotated)

    dbg(run_id, "2A", "CHUNK",
        f"chunk_index={chunk.chunk_index} -> found_invoices={len(found)}, no_invoice_found={len(not_found)}")

    return Layer2AResultSchema(
        chunk_id=chunk.chunk_id,
        run_id=chunk.run_id,
        chunk_index=chunk.chunk_index,
        total_chunks=chunk.total_chunks,
        found_invoices=found,
        no_invoice_found=not_found,
    )