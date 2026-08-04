"""
app.extraction.layer_2b_ai  (PATCHED v3 -- TRUE PROMPT BATCHING)
==================================================================
Layer 2B -- AI Agent Fallback, now GROUNDED in real AgingMap data AND
BATCHED to cut network calls.

PATCH NOTES (this revision -- batching, on top of the previous v2 patch):

  PROBLEM: v2 made ONE Claude API call PER UNRESOLVED ROW, sequentially,
  inside each chunk's thread. With e.g. 40 rows unresolved by regex in a
  200-row run, that's 40 blocking network round-trips.

  FIX: Rows are now grouped by OU number (grounding -- the invoice/customer
  candidate lists -- is identical for every row in the same OU), then split
  into batches of BATCH_SIZE rows. ONE Claude call is made per batch, with
  the model returning a JSON ARRAY (one object per row, tagged by
  row_index) instead of a single object. This cuts call count by roughly
  BATCH_SIZE-x for OUs with many unresolved rows.

  SAFETY NET: if a batch call fails outright (network error) or the model
  returns something that isn't a parseable JSON array, that ONE batch
  (not the whole chunk/run) falls back to the old one-call-per-row path
  (`_call_claude_single`) so a single bad batch can't blank out results
  for rows that would otherwise have succeeded individually.

  Everything else -- OU-aware invoice-shape rules, customer-name rules,
  cross-OU escape hatch, AgingMap validation, full debug trail -- is
  UNCHANGED, just now applied per-item inside a batch response instead of
  a single response.

Team contract (unchanged):
  INPUT:  Layer2AResultSchema          (from schemas.layer2)
  OUTPUT: Layer2BResultSchema          (internal, consumed by merger only)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..schemas.layer2 import Layer2AResultSchema, Layer2ARow
from ..schemas.extraction import IdentifiedPayment, UnknownPayment
from ..aging.aging_map import AgingMap
from .ou_prefixes import describe_ou
from .debug_logger import dbg, dbg_block
from .ai_providers import call_ai

logger = logging.getLogger(__name__)

MAX_CANDIDATE_INVOICES_SHOWN = 25   # cap tokens -- don't dump entire OU ledger
MAX_CANDIDATE_CUSTOMERS_SHOWN = 25
GLOBAL_FALLBACK_MIN_PCT = 60.0      # stricter than the in-OU 40% -- cross-OU match needs more confidence

# -- Batching knobs -------------------------------------------------------------
# BATCH_SIZE and AI_BATCH_MAX_CONCURRENCY are configurable via .env
# (Settings.AI_BATCH_SIZE / Settings.AI_BATCH_MAX_CONCURRENCY) — see
# _batch_settings() below. Token limits stay fixed: they're sized to the
# response schema, not something that should vary run to run.
BATCH_SIZE = 10                    # fallback if Settings import fails
AI_BATCH_MAX_CONCURRENCY = 4       # fallback if Settings import fails
BATCH_MAX_TOKENS_PER_ROW = 120     # fixed — not configurable
BATCH_MAX_TOKENS_FLOOR = 400       # fixed — not configurable


def _batch_settings() -> tuple[int, int]:
    """Returns (batch_size, max_concurrency) from .env, with fallback."""
    try:
        from ..db.settings import get_settings
        s = get_settings()
        return s.AI_BATCH_SIZE, s.AI_BATCH_MAX_CONCURRENCY
    except Exception:
        return BATCH_SIZE, AI_BATCH_MAX_CONCURRENCY

# Matches a leading ```json / ``` fence and a trailing ``` fence, including
# any surrounding whitespace/newlines. Applied defensively even though the
# system prompt asks the model not to use fences -- empirically it does anyway.
_FENCE_LEADING_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_TRAILING_RE = re.compile(r"\s*```\s*$")


def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing ```json or ``` fences the model adds despite instructions not to."""
    cleaned = _FENCE_LEADING_RE.sub("", text.strip())
    cleaned = _FENCE_TRAILING_RE.sub("", cleaned)
    return cleaned.strip()


# -- System prompts --------------------------------------------------------------
# Shared rules for both the batch path and the per-row fallback path.

