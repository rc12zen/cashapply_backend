"""
app.rule_engine.orchestrator  (PATCHED v2)
==========================================
Wires Phase 1 → 2 → 3 for a single analysis run.

PATCH NOTES (this revision — currency fix on top of previous patch):

CURRENCY MODEL CHANGE
---------------------
Previous code resolved ONE fx leg: credited_currency → functional_currency.
This was wrong for comparison and for Oracle posting.

Correct three-currency model:
  credited_currency  : currency the bank received the payment in (from bank row)
  invoice_currency   : currency the invoice was raised in (from aging row)
  functional_currency: OU ledger currency (from ou_functional_currency.json)

Two FX legs are now resolved:

  Leg 1  fx_credit_to_invoice   (credited → invoice)
         Used to convert credit_amount into invoice currency so it can be
         compared against invoice outstanding amounts (which are in invoice
         currency). This converted amount is also what Oracle receives as
         "Amount" in the receipt payload.

  Leg 2  fx_invoice_to_functional  (invoice → functional)
         Passed to Oracle as ConversionRate. Oracle uses this internally to
         book the receipt in the functional-currency ledger. We never compute
         the functional amount ourselves — Oracle owns that calculation.

  Derived / same-currency short-circuits:
    If credited == invoice    → no Leg 1 needed; Amount = credit_amount as-is
    If invoice  == functional → no Leg 2 needed; no ConversionRate in payload
    If all three are equal    → fully same-currency, no FX at all

SEQUENCING NOTE
---------------
Invoice currency comes from the aging row, not from the bank row or OU config.
It therefore cannot be known until _resolve_matched_invoices() has run and
at least one aging row has been resolved.  The two-pass _build_rule_input
design is retained:

  Pass 1 (db=None, line_item=None):
    invoice_currency is None (aging rows not resolved yet).
    Leg 1 / Leg 2 are skipped.
    Only credited_currency and functional_currency are stored so the LineItem
    can be persisted with correct base currency data before rule evaluation.

  Pass 2 (db=db, line_item=line_item):
    Called after the LineItem exists (for remittance lookup).
    invoice_currency is read from the aging map via a lightweight pre-lookup
    on the first candidate invoice number before the full evaluate_row() call.
    Both FX legs are resolved here and passed into the rule_input dict.

  The LineItem is updated with the resolved fx fields after Pass 2
  (update_line_item_fx() helper below).

PREVIOUS PATCHES (unchanged)
-----------------------------
  - FIX: AgingMap now loaded from aging_store (not DB table)
  - FIX: _build_rule_input keyword args corrected
  - FIX: ou_mismatch via resolve_ou_status() instead of hardcoded False
  - FIX: customer_match_pct uses AI confidence_score when available
  - LOGGING: _log_aging_map_summary() for early visibility of aging state
"""
from __future__ import annotations

import datetime as dt
import hashlib
import threading
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.models import AnalysisRun, LineItem, RunStatus, SourceFile, StatementTransactionRow
from ..db.session import session_scope
from ..db.settings import get_settings

from ..aging.aging_map import AgingMap
from ..aging import aging_store
from ..extraction.debug_logger import dbg
from ..bank_statement.detector import detect_config
from ..bank_statement.parser import parse_credit_rows
from ..schemas.chunk import CreditRowSchema

from ..extraction.chunk_processor import dispatch_chunks
from ..schemas.extraction import IdentifiedPayment, UnknownPayment

from .evaluator import evaluate_row
from .state_machine import apply_transition
from .remittance_lookup import build_remittance_view
from .fx_service import FxService, get_functional_currency
from .ou_resolver import resolve_ou_status
from ..oracle.receipt_creation import create_receipt_for_line_item

STATEMENT_BUCKET = "bank-statements"


