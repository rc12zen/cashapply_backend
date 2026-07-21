"""
app.bff.metrics  (PATCHED)
==========================================
Computes dashboard KPIs and run-summary tabs.

PATCH NOTES (this revision):
  - PROBLEM: LineItem only ever persists `current_state` (collapsed to just
    "unidentified" or "review_approve" — see state_machine.CATEGORY_TO_STATE)
    and `reason_code`/`rule_id` (fine-grained: R7, R9a, R9b, R11, etc). The
    frontend's tab filters were matching on `current_state`, so every
    sub-category that collapses to "review_approve" (needs_remittance,
    acceptable_short_payment, ready_to_post, conflict_exception) showed
    count=0 except for a generic catch-all.
  - FIX: added RULE_ID_TO_GROUP, a precise rule_id -> display-group mapping,
    and _category_for_row(), which layers terminal states (processed /
    rejected / post_failed) on top of it. This is now the SINGLE source of
    truth for "which bucket does this row belong in" — both compute_run_summary()
    and the per-row `category` field use it, so the dashboard KPI cards and
    the Line Items Ledger tabs are always consistent with each other.
  - MERGE: R9a (EXACT_MATCH) and R9b (ACCEPTABLE_SHORT_PAYMENT) are merged
    into a single "ready_for_oracle" group, since both go through the
    identical SPOC-approve -> Oracle-POST path and showing them as two
    separate KPIs was creating confusion. `passed_validation` already
    treated these two as equivalent on the backend (see state_machine.py),
    so this merge matches existing semantics, not just a display choice.
  - tabs now has one bucket per real group (unidentified, needs_remittance,
    ready_for_oracle, conflict_exception, processed, rejected, post_failed)
    instead of the old (matched, not_found, review_approve, processed)
    shape that required the frontend to re-derive sub-categories itself.
  - Legacy top-level fields (found/not_found/passed_validation/etc.) are
    KEPT in compute_metrics() for backward compatibility with any other
    consumer of that endpoint, but compute_run_summary()'s `metrics` dict
    is now the new 7-card shape directly — the frontend's dashboard for the
    run-detail view should read these new keys.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..db.models import AnalysisRun, LineItem, RowStatusHistory
from ..aging import aging_store
from ..rule_engine.fx_service import _STATIC_RATE_MAP
from .date_range import parse_date_from, parse_date_to


# ── Category / group mapping — SINGLE SOURCE OF TRUTH ────────────────────────
#
# rule_id is the most precise signal LineItem persists (current_state is
# collapsed to just "unidentified"/"review_approve" by the state machine and
# can't distinguish R7 from R9a from R11, etc — see state_machine.py
# CATEGORY_TO_STATE). Every rule_id below maps 1:1 to exactly one of the
# groups the dashboard/ledger now show.
GROUP_UNIDENTIFIED       = "unidentified"
GROUP_NEEDS_REMITTANCE   = "needs_remittance"
GROUP_READY_FOR_ORACLE   = "ready_for_oracle"   # MERGED: R9a (exact) + R9b (within tolerance)
GROUP_CONFLICT_EXCEPTION = "conflict_exception"
GROUP_PROCESSED          = "processed"           # terminal — overrides rule_id
GROUP_REJECTED           = "rejected"             # terminal — overrides rule_id
GROUP_POST_FAILED        = "post_failed"          # terminal — overrides rule_id

RULE_ID_TO_GROUP: dict[str, str] = {
    "R8":  GROUP_UNIDENTIFIED,
    "R7":  GROUP_NEEDS_REMITTANCE,
    "R9a": GROUP_READY_FOR_ORACLE,
    "R9b": GROUP_READY_FOR_ORACLE,
    "R0":  GROUP_CONFLICT_EXCEPTION,
    "R1":  GROUP_CONFLICT_EXCEPTION,
    "R2":  GROUP_CONFLICT_EXCEPTION,
    "R3":  GROUP_CONFLICT_EXCEPTION,
    "R4":  GROUP_CONFLICT_EXCEPTION,
    "R5":  GROUP_CONFLICT_EXCEPTION,
    "R6":  GROUP_CONFLICT_EXCEPTION,
    "R9c": GROUP_CONFLICT_EXCEPTION,
    "R11": GROUP_CONFLICT_EXCEPTION,
    "R13": GROUP_CONFLICT_EXCEPTION,
    "R14": GROUP_CONFLICT_EXCEPTION,
}

# Display labels — used wherever a human-readable group name is needed.
GROUP_LABELS: dict[str, str] = {
    GROUP_UNIDENTIFIED:       "Unidentified",
    GROUP_NEEDS_REMITTANCE:   "Needs Remittance",
    GROUP_READY_FOR_ORACLE:   "Ready for Oracle",
    GROUP_CONFLICT_EXCEPTION: "Conflict / Exception",
    GROUP_PROCESSED:          "Processed",
    GROUP_REJECTED:           "Rejected",
    GROUP_POST_FAILED:        "Post Failed",
}


def _category_for_row(r: LineItem) -> str:
    """
    The single function that decides which of the 7 display groups a row
    belongs to. Terminal states (set by HITL approve/reject + Oracle
    invoice-mapping result) ALWAYS take priority over the rule_id
    grouping, since a row that's been approved-and-invoice-mapped is
    "processed" regardless of whether it got there via R9a or R9b.

    PATCH: was r.oracle_post_status — that field now only means "a bare
    receipt was created during Bank Reconciliation" (see
    rule_engine/orchestrator.py's Step 4.5), which happens for EVERY row
    regardless of category. Using it here would have made every row
    "Processed" the instant a run finished, before anyone approved
    anything. reference_status is the invoice-mapping outcome — set only
    when a SPOC approves a ready_for_oracle row (hitl/service.py) — which
    is what "Processed"/"Posted"/"Invoice Mapped" actually means.
    """
    if r.reference_status == "success":
        return GROUP_PROCESSED
    if r.hitl_status == "rejected":
        return GROUP_REJECTED
    if r.reference_status == "failed":
        return GROUP_POST_FAILED
    return RULE_ID_TO_GROUP.get(r.rule_id, GROUP_CONFLICT_EXCEPTION)


def _base_query(db: Session, run_id: int | None, date_from: str | None, date_to: str | None,
                 bank_name: str | None = None, business_unit: str | None = None,
                 run_by: str | None = None):
    q = db.query(LineItem)
    if run_id:
        q = q.filter(LineItem.run_id == run_id)
    if date_from:
        # PATCH: was LineItem.statement_date — the date PRINTED ON the bank
        # statement (i.e. the transaction's own date, which for e.g. an
        # "Apr-2026.xlsx" file is in April regardless of when it was
        # uploaded/processed). The frontend's Today/Yesterday/WTD/MTD pills
        # compute date ranges relative to *now*, so filtering on
        # statement_date meant clicking "Today" almost always returned
        # nothing — it was asking "was this bank transaction dated today",
        # not "did we process this today". created_at (set when the row
        # was inserted during an analysis run) is the axis that actually
        # matches what those pills mean, and matches how Analysis History's
        # own date filter already works (AnalysisRun.started_at — see
        # bff/run_routes.py's get_run_history).
        # Parse before comparing — a raw string vs a timestamp column throws
        # on Postgres. See bff/date_range.py.
        q = q.filter(LineItem.created_at >= parse_date_from(date_from))
    if date_to:
        # parse_date_to pushes the boundary to end-of-day: created_at has a
        # real time-of-day component, so a bare "<= date_to" would exclude
        # almost the entire day.
        q = q.filter(LineItem.created_at <= parse_date_to(date_to))
    if bank_name:
        q = q.filter(LineItem.bank_name == bank_name)
    if business_unit:
        q = q.filter(LineItem.business_unit == business_unit)
    if run_by and not run_id:
        # Skip when run_id is already set ("Last Analysis" pill): run_id pins
        # one specific run, which already has exactly one triggered_by. ANDing
        # run_by on top only ever narrows to that same run (redundant) or,
        # far more often, zeroes every row out (whenever the selected user
        # didn't happen to start that exact latest run) — making the whole
        # dashboard look broken every time the User dropdown changes.
        # PATCH: was RowStatusHistory.triggered_by ("approved_by") — that
        # only exists once a human has actually clicked Approve/Reject on
        # at least one row, so the User dropdown stayed empty for every
        # run until someone did HITL work on it. AnalysisRun.triggered_by
        # (the run STARTER's email — set at /api/run/start, see
        # bff/run_routes.py) is known the instant a run exists, no waiting
        # required. Matches the same field Analysis History's "Started By"
        # filter already uses (bff/run_routes.py's get_run_history).
        matching_run_ids = (
            db.query(AnalysisRun.run_id)
            .filter(AnalysisRun.triggered_by == run_by)
            .subquery()
        )
        q = q.filter(LineItem.run_id.in_(matching_run_ids))
    return q


def compute_metrics(db: Session, run_id: int | None = None, date_from: str | None = None,
                     date_to: str | None = None, bank_name: str | None = None,
                     business_unit: str | None = None, run_by: str | None = None) -> dict:
    """
    Dashboard-wide KPIs. Legacy field names are KEPT here for backward
    compatibility with any existing consumer of this endpoint. New grouped
    counts are added under `groups` for callers that want the merged,
    unambiguous buckets instead.
    """
    q = _base_query(db, run_id, date_from, date_to, bank_name, business_unit, run_by)
    rows = q.all()

    def count(pred):
        return sum(1 for r in rows if pred(r))

    groups = _group_counts(rows)

    # Bank statements analysed within the filtered scope — sum the
    # selected_files of every run represented in the filtered rows. Unifies
    # run-scoped ("Last Analysis") and date/bank/BU/user modes; empty scope -> 0.
    # ponytail: bank/BU filter narrows rows (hence runs) but selected_files is
    # per-run, not per-bank — so this over-counts a multi-bank run's files under
    # a bank filter. Add a LineItem->source_file_id FK for exact per-bank counts.
    run_ids = {r.run_id for r in rows if r.run_id is not None}
    if run_ids:
        runs = db.query(AnalysisRun).filter(AnalysisRun.run_id.in_(run_ids)).all()
        total_statements = sum(len(ro.selected_files or []) for ro in runs)
    else:
        total_statements = 0

    return {
        # ── Legacy fields — unchanged, kept for backward compatibility ──────
        "total_rows_ingested": len(rows),
        "found": count(lambda r: r.is_matched),
        "not_found": count(lambda r: not r.is_matched),
        "passed_validation": count(lambda r: r.passed_validation),
        "failed_validation": count(lambda r: not r.passed_validation and r.is_matched),
        "pending_hitl": count(lambda r: r.current_state == "review_approve"),
        "approved": count(lambda r: r.hitl_status == "approved"),
        "rejected": count(lambda r: r.hitl_status == "rejected"),
        "posted_to_oracle": count(lambda r: r.reference_status == "success"),  # PATCH: was oracle_post_status — see _category_for_row
        "extraction_method_breakdown": _breakdown(rows),
        "aging_report_loaded":    aging_store.get_status().get("loaded", False),
        "aging_report_row_count": aging_store.get_status().get("row_count", 0),
        "total_statements": total_statements,

        # ── New, unambiguous grouped counts ──────────────────────────────────
        "groups": groups,
        "identified": len(rows) - groups.get("unidentified", 0),

        # ── Amount view (USD equivalent) ──────────────────────────────────────
        "group_amounts": _group_amounts(rows),
        "total_usd_amount": round(sum(_to_usd(r) for r in rows), 2),
    }


def _to_functional_amount(r: LineItem) -> float:
    """
    Convert a row's credit_amount to ITS OWN functional (ledger) currency
    using the two FX legs already stored on the LineItem.

    Leg 1: statement_currency → invoice_currency  (fx_credit_to_invoice)
    Leg 2: invoice_currency   → functional_currency (fx_invoice_to_functional)

    Combined: credit_amount × leg1_rate × leg2_rate = functional amount.

    If either rate is missing we treat it as 1.0 (same currency on that leg).

    IMPORTANT: this is NOT always INR. functional_currency is whichever
    ledger currency the row's OU uses (see
    rule_engine/configs/ou_functional_currency.json) — USD for US OUs,
    MXN for Mexico OUs, GBP for UK, etc. This function used to be named
    _to_inr() and was summed directly into a KPI labeled "INR", which
    silently produced a meaningless mixed-currency total the moment any
    row belonged to a non-Indian OU. See _to_usd() below for the actual
    single-currency aggregate this dashboard needs.
    """
    amount = float(r.credit_amount or 0)
    if not amount:
        return 0.0
    leg1 = float(r.fx_credit_to_invoice or 1.0)
    leg2 = float(r.fx_invoice_to_functional or 1.0)
    return round(amount * leg1 * leg2, 2)


def _convert_static(amount: float, from_ccy: str, to_ccy: str) -> float:
    """
    Approximate currency conversion using rule_engine.fx_service's static
    fallback rate table — the SAME table already used for rule-band
    evaluation when Oracle's live GL rate isn't available, so this display
    conversion is at least consistent with the rest of the app rather
    than inventing a second, different rate source.

    NOT for Oracle posting (same caveat as the source table) — this is
    purely for the dashboard's single-currency summary view. Tries a
    direct pair, then the inverse, then bridges through USD.
    """
    from_ccy = (from_ccy or "").upper()
    to_ccy = (to_ccy or "").upper()
    if not from_ccy or from_ccy == to_ccy:
        return amount

    direct = _STATIC_RATE_MAP.get(f"{from_ccy}_{to_ccy}")
    if direct:
        return round(amount * direct, 2)

    inverse = _STATIC_RATE_MAP.get(f"{to_ccy}_{from_ccy}")
    if inverse:
        return round(amount / inverse, 2)

    from_to_usd = _STATIC_RATE_MAP.get(f"{from_ccy}_USD")
    usd_to_target = _STATIC_RATE_MAP.get(f"USD_{to_ccy}")
    if from_to_usd and usd_to_target:
        return round(amount * from_to_usd * usd_to_target, 2)

    # No rate found anywhere — return the raw amount rather than silently
    # dropping it, but this case means the rate table needs a new entry
    # for this currency pair (see fx_service.py's _STATIC_RATE_MAP).
    return amount


def _to_usd(r: LineItem) -> float:
    """The dashboard's actual single-currency total — every row's amount
    converted from ITS OWN functional currency into USD, so rows from
    different OUs/ledgers can be summed together meaningfully."""
    functional_amount = _to_functional_amount(r)
    return _convert_static(functional_amount, r.functional_currency or "USD", "USD")


def _group_amounts(rows: list[LineItem]) -> dict:
    """USD-equivalent total credit amount per display group."""
    amounts = {g: 0.0 for g in (
        GROUP_UNIDENTIFIED, GROUP_NEEDS_REMITTANCE, GROUP_READY_FOR_ORACLE,
        GROUP_CONFLICT_EXCEPTION, GROUP_PROCESSED, GROUP_REJECTED, GROUP_POST_FAILED,
    )}
    for r in rows:
        amounts[_category_for_row(r)] += _to_usd(r)
    return {k: round(v, 2) for k, v in amounts.items()}
    """Counts every row into exactly one of the 7 display groups."""
    counts = {g: 0 for g in (
        GROUP_UNIDENTIFIED, GROUP_NEEDS_REMITTANCE, GROUP_READY_FOR_ORACLE,
        GROUP_CONFLICT_EXCEPTION, GROUP_PROCESSED, GROUP_REJECTED, GROUP_POST_FAILED,
    )}
    for r in rows:
        counts[_category_for_row(r)] += 1
    return counts


def _group_counts(rows: list[LineItem]) -> dict:
    """Counts every row into exactly one of the 7 display groups."""
    counts = {g: 0 for g in (
        GROUP_UNIDENTIFIED, GROUP_NEEDS_REMITTANCE, GROUP_READY_FOR_ORACLE,
        GROUP_CONFLICT_EXCEPTION, GROUP_PROCESSED, GROUP_REJECTED, GROUP_POST_FAILED,
    )}
    for r in rows:
        counts[_category_for_row(r)] += 1
    return counts


def _breakdown(rows) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        key = r.extraction_method or "none"
        out[key] = out.get(key, 0) + 1
    return out


def compute_run_summary_row(db: Session, run: AnalysisRun) -> dict:
    """
    PATCH: added total_identified / total_unidentified / total_ready_for_oracle,
    computed via _category_for_row() — the same grouping logic used everywhere
    else (run-detail metrics, ledger tabs, HITL gate). This is now the
    taxonomy the Analysis History run-list table shows, instead of the old
    matched/not_found/pending_hitl trio which couldn't distinguish a row
    that's genuinely ready to post from one stuck needing a remittance or
    a conflict needing SPOC judgment.

      identified        = every row NOT in "unidentified" (i.e. some signal
                           was found — customer and/or invoice — regardless
                           of whether it's fully reconciled yet)
      unidentified       = R8, NO_SIGNAL — nothing extracted at all
      ready_for_oracle   = R9a (exact match) + R9b (acceptable short payment)
                           — the only rows eligible for one-click Approve

    Legacy fields (total_matched, total_not_found, pending_hitl, etc.) are
    KEPT for backward compatibility with any other consumer of this row
    shape (e.g. CSV export, other dashboard widgets) — they're just no
    longer what the Analysis History table itself displays.
    """
    rows = db.query(LineItem).filter(LineItem.run_id == run.run_id).all()
    matched = sum(1 for r in rows if r.is_matched)
    not_found = sum(1 for r in rows if not r.is_matched)

    total_unidentified = 0
    total_ready_for_oracle = 0
    for r in rows:
        cat = _category_for_row(r)
        if cat == GROUP_UNIDENTIFIED:
            total_unidentified += 1
        if cat == GROUP_READY_FOR_ORACLE:
            total_ready_for_oracle += 1
    total_identified = len(rows) - total_unidentified

    return {
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "status": run.status.value if hasattr(run.status, "value") else run.status,
        "selected_files": run.selected_files,
        "bank_names": sorted({r.bank_name for r in rows if r.bank_name}),
        "business_units": sorted({r.business_unit for r in rows if r.business_unit}),
        "total_credit_rows": len(rows),
        # ── New taxonomy — what the Analysis History table now shows ────────
        "total_identified": total_identified,
        "total_unidentified": total_unidentified,
        "total_ready_for_oracle": total_ready_for_oracle,
        # ── Legacy fields — kept for backward compatibility ──────────────────
        "total_matched": matched,
        "total_not_found": not_found,
        "passed_validation": sum(1 for r in rows if r.passed_validation),
        "failed_validation": sum(1 for r in rows if not r.passed_validation and r.is_matched),
        "pending_hitl": sum(1 for r in rows if r.current_state == "review_approve"),
        "approved": sum(1 for r in rows if r.hitl_status == "approved"),
        "rejected": sum(1 for r in rows if r.hitl_status == "rejected"),
        "posted_to_oracle": sum(1 for r in rows if r.reference_status == "success"),  # PATCH: was oracle_post_status — see _category_for_row
        "total_credit_amount": float(sum(r.credit_amount or 0 for r in rows)),
        "match_rate_pct": round(matched / len(rows) * 100, 1) if rows else 0.0,
        "triggered_by": run.triggered_by,
    }


def _serialize_line_item_for_tab(r: LineItem, source: str) -> dict:
    return {
        "id": r.id,
        "run_id": r.run_id,
        "bank_name": r.bank_name,
        "business_unit": r.business_unit,
        "statement_date": r.statement_date.isoformat() if r.statement_date else None,
        "narrative": r.narrative,
        "credit_amount": float(r.credit_amount or 0),
        "statement_currency": r.statement_currency,
        "extracted_customer_name": r.extracted_customer_name,
        "extracted_invoice_number": ",".join(r.extracted_invoice_numbers or []),
        "extraction_method": r.extraction_method,
        "confidence_score": r.confidence_score,
        "matched_customer_name": (r.matched_invoices or [{}])[0].get("customer_name") if r.matched_invoices else None,
        "matched_invoice_number": ",".join(m["invoice_number"] for m in (r.matched_invoices or [])),
        "outstanding_amount": float(r.target_total or 0),
        "invoice_currency": r.statement_currency,
        "is_matched": r.is_matched,
        "passed_validation": r.passed_validation,
        "status": r.status,
        "validation_status": r.reason_code,
        "failed_rules": r.failed_rules,
        "hitl_status": r.hitl_status,
        "oracle_transaction_ref": r.oracle_ref_no,
        "oracle_post_status": r.oracle_post_status,  # receipt-creation status (step 1)
        "reference_status": r.reference_status,        # invoice-mapping status (step 2)
        "_source": source,
        "reason_code": r.reason_code,
        "rule_id": r.rule_id,
        "current_state": r.current_state,
        "shortfall_pct": r.shortfall_pct,
        # PATCH: the precise, unambiguous display group for this row —
        # frontend tabs/filters should key off this field, not current_state.
        "category": _category_for_row(r),
        "category_label": GROUP_LABELS.get(_category_for_row(r), _category_for_row(r)),
    }


def compute_run_summary(db: Session, run_id: int) -> dict:
    """
    PATCH: metrics + tabs are now both keyed by the same 7-group taxonomy
    from _category_for_row(), computed once per row and reused for both.
    This guarantees the KPI card counts and the Line Items Ledger tab
    counts can never disagree with each other.
    """
    rows = db.query(LineItem).filter(LineItem.run_id == run_id).all()

    # Compute each row's group exactly once.
    rows_by_group: dict[str, list[LineItem]] = {
        GROUP_UNIDENTIFIED: [], GROUP_NEEDS_REMITTANCE: [], GROUP_READY_FOR_ORACLE: [],
        GROUP_CONFLICT_EXCEPTION: [], GROUP_PROCESSED: [], GROUP_REJECTED: [], GROUP_POST_FAILED: [],
    }
    for r in rows:
        rows_by_group[_category_for_row(r)].append(r)

    metrics = {
        "total_rows":              len(rows),
        "unidentified":             len(rows_by_group[GROUP_UNIDENTIFIED]),
        "needs_remittance":         len(rows_by_group[GROUP_NEEDS_REMITTANCE]),
        "ready_for_oracle":         len(rows_by_group[GROUP_READY_FOR_ORACLE]),
        "conflict_exception":       len(rows_by_group[GROUP_CONFLICT_EXCEPTION]),
        "processed":                len(rows_by_group[GROUP_PROCESSED]),
        "rejected":                 len(rows_by_group[GROUP_REJECTED]),
        "post_failed":              len(rows_by_group[GROUP_POST_FAILED]),
    }

    # _source on each serialized row controls whether the frontend's row-click
    # navigates to the row-detail page (only rows that were actually
    # identified/matched at extraction time make sense to drill into).
    # "Unidentified" rows have no extraction to inspect — keep source "not_found".
    # Every other group had a real extraction result, so source "matched".
    SOURCE_FOR_GROUP = {
        GROUP_UNIDENTIFIED:       "not_found",
        GROUP_NEEDS_REMITTANCE:   "matched",
        GROUP_READY_FOR_ORACLE:   "matched",
        GROUP_CONFLICT_EXCEPTION: "matched",
        GROUP_PROCESSED:          "matched",
        GROUP_REJECTED:           "matched",
        GROUP_POST_FAILED:        "matched",
    }

    tabs = {
        group: {
            "count": len(rows_by_group[group]),
            "rows": [_serialize_line_item_for_tab(r, SOURCE_FOR_GROUP[group]) for r in rows_by_group[group]],
        }
        for group in rows_by_group
    }

    return {"metrics": metrics, "tabs": tabs}


def get_unidentified_rows(db: Session) -> dict:
    rows = db.query(LineItem).filter(LineItem.current_state == "unidentified").all()
    return {"rows": [_serialize_line_item_for_tab(r, "not_found") for r in rows]}


def get_conflict_rows(db: Session) -> dict:
    """
    PATCH: was matching on (reason_code is not null AND passed_validation=False
    AND is_matched=True) — an approximation that happened to overlap with
    conflict_exception rows but wasn't precise (e.g. it would also catch
    needs_remittance rows, which are is_matched=True, passed_validation=False,
    but NOT a conflict). Now uses _category_for_row() directly.
    """
    rows = db.query(LineItem).all()
    conflict_rows = [r for r in rows if _category_for_row(r) == GROUP_CONFLICT_EXCEPTION]
    return {"rows": [_serialize_line_item_for_tab(r, "matched") for r in conflict_rows]}