_SHARED_RULES = """\
You are a financial data extraction assistant for an AR cash reconciliation system.
Your job: read bank statement narrative lines and identify which customer paid,
and which invoice number(s) each payment is for.

You will be given GROUNDING LISTS for this OU (business unit): a list of OPEN
INVOICES (number, amount, customer) and a list of CUSTOMER NAMES, both already
filtered to this OU. PREFER matching against these lists over inventing a new
name/number -- they are real data from our ERP aging report.

INVOICE NUMBER SHAPE (important):
  Every real invoice number starts with the 3-digit OU prefix, e.g. an
  invoice in OU 111 (Pune) always starts with '111', e.g. '11172600002343'.
  It may have 0-3 trailing letters, e.g. '11172600002343AP'.
  Bank systems sometimes SPLIT the number with stray spaces across the
  narrative, e.g. '1117 2 600003523' really means '11172600003523' -- treat
  consecutive digit groups separated only by spaces as one number.

CUSTOMER NAME SHAPE (important):
  Customer names in narratives commonly appear as:
    - Clean legal form:      "XYZ TRADERS PVT LTD"
    - Bank-truncated form:   "XYZ TRD PVT LT"
    - Embedded in noise:     "NEFT-N123456-XYZ TRADERS-PAYMENT"
  Do NOT extract a person's name (e.g. "MR RAJESH KUMAR") as customer_name --
  we want the paying COMPANY, not an individual signatory.
  Some narratives carry ONLY transaction codes and no name at all -- in that
  case return customer_name: null. Do not force a guess.

MULTIPLE INVOICES:
  A single payment can cover MULTIPLE invoices. Return every invoice number
  you can identify as a JSON array, not just the first one.

IF THE CUSTOMER YOU IDENTIFY IS NOT IN THE PROVIDED LIST:
  You may still report it -- set "customer_in_ou_candidates": false. We will
  separately re-check it against the full database (all OUs) before relying
  on it. This is expected for genuine inter-company / cross-OU payments.
"""

_SYSTEM_PROMPT_BATCH = _SHARED_RULES + """
You will be given MULTIPLE transactions in one request, each tagged with a
"row_index". Process each transaction independently -- do not let one
transaction's narrative influence another's answer.

Return ONLY a valid JSON array -- no explanation, no markdown fences, one
object per transaction you were given, tagged with its row_index so answers
can be matched back to the correct row regardless of order. Schema:
[
  {
    "row_index": <int>,
    "customer_name": "<string or null>",
    "customer_in_ou_candidates": <true/false>,
    "invoice_numbers": ["<string>", ...],
    "confidence": <float 0.0-1.0>,
    "reasoning": "<one sentence>"
  },
  ...
]
Return exactly one object per row_index you were given -- never merge two
transactions into one object, never drop a row_index.
"""

_SYSTEM_PROMPT_SINGLE = _SHARED_RULES + """
Return ONLY valid JSON -- no explanation, no markdown fences. Schema:
{
  "customer_name": "<string or null>",
  "customer_in_ou_candidates": <true/false>,
  "invoice_numbers": ["<string>", ...],
  "confidence": <float 0.0-1.0>,
  "reasoning": "<one sentence>"
}
"""

_GROUNDING_TEMPLATE = """\
Transaction context (shared by every row below):
- Business unit / OU: {ou_description}

GROUNDING -- open invoices for this OU (sample, up to {max_inv} shown):
{invoice_candidates}

GROUNDING -- known customer names for this OU (sample, up to {max_cust} shown):
{customer_candidates}
"""

_BATCH_ROW_TEMPLATE = """\
--- row_index: {row_index} ---
Bank statement narrative:
\"\"\"{narrative}\"\"\"
Bank: {bank_name}
Business unit code: {business_unit}
Credit amount: {credit_amount} {currency}
Bank reference: {bank_reference}
Customer reference number: {customer_reference_number}
"""

_SINGLE_USER_TEMPLATE = _GROUNDING_TEMPLATE + """
Bank statement narrative:
\"\"\"{narrative}\"\"\"

Transaction context:
- Bank: {bank_name}
- Business unit code: {business_unit}
- Credit amount: {credit_amount} {currency}
- Bank reference: {bank_reference}
- Customer reference number: {customer_reference_number}
"""


@dataclass
class Layer2BResult:
    """Internal result type -- consumed only by merger.py."""
    chunk_id: str
    run_id: int
    chunk_index: int
    total_chunks: int
    identified: list[IdentifiedPayment] = field(default_factory=list)
    unknown: list[UnknownPayment] = field(default_factory=list)


# -- Grounding builder ------------------------------------------------------------