def run_analysis_background(run_id: int, selected_files: list[str]) -> None:
    """
    LEGACY PoC path — bare thread, no retry, no crash-survival. Kept only so
    any existing caller that hasn't been updated still works. New code
    should defer app.tasks.analysis_tasks.run_analysis_task instead (see
    bff/run_routes.py), which routes through _run_analysis_locked() below
    via a procrastinate worker process.
    """
    t = threading.Thread(target=_run_analysis_locked, args=(run_id, selected_files), daemon=True)
    t.start()


# ── Concurrency guard (design doc §4) ────────────────────────────────────────

def _advisory_lock_key(selected_files: list[str]) -> int:
    """
    Deterministic bigint key derived from the sorted set of filenames being
    analyzed, for pg_try_advisory_lock. Two runs over the same file set can
    never hold the lock simultaneously; two runs over disjoint file sets
    never contend.
    """
    joined = "|".join(sorted(selected_files))
    digest = hashlib.sha256(joined.encode()).hexdigest()
    # Postgres advisory locks take a signed bigint — fold the hash into range.
    return int(digest[:15], 16) - (1 << 59)


def _run_analysis_locked(run_id: int, selected_files: list[str]) -> None:
    """
    Wraps _run_analysis() with a non-blocking Postgres advisory lock keyed by
    the file set, so two concurrent requests to analyze the same statement(s)
    — two users, two tabs, an impatient double-click that got past the
    frontend's disabled-button guard — can't both proceed and double-consume
    the same StatementTransactionRow set. The second caller fails fast with a
    clear error instead of silently corrupting state or queuing invisibly.

    Uses its own dedicated connection for the lock's lifetime (advisory locks
    are session-scoped) — separate from the session _run_analysis() itself
    opens internally, so returning this session to the pool mid-run can't
    accidentally release the lock early.
    """
    lock_key = _advisory_lock_key(selected_files)

    from ..db.session import get_engine
    engine = get_engine()
    conn = engine.connect()
    try:
        got_lock = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}).scalar()
        if not got_lock:
            from ..db.session import session_scope
            with session_scope() as db:
                run = db.query(AnalysisRun).get(run_id)
                if run:
                    run.status = RunStatus.ERROR
                    run.error_message = (
                        "Another analysis run is already processing these files. "
                        "Wait for it to finish, then try again."
                    )
            return
        try:
            _run_analysis(run_id, selected_files)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
    finally:
        conn.close()


# ── Logging helper ─────────────────────────────────────────────────────────────

def _log_aging_map_summary(run_id: int, aging_map: AgingMap) -> None:
    """
    Prints/logs a full summary of the AgingMap right after it is loaded
    for this run so it is immediately visible (console + run log file)
    whether aging data actually loaded or not.
    """
    ou_keys = sorted(aging_map.ou_numbers)
    dbg(run_id, "INIT", "AGING_MAP",
        f"invoice_count={aging_map.invoice_count} "
        f"customer_count={aging_map.customer_count} "
        f"ou_count={len(ou_keys)} "
        f"ou_keys={ou_keys}")

    if aging_map.invoice_count == 0:
        dbg(run_id, "INIT", "AGING_MAP",
            "WARNING: AgingMap is EMPTY for this run. Layer 2B grounding will "
            "be (none available) for every row. Check that an aging report has "
            "been uploaded AND refreshed via aging.parser.refresh_aging_map().")
    else:
        sample_ou = ou_keys[0] if ou_keys else None
        if sample_ou:
            sample_invoices = aging_map.invoices_for_ou(sample_ou, limit=3)
            sample_customers = aging_map.customers_for_ou(sample_ou, limit=3)
            dbg(run_id, "INIT", "AGING_MAP",
                f"sample OU={sample_ou} -> "
                f"sample_invoices={sample_invoices} "
                f"sample_customers={sample_customers}")


# ── Invoice-currency pre-lookup ────────────────────────────────────────────────