def _build_ou_grounding(aging_map: AgingMap, ou_number: str | None, run_id: int, row_ref: str):
    """
    Pull OU-scoped invoice + customer candidates from AgingMap for prompt grounding.
    Called ONCE per OU group (not once per row) since every row sharing an OU
    gets the identical candidate lists.
    """
    dbg(run_id, "2B", row_ref,
        f"DEBUG ou_number_passed={ou_number!r} (type={type(ou_number).__name__}) "
        f"known_ou_keys_in_aging_map={sorted(aging_map.ou_numbers)}")

    if not hasattr(aging_map, "invoices_for_ou") or not hasattr(aging_map, "customers_for_ou"):
        dbg(run_id, "2B", row_ref,
            "WARNING: AgingMap missing invoices_for_ou()/customers_for_ou() -- "
            "running WITHOUT grounding. Apply the aging_map.py update.")
        return "  (none available)", "  (none available)", []

    invoices = aging_map.invoices_for_ou(ou_number, limit=MAX_CANDIDATE_INVOICES_SHOWN) if ou_number else []
    customers = aging_map.customers_for_ou(ou_number, limit=MAX_CANDIDATE_CUSTOMERS_SHOWN) if ou_number else []

    dbg(run_id, "2B", row_ref,
        f"Grounding built: {len(invoices)} invoice candidates, {len(customers)} customer candidates "
        f"for {describe_ou(ou_number)}")

    inv_lines = "\n".join(
        f"  - {i.get('invoice_number')} | amount={i.get('amount')} | customer={i.get('customer_name')}"
        for i in invoices
    ) or "  (none available)"
    cust_lines = "\n".join(f"  - {c}" for c in customers) or "  (none available)"

    return inv_lines, cust_lines, customers


_client_singleton = None  # unused, kept only so any external import of this
                           # name doesn't break -- provider clients now live
                           # in extraction/ai_providers.py


# -- AI call -- BATCH path ---------------------------------------------------------

def _call_claude_batch(
    rows_batch: list[Layer2ARow],
    ou_number: str | None,
    invoice_candidates: str,
    customer_candidates: str,
    run_id: int,
    batch_ref: str,
) -> tuple[Optional[list[dict]], str]:
    """
    Call the configured AI provider (see extraction/ai_providers.py --
    Settings.AI_PROVIDER, "anthropic" or "openai") ONCE for a batch of rows
    sharing the same OU. Name kept as `_call_claude_batch` for a minimal
    diff even though this is no longer Claude-specific -- the function's
    job (build the batch prompt, parse the JSON array response, record
    usage) is identical regardless of which provider actually answers.

    Returns (parsed_json_array, raw_text). parsed_json_array is None if the
    call failed outright OR the response could not be parsed as a JSON
    array -- callers should fall back to per-row processing for this batch
    in that case.
    """
    raw = ""
    try:
        grounding = _GROUNDING_TEMPLATE.format(
            ou_description=describe_ou(ou_number),
            max_inv=MAX_CANDIDATE_INVOICES_SHOWN,
            max_cust=MAX_CANDIDATE_CUSTOMERS_SHOWN,
            invoice_candidates=invoice_candidates,
            customer_candidates=customer_candidates,
        )

        row_blocks = []
        for idx, row in enumerate(rows_batch):
            orig = row.original
            row_blocks.append(_BATCH_ROW_TEMPLATE.format(
                row_index=idx,
                narrative=orig.narrative or "(empty)",
                bank_name=orig.bank_name or "",
                business_unit=orig.business_unit or "",
                credit_amount=orig.credit_amount,
                currency=orig.currency or "",
                bank_reference=orig.bank_reference or "(none)",
                customer_reference_number=orig.customer_reference_number or "(none)",
            ))

        user_msg = grounding + "\n" + "\n".join(row_blocks)
        max_tokens = max(BATCH_MAX_TOKENS_FLOOR, BATCH_MAX_TOKENS_PER_ROW * len(rows_batch))

        dbg_block(run_id, "2B", batch_ref, "BATCH PROMPT SENT",
                   [_SYSTEM_PROMPT_BATCH, "---", user_msg])

        result = call_ai(_SYSTEM_PROMPT_BATCH, user_msg, max_tokens)

        from ..ai_usage.tracker import record_usage
        record_usage(
            run_id=run_id, call_type="batch", batch_ref=batch_ref,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            latency_ms=result.latency_ms, succeeded=True,
            model=result.model, provider=result.provider,
        )

        raw = result.text
        dbg_block(run_id, "2B", batch_ref, "BATCH RAW RESPONSE", [raw])

        cleaned = _strip_markdown_fences(raw)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as parse_exc:
            dbg(run_id, "2B", batch_ref,
                f"BATCH JSON parse FAILED: {parse_exc} | cleaned_text='{cleaned[:300]}'")
            return None, raw

        if not isinstance(parsed, list):
            dbg(run_id, "2B", batch_ref,
                f"BATCH response was valid JSON but not a list (got {type(parsed).__name__}) -- treating as failed batch")
            return None, raw

        return parsed, raw

    except Exception as exc:
        dbg(run_id, "2B", batch_ref, f"BATCH AI call FAILED: {exc}")
        logger.warning("Layer 2B batch AI call failed: %s", exc)
        try:
            from ..ai_usage.tracker import record_usage
            from ..db.settings import get_settings
            s = get_settings()
            provider = (s.AI_PROVIDER or "anthropic").strip().lower()
            record_usage(
                run_id=run_id, call_type="batch", batch_ref=batch_ref,
                input_tokens=0, output_tokens=0, succeeded=False,
                model=s.OPENAI_MODEL if provider == "openai" else s.CLAUDE_MODEL,
                provider=provider,
            )
        except Exception:
            pass
        return None, raw


# -- AI call -- SINGLE-ROW fallback path (used only when a batch fails) --------

def _call_claude_single(
    narrative: str, bank_name: str, business_unit: str, credit_amount: float,
    currency: str, bank_reference: str, customer_reference_number: str,
    ou_number: str | None, invoice_candidates: str, customer_candidates: str,
    run_id: int, row_ref: str,
) -> tuple[dict, str]:
    """
    Fallback path: one call for one row. Only used when the batch containing
    this row could not be parsed as a whole, so a single malformed batch
    response doesn't blank out every row in it. Same provider-agnostic
    call_ai() as the batch path -- see _call_claude_batch's docstring.
    """
    raw = ""
    try:
        user_msg = _SINGLE_USER_TEMPLATE.format(
            narrative=narrative or "(empty)",
            ou_description=describe_ou(ou_number),
            bank_name=bank_name or "",
            business_unit=business_unit or "",
            credit_amount=credit_amount,
            currency=currency or "",
            bank_reference=bank_reference or "(none)",
            customer_reference_number=customer_reference_number or "(none)",
            max_inv=MAX_CANDIDATE_INVOICES_SHOWN,
            max_cust=MAX_CANDIDATE_CUSTOMERS_SHOWN,
            invoice_candidates=invoice_candidates,
            customer_candidates=customer_candidates,
        )

        dbg_block(run_id, "2B", row_ref, "FALLBACK SINGLE-ROW PROMPT SENT",
                   [_SYSTEM_PROMPT_SINGLE, "---", user_msg])

        result = call_ai(_SYSTEM_PROMPT_SINGLE, user_msg, 400)

        from ..ai_usage.tracker import record_usage
        record_usage(
            run_id=run_id, call_type="single", batch_ref=row_ref,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            latency_ms=result.latency_ms, succeeded=True,
            model=result.model, provider=result.provider,
        )

        raw = result.text
        dbg_block(run_id, "2B", row_ref, "FALLBACK RAW RESPONSE", [raw])

        cleaned = _strip_markdown_fences(raw)
        try:
            return json.loads(cleaned), raw
        except json.JSONDecodeError as parse_exc:
            dbg(run_id, "2B", row_ref,
                f"FALLBACK JSON parse FAILED: {parse_exc} | cleaned_text='{cleaned[:200]}'")
            logger.warning("Layer 2B fallback JSON parse failed for row %s: %s", row_ref, parse_exc)
            return {}, raw

    except Exception as exc:
        dbg(run_id, "2B", row_ref, f"FALLBACK AI call FAILED: {exc}")
        logger.warning("Layer 2B fallback AI call failed: %s", exc)
        try:
            from ..ai_usage.tracker import record_usage
            from ..db.settings import get_settings
            s = get_settings()
            provider = (s.AI_PROVIDER or "anthropic").strip().lower()
            record_usage(
                run_id=run_id, call_type="single", batch_ref=row_ref,
                input_tokens=0, output_tokens=0, succeeded=False,
                model=s.OPENAI_MODEL if provider == "openai" else s.CLAUDE_MODEL,
                provider=provider,
            )
        except Exception:
            pass
        return {}, raw