def _resolve_invoice_currency(
    payment: IdentifiedPayment,
    aging_map: AgingMap,
    remittance_invoices: list[dict],
    ou_number: str | None,
) -> str | None:
    """
    Return the currency of the first resolvable invoice for this payment.

    Priority:
      1. Remittance invoice list (structured doc, more reliable)
      2. Extraction confirmed_invoice_numbers (narrative regex)

    Returns None when no candidate invoice resolves against the aging map
    (e.g. all invoices are closed/typo — R4 will fire later in evaluate_row).
    This is safe: _build_rule_input treats None as "same as credited" and
    skips Leg 1, which means no cross-currency conversion is attempted and
    the rule engine will eventually hit R4 or R8.
    """
    candidates = (
        [inv["invoice_number"] for inv in remittance_invoices]
        if remittance_invoices
        else (payment.confirmed_invoice_numbers or [])
    )
    for inv_no in candidates:
        aging_row = aging_map.lookup_invoice(inv_no, ou_number)
        if aging_row is not None:
            return (aging_row.invoice_currency or "").upper().strip() or None
    return None


# ── Rule-input builder ─────────────────────────────────────────────────────────

def _build_rule_input(
    payment: IdentifiedPayment,
    aging_map: AgingMap,
    db: Session | None,
    line_item: LineItem | None,
    fx_service: FxService,
) -> dict:
    """
    Assemble the RuleEngineInputSchema dict for evaluate_row().

    Called twice per payment:
      Pass 1 (db=None, line_item=None):
        Resolves credited_currency and functional_currency only.
        invoice_currency is not yet known (aging rows not resolved until
        _resolve_matched_invoices runs inside evaluate_row).
        FX legs are skipped — the cross_currency block is marked
        is_cross_currency=False so the rule engine does not attempt
        FX conversion on this pass. The LineItem is persisted with base
        currency data after this call.

      Pass 2 (db=db, line_item=line_item):
        Remittance lookup is performed (requires persisted line_item id).
        invoice_currency is resolved via _resolve_invoice_currency().
        Leg 1 (credit→invoice) and Leg 2 (invoice→functional) are resolved.
        The resulting cross_currency block is passed into evaluate_row().

    Currency fields in cross_currency block:
      credited_currency       : currency that arrived in the bank row
      invoice_currency        : currency the invoice was raised in (from aging)
                                None on Pass 1
      functional_currency     : OU ledger currency
      fx_credit_to_invoice    : rate to convert credit_amount → invoice currency
                                None if credited == invoice or not yet resolved
      fx_invoice_to_functional: rate Oracle uses to post invoice amt → functional
                                None if invoice == functional or not yet resolved
      fx_credit_to_invoice_source   : "oracle_gl" | "static_map" | None
      fx_invoice_to_functional_source: "oracle_gl" | "static_map" | None

      is_cross_currency: True when credited_currency != invoice_currency
                         (this drives conversion in evaluator & Oracle payload)
      is_cross_ledger  : True when invoice_currency != functional_currency
                         (this drives ConversionRate in Oracle payload)
    """
    orig = payment.original

    # ── Remittance (Pass 2 only) ──────────────────────────────────────────────
    remittance_view = (
        build_remittance_view(db, line_item, payment.customer_name)
        if db is not None and line_item is not None
        else {"found": False, "invoices": [], "ambiguous": False,
              "is_cross_currency": False, "remittance_currency": None}
    )

    # ── Base currencies ───────────────────────────────────────────────────────
    credited_currency   = (orig.currency or "").upper().strip()
    functional_currency = get_functional_currency(orig.ou_number) or credited_currency
    ou_number           = orig.ou_number
    rate_date           = orig.statement_date

    # ── Invoice currency (Pass 2 only) ───────────────────────────────────────
    # On Pass 1 we have no aging rows yet; skip FX and mark not cross-currency
    # so the evaluator does not attempt conversion.
    if db is not None and line_item is not None:
        remittance_invoices = remittance_view.get("invoices") or []
        invoice_currency = _resolve_invoice_currency(
            payment, aging_map, remittance_invoices, ou_number
        ) or credited_currency   # treat unresolved as same-currency (safe: R4 fires)
    else:
        invoice_currency = None  # unknown on Pass 1

    # ── Leg 1: credited → invoice ─────────────────────────────────────────────
    # Converts the bank credit amount into invoice currency so comparison and
    # Oracle Amount are both expressed in invoice currency.
    is_cross_currency = (
        bool(invoice_currency)
        and bool(credited_currency)
        and credited_currency != invoice_currency
    )

    fx_credit_to_invoice        = None
    fx_credit_to_invoice_source = None

    if is_cross_currency:
        fx_credit_to_invoice = fx_service.get_rate(
            from_ccy=credited_currency,
            to_ccy=invoice_currency,
            rate_date=rate_date,
        )
        if fx_credit_to_invoice is not None:
            oracle_rate = fx_service._fetch_from_oracle(
                credited_currency, invoice_currency,
                rate_date.strftime("%Y-%m-%d") if rate_date else "",
            )
            fx_credit_to_invoice_source = (
                "oracle_gl" if oracle_rate is not None else "static_map"
            )

    # ── Leg 2: invoice → functional ──────────────────────────────────────────
    # Rate that Oracle uses internally to post the receipt into the ledger.
    # We do NOT apply this rate ourselves — we only pass it to Oracle.
    is_cross_ledger = (
        bool(invoice_currency)
        and bool(functional_currency)
        and invoice_currency != functional_currency
    )

    fx_invoice_to_functional        = None
    fx_invoice_to_functional_source = None

    if is_cross_ledger and invoice_currency:
        fx_invoice_to_functional = fx_service.get_rate(
            from_ccy=invoice_currency,
            to_ccy=functional_currency,
            rate_date=rate_date,
        )
        if fx_invoice_to_functional is not None:
            oracle_rate = fx_service._fetch_from_oracle(
                invoice_currency, functional_currency,
                rate_date.strftime("%Y-%m-%d") if rate_date else "",
            )
            fx_invoice_to_functional_source = (
                "oracle_gl" if oracle_rate is not None else "static_map"
            )

    # ── Cross-OU status ───────────────────────────────────────────────────────
    ou_status = resolve_ou_status(
        customer_name=payment.customer_name,
        bank_ou_number=orig.ou_number,
        aging_map=aging_map,
        fuzzy_min_pct=60.0,
    )

    # ── Effective customer match pct ──────────────────────────────────────────
    if payment.customer_name is not None and payment.confidence_score is not None:
        effective_customer_match_pct = payment.confidence_score * 100.0
    else:
        effective_customer_match_pct = payment.customer_match_pct

    return {
        "original_row": {
            "credit_amount":       orig.credit_amount,
            "currency":            credited_currency,
            "functional_currency": functional_currency,
            "narrative":           orig.narrative,
            "bank_reference":      orig.bank_reference,
            "ou_number":           orig.ou_number,
        },
        "extraction": {
            "extracted_invoices":  payment.confirmed_invoice_numbers,
            "customer_match_pct":  effective_customer_match_pct,
            "invoice_match_pct":   100.0 if payment.confirmed_invoice_numbers else 0.0,
            "customer_text_match": payment.customer_name is not None,
        },
        "remittance": remittance_view,
        "aging_lookup": lambda inv_no, ou: aging_map.lookup_invoice(inv_no, ou),
        "cross_currency": {
            # Leg 1 — credit → invoice (for comparison + Oracle Amount)
            "is_cross_currency":              is_cross_currency,
            "credited_currency":              credited_currency,
            "invoice_currency":               invoice_currency,
            "fx_credit_to_invoice":           fx_credit_to_invoice,
            "fx_credit_to_invoice_source":    fx_credit_to_invoice_source,

            # Leg 2 — invoice → functional (for Oracle ConversionRate)
            "is_cross_ledger":                is_cross_ledger,
            "functional_currency":            functional_currency,
            "fx_invoice_to_functional":       fx_invoice_to_functional,
            "fx_invoice_to_functional_source": fx_invoice_to_functional_source,
        },
        "ou_mismatch":                        ou_status.is_cross_ou,
        "customer_ou_numbers":                ou_status.customer_ous,
        "duplicate_invoice_across_customers": False,
        "already_processed_match":            False,
    }