# -- Validation (unchanged -- operates on one row + one ai_data dict) ----------

def _validate_ai_output(
    ai_data: dict, row: Layer2ARow, aging_map: AgingMap, run_id: int, row_ref: str,
) -> tuple[list[str], Optional[str], bool]:
    """
    Validate AI-extracted entities against AgingMap.
    Returns (confirmed_invoices, confirmed_customer_name, was_cross_ou_check).
    """
    confirmed_invoices: list[str] = []
    confirmed_customer: Optional[str] = None
    cross_ou_used = False

    ou = row.original.ou_number

    for inv in ai_data.get("invoice_numbers") or []:
        inv_clean = str(inv).strip()
        hit = aging_map.lookup_invoice(inv_clean, ou)
        if hit is not None:
            confirmed_invoices.append(inv_clean)
            dbg(run_id, "2B", row_ref, f"[Validate] invoice '{inv_clean}' CONFIRMED in aging_map (OU={ou})")
        else:
            dbg(run_id, "2B", row_ref, f"[Validate] invoice '{inv_clean}' NOT found in aging_map (OU={ou}) -- discarded")

    ai_customer = ai_data.get("customer_name")
    in_ou_list = ai_data.get("customer_in_ou_candidates", True)

    if ai_customer:
        if in_ou_list and ou and hasattr(aging_map, "fuzzy_customer_in_ou"):
            match_row, score = aging_map.fuzzy_customer_in_ou(ai_customer, ou, min_pct=40.0)
            if match_row:
                confirmed_customer = match_row.customer_name
                dbg(run_id, "2B", row_ref,
                    f"[Validate] customer '{ai_customer}' CONFIRMED in-OU as '{confirmed_customer}' (score={score})")
            else:
                dbg(run_id, "2B", row_ref, f"[Validate] customer '{ai_customer}' did NOT confirm in-OU (score={score})")
        else:
            dbg(run_id, "2B", row_ref,
                f"[Validate] AI flagged '{ai_customer}' as OUTSIDE OU candidates (or OU unknown) -- "
                f"running GLOBAL (all-OU) fuzzy_customer check, min_pct={GLOBAL_FALLBACK_MIN_PCT}")
            cross_ou_used = True
            match_row, score = aging_map.fuzzy_customer(ai_customer, min_pct=GLOBAL_FALLBACK_MIN_PCT)

            if match_row:
                confirmed_customer = match_row.customer_name
                dbg(run_id, "2B", row_ref,
                    f"[Validate] CROSS-OU customer CONFIRMED: '{confirmed_customer}' (score={score}) "
                    f"-- flag for review: payment may be inter-company or row OU may be wrong")
            else:
                dbg(run_id, "2B", row_ref, f"[Validate] cross-OU check FAILED for '{ai_customer}' (score={score})")

    return confirmed_invoices, confirmed_customer, cross_ou_used


def _classify_row(
    row: Layer2ARow, ai_data: dict, raw_response: str,
    aging_map: AgingMap, run_id: int, row_ref: str,
) -> IdentifiedPayment | UnknownPayment:
    """Shared tail logic: validate one row's ai_data and return the classified payment."""
    if not ai_data:
        return UnknownPayment(
            original=row.original, regex_candidates_tried=row.regex_candidate_invoices,
            ai_attempted=True, ai_raw_response=raw_response or None, failure_reason="ai_no_output",
        )

    confirmed_invoices, confirmed_customer, cross_ou_used = _validate_ai_output(
        ai_data, row, aging_map, run_id, row_ref
    )

    row_type = "MULTI" if len(confirmed_invoices) >= 2 else ("SINGLE" if confirmed_invoices else "NONE")
    confidence = float(ai_data.get("confidence") or 0.0)

    if confirmed_invoices or confirmed_customer:
        dbg(run_id, "2B", row_ref,
            f"RESULT: IDENTIFIED row_type={row_type} invoices={confirmed_invoices} "
            f"customer={confirmed_customer or row.customer_fuzzy_match} confidence={confidence} "
            f"cross_ou_used={cross_ou_used}")
        return IdentifiedPayment(
            original=row.original,
            confirmed_invoice_numbers=confirmed_invoices,
            customer_name=confirmed_customer or row.customer_fuzzy_match,
            customer_match_pct=row.customer_match_pct,
            extraction_method="ai+aging_validated" if not cross_ou_used else "ai+aging_validated_cross_ou",
            confidence_score=confidence,
            identified_by_layer="2b",
            ai_raw_response=raw_response,
        )

    dbg(run_id, "2B", row_ref, f"RESULT: UNKNOWN -- nothing validated (confidence={confidence})")
    return UnknownPayment(
        original=row.original, regex_candidates_tried=row.regex_candidate_invoices,
        ai_attempted=True, ai_raw_response=raw_response, failure_reason="ai_validation_failed",
    )


def _process_batch_with_fallback(
    rows_batch: list[Layer2ARow], ou_number: str | None,
    invoice_candidates: str, customer_candidates: str, aging_map: AgingMap,
    run_id: int, batch_ref: str,
) -> tuple[list[IdentifiedPayment], list[UnknownPayment]]:
    """
    Run one batch through Claude; fall back to per-row calls if the batch itself
    failed to parse. Returns its own (identified, unknown) lists rather than
    mutating shared state, so this is safe to run concurrently with other
    batches from a ThreadPoolExecutor.
    """
    identified: list[IdentifiedPayment] = []
    unknown: list[UnknownPayment] = []

    parsed, raw_response = _call_claude_batch(
        rows_batch, ou_number, invoice_candidates, customer_candidates, run_id, batch_ref
    )

    if parsed is None:
        dbg(run_id, "2B", batch_ref,
            f"Batch of {len(rows_batch)} rows failed to parse -- falling back to per-row calls for this batch only")
        for idx, row in enumerate(rows_batch):
            orig = row.original
            row_ref = orig.bank_reference or f"{batch_ref}.idx={idx}"
            ai_data, single_raw = _call_claude_single(
                narrative=orig.narrative or "", bank_name=orig.bank_name, business_unit=orig.business_unit or "",
                credit_amount=orig.credit_amount, currency=orig.currency,
                bank_reference=orig.bank_reference or "", customer_reference_number=orig.customer_reference_number or "",
                ou_number=ou_number, invoice_candidates=invoice_candidates,
                customer_candidates=customer_candidates, run_id=run_id, row_ref=row_ref,
            )
            item = _classify_row(row, ai_data, single_raw, aging_map, run_id, row_ref)
            (identified if isinstance(item, IdentifiedPayment) else unknown).append(item)
        return identified, unknown

    # Map batch results back to rows by row_index (never assume list order == input order).
    by_index: dict[int, dict] = {}
    for item in parsed:
        if isinstance(item, dict) and "row_index" in item:
            try:
                by_index[int(item["row_index"])] = item
            except (TypeError, ValueError):
                continue

    for idx, row in enumerate(rows_batch):
        orig = row.original
        row_ref = orig.bank_reference or f"{batch_ref}.idx={idx}"
        ai_data = by_index.get(idx, {})
        if idx not in by_index:
            dbg(run_id, "2B", row_ref,
                f"WARNING: batch response missing row_index={idx} -- treating as no AI output for this row")
        classified = _classify_row(row, ai_data, raw_response, aging_map, run_id, row_ref)
        (identified if isinstance(classified, IdentifiedPayment) else unknown).append(classified)

    return identified, unknown


# -- Main entry point ---------------------------------------------------------------