# ── LineItem persistence helpers ───────────────────────────────────────────────

def _persist_line_item(
    db: Session,
    run_id: int,
    orig: CreditRowSchema,
    extraction_method: str,
    customer_name: str | None,
    invoice_numbers: list[str],
    customer_match_pct: float,
    confidence_score: float | None,
    # Currency fields populated after Pass 1 rule_input build
    credited_currency: str | None = None,
    functional_currency: str | None = None,
    invoice_currency: str | None = None,         # None on Pass 1 — updated after Pass 2
    is_cross_currency: bool = False,             # credited != invoice
    is_cross_ledger: bool = False,               # invoice != functional
    fx_credit_to_invoice: float | None = None,
    fx_credit_to_invoice_source: str | None = None,
    fx_invoice_to_functional: float | None = None,
    fx_invoice_to_functional_source: str | None = None,
    remittance_extraction_id: int | None = None,
) -> LineItem:
    """Create and flush a LineItem record before rule evaluation."""
    li = LineItem(
        run_id=run_id,
        bank_name=orig.bank_name,
        account_number=orig.account_number,
        business_unit=orig.business_unit,
        ou_number=orig.ou_number,
        statement_date=orig.statement_date,
        narrative=orig.narrative,
        credit_amount=orig.credit_amount,

        # Three-currency model
        statement_currency=credited_currency,           # what arrived in bank
        invoice_currency=invoice_currency,              # invoice denomination (may be None initially)
        functional_currency=functional_currency,        # OU ledger currency

        # Leg 1: credit → invoice
        is_cross_currency=is_cross_currency,
        fx_credit_to_invoice=fx_credit_to_invoice,
        fx_credit_to_invoice_source=fx_credit_to_invoice_source,

        # Leg 2: invoice → functional (Oracle ConversionRate)
        is_cross_ledger=is_cross_ledger,
        fx_invoice_to_functional=fx_invoice_to_functional,
        fx_invoice_to_functional_source=fx_invoice_to_functional_source,

        bank_reference=orig.bank_reference,
        extracted_customer_name=customer_name,
        extracted_invoice_numbers=invoice_numbers,
        extraction_method=extraction_method,
        customer_match_pct=customer_match_pct,
        confidence_score=confidence_score,
        remittance_extraction_id=remittance_extraction_id,
    )
    db.add(li)
    db.flush()
    return li


def _update_line_item_fx(
    db: Session,
    line_item: LineItem,
    rule_input: dict,
) -> None:
    """
    After Pass 2 rule_input is built, back-fill the resolved FX fields onto
    the LineItem that was already persisted from Pass 1.

    This is necessary because Pass 1 cannot know invoice_currency (it comes
    from the aging row, which is only resolved during evaluate_row).
    """
    cc = rule_input.get("cross_currency") or {}

    line_item.invoice_currency               = cc.get("invoice_currency")
    line_item.is_cross_currency              = cc.get("is_cross_currency", False)
    line_item.fx_credit_to_invoice           = cc.get("fx_credit_to_invoice")
    line_item.fx_credit_to_invoice_source    = cc.get("fx_credit_to_invoice_source")
    line_item.is_cross_ledger                = cc.get("is_cross_ledger", False)
    line_item.fx_invoice_to_functional       = cc.get("fx_invoice_to_functional")
    line_item.fx_invoice_to_functional_source = cc.get("fx_invoice_to_functional_source")

    db.flush()


def _mark_row_consumed(db: Session, orig: CreditRowSchema, run_id: int) -> None:
    """
    Stamps consumed_by_run_id on the StatementTransactionRow this
    CreditRowSchema was sourced from (design doc §0) — in the SAME
    transaction as the LineItem it produced (both flushed together, both
    committed together at the end of the chunk loop below), so a failed run
    never "consumes" a row it didn't actually produce a LineItem for.
    No-op for rows from the legacy direct-parse fallback (statement_row_id
    is None in that path).
    """
    if orig.statement_row_id is None:
        return
    db.query(StatementTransactionRow).filter(
        StatementTransactionRow.id == orig.statement_row_id
    ).update({"consumed_by_run_id": run_id})