def run_layer_2b(layer_2a_result: Layer2AResultSchema, aging_map: AgingMap) -> Layer2BResult:
    """
    Process no_invoice_found rows through the OU-grounded AI fallback,
    BATCHED by OU number to minimize network calls, and run CONCURRENTLY
    (up to AI_BATCH_MAX_CONCURRENCY at once) to cut wall-clock time.

    Note: this ThreadPoolExecutor is nested inside the chunk-level one in
    chunk_processor.py. Total AI calls in flight across the whole run at
    once is roughly CHUNK_MAX_WORKERS * AI_BATCH_MAX_CONCURRENCY.
    """
    run_id = layer_2a_result.run_id
    chunk_ref = f"chunk={layer_2a_result.chunk_index}"
    result = Layer2BResult(
        chunk_id=layer_2a_result.chunk_id,
        run_id=run_id,
        chunk_index=layer_2a_result.chunk_index,
        total_chunks=layer_2a_result.total_chunks,
    )

    batch_size, max_concurrency = _batch_settings()

    all_rows = layer_2a_result.no_invoice_found

    # Master switch (.env: AI_EXTRACTION_ENABLED=false) -- skip the ENTIRE Layer
    # 2B AI pass without making a single provider call, so local dev spends zero
    # tokens. Every unresolved row is returned as UnknownPayment with
    # ai_attempted=False (exactly as if the AI found nothing), so downstream
    # treats them as unidentified -- same handling as an empty-narrative row.
    from ..db.settings import get_settings
    if not get_settings().AI_EXTRACTION_ENABLED:
        dbg(run_id, "2B", "CHUNK",
            f"{chunk_ref} AI extraction DISABLED (AI_EXTRACTION_ENABLED=false) -- "
            f"skipping all {len(all_rows)} unresolved row(s), 0 AI calls")
        for row in all_rows:
            result.unknown.append(UnknownPayment(
                original=row.original, regex_candidates_tried=row.regex_candidate_invoices,
                ai_attempted=False, failure_reason="ai_disabled",
            ))
        return result

    dbg(run_id, "2B", "CHUNK",
        f"{chunk_ref} starting AI fallback for {len(all_rows)} unresolved rows "
        f"(batch_size={batch_size} from .env: AI_BATCH_SIZE, "
        f"max_concurrency={max_concurrency} from .env: AI_BATCH_MAX_CONCURRENCY)")

    # -- Split off empty/NaN narratives -- AI can't help, don't waste a call ----
    ai_eligible: list[Layer2ARow] = []
    for row in all_rows:
        narrative = (row.original.narrative or "").strip()
        if not narrative or narrative.lower() in ("nan", "none"):
            row_ref = row.original.bank_reference or f"idx={all_rows.index(row)}"
            dbg(run_id, "2B", row_ref, "SKIP -- empty/NaN narrative, AI cannot help")
            result.unknown.append(UnknownPayment(
                original=row.original, regex_candidates_tried=row.regex_candidate_invoices,
                ai_attempted=False, failure_reason="empty_narrative",
            ))
        else:
            ai_eligible.append(row)

    # -- Group by OU number -- grounding lists are identical within a group -----
    groups: dict[str, list[Layer2ARow]] = {}
    for row in ai_eligible:
        ou_key = (row.original.ou_number or "").strip() or "UNKNOWN_OU"
        groups.setdefault(ou_key, []).append(row)

    dbg(run_id, "2B", "CHUNK",
        f"{chunk_ref} grouped {len(ai_eligible)} rows into {len(groups)} OU group(s): "
        f"{[(k, len(v)) for k, v in groups.items()]}")

    # -- Build a flat list of batch jobs across ALL OU groups --------------------
    # (grounding is built once per OU group, then reused by every batch job for
    # that OU -- the jobs themselves are what get parallelized below)
    jobs: list[tuple[list, str | None, str, str, str]] = []
    for ou_key, rows in groups.items():
        ou_number = None if ou_key == "UNKNOWN_OU" else ou_key
        grounding_ref = f"{chunk_ref}.ou={ou_key}"
        invoice_candidates, customer_candidates, _ = _build_ou_grounding(
            aging_map, ou_number, run_id, grounding_ref
        )
        batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
        for b_idx, batch in enumerate(batches):
            batch_ref = f"{grounding_ref}.batch={b_idx}"
            jobs.append((batch, ou_number, invoice_candidates, customer_candidates, batch_ref))

    # -- Dispatch batch jobs concurrently -----------------------------------------
    if jobs:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
            futures = {
                pool.submit(_process_batch_with_fallback, batch, ou_number,
                            invoice_candidates, customer_candidates, aging_map, run_id, batch_ref): batch_ref
                for batch, ou_number, invoice_candidates, customer_candidates, batch_ref in jobs
            }
            for future in as_completed(futures):
                batch_ref = futures[future]
                try:
                    identified, unknown = future.result()
                    result.identified.extend(identified)
                    result.unknown.extend(unknown)
                except Exception as exc:
                    dbg(run_id, "2B", batch_ref, f"Batch job raised an unexpected exception: {exc}")
                    logger.warning("Layer 2B batch job %s failed: %s", batch_ref, exc)

    dbg(run_id, "2B", "CHUNK",
        f"{chunk_ref} done -> identified={len(result.identified)}, unknown={len(result.unknown)}")

    return result