# ── Main analysis runner ───────────────────────────────────────────────────────

def _run_analysis(run_id: int, selected_files: list[str]) -> None:
    settings = get_settings()

    from ..storage.client import get_storage_client
    storage = get_storage_client()

    with session_scope() as db:
        try:
            # ── Step 1: Load AgingMap ─────────────────────────────────────────
            aging_map = aging_store.get_aging_map()
            if aging_map is None:
                raise RuntimeError(
                    "No aging map loaded. Upload and refresh an aging report "
                    "via /api/config/refresh-aging before running an analysis."
                )
            _log_aging_map_summary(run_id, aging_map)

            # ── Step 1b: Build FxService once per run ─────────────────────────
            fx_service = FxService(
                oracle_base_url=settings.ORACLE_FUSION_BASE_URL,
                oracle_auth=(
                    (settings.ORACLE_BASIC_USERNAME, settings.ORACLE_BASIC_PASSWORD)
                    if settings.ORACLE_AUTH_MODE == "basic"
                    else None
                ),
            )

            all_credit_rows: list[CreditRowSchema] = []
            seen_row_ids: set[int] = set()  # avoid double-adding a row if two
            # selected filenames resolve to the same bank_account_id

            # ── Step 2: Load credit rows for all selected bank statement files ─
            #
            # PREFERRED PATH (design doc §0): pull unconsumed rows from the
            # durable StatementTransactionRow ledger — populated by the
            # background ingestion job (app.ingestion.ingest_service) at
            # upload time, already deduplicated across separate uploads.
            # This is what makes "upload today's statement, tomorrow's has
            # 80% overlapping rows" cheap: only genuinely-new rows land here.
            #
            # Scoped by BANK ACCOUNT, not by the specific SourceFile the row
            # happened to be ingested under. Row-level dedup (the row's
            # UNIQUE(bank_account_id, row_hash) constraint) is already
            # account-scoped, so a row that first landed under an earlier
            # upload for this account — even one later archived via the
            # frontend's "remove" (✕) button, which only sets
            # SourceFile.archived and never touches these rows — is still
            # reachable by any later run against the same account. Scoping
            # by source_file_id instead (the previous behavior) meant those
            # rows became permanently unconsumable the moment their original
            # file was archived, even though they were never processed.
            #
            # LEGACY FALLBACK: a SourceFile that predates the ingestion split
            # (ingest_status is NULL) or hasn't finished ingesting yet falls
            # back to the original direct-file-parse behavior so existing
            # data / in-flight uploads keep working during the migration
            # window. New uploads should always go through ingest_status.
            for filename in selected_files:
                source = db.query(SourceFile).filter(
                    SourceFile.kind == "bank_statement",
                    SourceFile.filename == filename,
                ).first()
                if not source:
                    continue

                if source.ingest_status == "ready":
                    if source.bank_account_id is not None:
                        row_query = db.query(StatementTransactionRow).filter(
                            StatementTransactionRow.bank_account_id == source.bank_account_id,
                            StatementTransactionRow.consumed_by_run_id.is_(None),
                        )
                    else:
                        # No bank account could be resolved at ingest time
                        # (e.g. account number missing from the statement) —
                        # fall back to the old file-scoped query rather than
                        # risk pulling in unrelated rows under a shared
                        # NULL bucket.
                        row_query = db.query(StatementTransactionRow).filter(
                            StatementTransactionRow.source_file_id == source.id,
                            StatementTransactionRow.consumed_by_run_id.is_(None),
                        )

                    unconsumed = row_query.all()
                    for row in unconsumed:
                        if row.id in seen_row_ids:
                            continue
                        seen_row_ids.add(row.id)
                        all_credit_rows.append(CreditRowSchema(
                            run_id=run_id,
                            source_filename=filename,
                            row_index=row.id,
                            bank_name=(row.raw_row_json or {}).get("bank_name") or "",
                            bank_config_key=source.bank_config_key or "UNKNOWN",
                            account_number=(row.raw_row_json or {}).get("account_number"),
                            business_unit=(row.raw_row_json or {}).get("business_unit") or source.business_unit,
                            ou_number=(row.raw_row_json or {}).get("ou_number") or source.ou_number,
                            statement_date=row.statement_date,
                            narrative=row.narrative,
                            credit_amount=float(row.credit_amount or 0),
                            currency=row.currency,
                            bank_reference=row.bank_reference,
                            statement_row_id=row.id,
                        ))
                    continue

                # Legacy fallback — unchanged from the original implementation.
                local_path = storage.local_path_for_read(STATEMENT_BUCKET, source.storage_key)
                detection = detect_config(local_path)
                if not detection.success:
                    continue

                raw_rows = parse_credit_rows(local_path, detection, filename)

                for idx, raw in enumerate(raw_rows):
                    all_credit_rows.append(CreditRowSchema(
                        run_id=run_id,
                        source_filename=filename,
                        row_index=idx,
                        bank_name=raw.bank_name,
                        bank_config_key=detection.config_key or "UNKNOWN",
                        account_number=raw.account_number,
                        business_unit=raw.business_unit,
                        ou_number=raw.ou_number,
                        statement_date=raw.statement_date,
                        narrative=raw.narrative,
                        credit_amount=raw.credit_amount,
                        currency=raw.currency,
                        bank_reference=raw.bank_reference,
                    ))

            dbg(run_id, "INIT", "CREDIT_ROWS",
                f"loaded {len(all_credit_rows)} credit rows from "
                f"{len(selected_files)} selected file(s)")

            # ── Step 3: Extraction ────────────────────────────────────────────
            chunk_results = dispatch_chunks(
                run_id=run_id,
                rows=all_credit_rows,
                aging_map=aging_map,
                customer_fuzzy_min_pct=settings.CUSTOMER_FUZZY_MATCH_MIN_PCT,
            )

            # ── Step 4: Rule evaluation + DB persistence ──────────────────────
            for chunk_result in chunk_results:

                for payment in chunk_result.identified_payments:
                    orig = payment.original

                    # Pass 1 — resolve credited/functional currencies only.
                    # invoice_currency is None (aging not resolved yet).
                    # We persist the LineItem now so it has a DB id for the
                    # remittance lookup in Pass 2.
                    pass1_input = _build_rule_input(
                        payment, aging_map, db=None, line_item=None, fx_service=fx_service
                    )
                    cc1 = pass1_input["cross_currency"]

                    line_item = _persist_line_item(
                        db, run_id, orig,
                        extraction_method=payment.extraction_method,
                        customer_name=payment.customer_name,
                        invoice_numbers=payment.confirmed_invoice_numbers,
                        customer_match_pct=payment.customer_match_pct,
                        confidence_score=payment.confidence_score,
                        credited_currency=cc1["credited_currency"],
                        functional_currency=cc1["functional_currency"],
                        invoice_currency=None,          # filled after Pass 2
                        is_cross_currency=False,        # filled after Pass 2
                        is_cross_ledger=False,          # filled after Pass 2
                    )

                    # Pass 2 — remittance lookup + invoice_currency resolution +
                    # both FX legs resolved.
                    pass2_input = _build_rule_input(
                        payment, aging_map, db, line_item, fx_service
                    )

                    # Back-fill FX fields onto the LineItem now that we know them.
                    _update_line_item_fx(db, line_item, pass2_input)

                    rule_result = evaluate_row(
                        pass2_input,
                        short_payment_tolerance_pct=settings.SHORT_PAYMENT_TOLERANCE_PCT,
                        customer_fuzzy_min_pct=settings.CUSTOMER_FUZZY_MATCH_MIN_PCT,
                    )
                    apply_transition(db, line_item, rule_result, trigger="rule_engine")
                    _mark_row_consumed(db, orig, run_id)

                for unknown in chunk_result.unknown_payments:
                    orig = unknown.original
                    line_item = _persist_line_item(
                        db, run_id, orig,
                        extraction_method="none",
                        customer_name=None,
                        invoice_numbers=[],
                        customer_match_pct=0.0,
                        confidence_score=None,
                        credited_currency=(orig.currency or "").upper().strip(),
                        functional_currency=get_functional_currency(orig.ou_number),
                    )
                    from .evaluator import RuleResult
                    apply_transition(
                        db, line_item,
                        RuleResult("R8", "NO_SIGNAL", "unidentified"),
                        trigger="rule_engine",
                    )
                    _mark_row_consumed(db, orig, run_id)

                db.commit()

            # ── Step 4.5: Create a bare Oracle receipt for EVERY credit row ────
            # in this run — regardless of category (unidentified,
            # needs_remittance, ready_for_oracle, conflict_exception, all of
            # it). This is the "Receipt Creation" half of the new two-step
            # flow: no longer gated on ready_for_oracle / SPOC approval, and
            # deliberately sent WITHOUT remittanceReferences (see
            # oracle.fusion_client.build_receipt_creation_payload). Invoice
            # mapping (attaching remittanceReferences to this same receipt)
            # happens later, only for ready_for_oracle rows, at SPOC-approval
            # time — see hitl/service.py.
            #
            # Runs synchronously here (this whole function already executes
            # inside the background worker, so a per-row HTTP call to Oracle
            # doesn't block the user) and commits after each row so a
            # mid-loop failure doesn't lose progress already made.
            all_line_items_this_run = (
                db.query(LineItem).filter(LineItem.run_id == run_id).all()
            )
            dbg(run_id, "ORACLE", "batch", f"Step 4.5: creating receipts for {len(all_line_items_this_run)} row(s)...")
            receipt_success_count = 0
            receipt_failed_count = 0
            for li in all_line_items_this_run:
                try:
                    result = create_receipt_for_line_item(db, li)
                    if result.get("success"):
                        receipt_success_count += 1
                        dbg(run_id, "ORACLE", f"row={li.id}", f"Receipt created — StandardReceiptId={li.standard_receipt_id} ReceiptNumber={li.oracle_ref_no}")
                    else:
                        receipt_failed_count += 1
                        dbg(run_id, "ORACLE", f"row={li.id}", f"Receipt creation FAILED — {li.post_message}")
                except Exception as receipt_exc:
                    receipt_failed_count += 1
                    li.oracle_post_status = "failed"
                    li.post_message = f"Receipt creation raised: {receipt_exc}"
                    dbg(run_id, "ORACLE", f"row={li.id}", f"Receipt creation RAISED — {receipt_exc}")
                db.commit()
            dbg(run_id, "ORACLE", "batch", f"Step 4.5 complete: {receipt_success_count} succeeded, {receipt_failed_count} failed out of {len(all_line_items_this_run)} row(s).")

            # ── Step 5: Mark run complete ─────────────────────────────────────
            run = db.query(AnalysisRun).get(run_id)
            run.status = RunStatus.COMPLETED
            run.completed_at = dt.datetime.utcnow()
            db.commit()

        except Exception as exc:
            run = db.query(AnalysisRun).get(run_id)
            if run:
                run.status = RunStatus.ERROR
                run.error_message = str(exc)
            db.commit()
            raise