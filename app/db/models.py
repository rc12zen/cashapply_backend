"""
cashapply_shared.db.models  (PATCHED v2)
=========================================
Shared SQLAlchemy models.

CURRENCY MODEL CHANGE (this revision)
--------------------------------------
LineItem previously tracked one FX leg:
  is_cross_currency = (statement_currency != functional_currency)
  fx_rate           = rate: statement_currency → functional_currency
  fx_rate_source

This was wrong. The correct model has THREE currencies and TWO legs:

  statement_currency  : currency that arrived in the bank row (credited)
  invoice_currency    : currency the invoice was raised in (from aging row)
  functional_currency : OU ledger currency (from ou_functional_currency.json)

  Leg 1  credited → invoice    (for comparison + Oracle Amount)
    is_cross_currency              : statement_currency != invoice_currency
    fx_credit_to_invoice           : FX rate for Leg 1
    fx_credit_to_invoice_source    : "gl_rates_table" | "static_map" | "spoc_manual"

  Leg 2  invoice → functional  (for Oracle ConversionRate only — we don't apply it)
    is_cross_ledger                : invoice_currency != functional_currency
    fx_invoice_to_functional       : FX rate for Leg 2  (= Oracle ConversionRate)
    fx_invoice_to_functional_source: "gl_rates_table" | "static_map" | "spoc_manual"

  is_cross_ou_currency:
    Set True on ready_to_post / acceptable_short_payment rows where the
    customer's aging invoices live in a different OU than the bank account.
    Does NOT require HITL (amounts match) but must be visible on the
    front-end review screen and audit trail.

OLD COLUMNS RETAINED for backward-compatibility during migration:
  fx_rate        → aliased to fx_credit_to_invoice via Python property
  fx_rate_source → aliased to fx_credit_to_invoice_source via Python property
  is_cross_currency now carries the correct meaning (credited != invoice).
    If you were relying on the old meaning (credited != functional) see
    is_cross_ledger instead.

matched_invoices JSON schema (per element):
  {
    "invoice_number":    str,
    "outstanding_amount": float,
    "customer_name":     str,
    "ou_number":         str,
    "invoice_currency":  str,   ← NEW: needed for Oracle ReferenceAmount audit
    "stated_amount":     float, ← always set by rule engine (never None)
    "deduction_amount":  float | null
  }
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Enum, Float, ForeignKey,
    Integer, JSON, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


# ── Enums ────────────────────────────────────────────────────────────────────

class RowState(str, enum.Enum):
    UNIDENTIFIED             = "unidentified"
    NEEDS_REMITTANCE         = "needs_remittance"
    CONFLICT_EXCEPTION       = "conflict_exception"
    ACCEPTABLE_SHORT_PAYMENT = "acceptable_short_payment"
    READY_TO_POST            = "ready_to_post"
    REVIEW_APPROVE           = "review_approve"
    PROCESSED                = "processed"
    REJECTED                 = "rejected"
    POST_FAILED              = "post_failed"


class RunStatus(str, enum.Enum):
    IDLE      = "idle"
    RUNNING   = "running"
    COMPLETED = "completed"
    ERROR     = "error"


# ── App1: Run ────────────────────────────────────────────────────────────────

class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    run_id         = Column(Integer, primary_key=True, autoincrement=True)
    started_at     = Column(DateTime, default=dt.datetime.utcnow)
    completed_at   = Column(DateTime, nullable=True)
    status         = Column(Enum(RunStatus), default=RunStatus.IDLE)
    triggered_by   = Column(String, nullable=True)          # legacy free-text; kept for compat
    selected_files = Column(JSON, default=list)
    error_message  = Column(Text, nullable=True)

    # ── Auth/RBAC integration (additive) ──────────────────────────────────────
    triggered_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # PATCH: which aging report SourceFile was ACTIVE at the moment this run
    # started — set once in run_routes.start_run() and never touched again.
    # Lets Analysis History show "the aging report this run actually matched
    # against" even after a newer aging report has since been loaded and
    # become the active one (see bff/config_routes.py's aging-preview /
    # aging-download, which now accept an optional source_file_id to read
    # this historical snapshot instead of always reading "active").
    # Nullable because existing rows predate this column and runs started
    # before any aging report was ever uploaded have nothing to point at.
    # NOTE: Base.metadata.create_all() only creates missing TABLES, not
    # missing COLUMNS -- an already-deployed DB needs a one-off
    # `ALTER TABLE analysis_runs ADD COLUMN aging_source_file_id INTEGER`
    # (or equivalent) applied manually before this ships to that environment.
    aging_source_file_id = Column(Integer, ForeignKey("source_files.id"), nullable=True)

    line_items = relationship("LineItem", back_populates="run")


# ── App1: Uploaded source files ───────────────────────────────────────────────

class SourceFile(Base):
    __tablename__ = "source_files"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    kind            = Column(String, nullable=False)
    filename        = Column(String, nullable=False)
    storage_key     = Column(String, nullable=False)
    bank_config_key = Column(String, nullable=True)
    ou_number       = Column(String, nullable=True)
    business_unit   = Column(String, nullable=True)
    uploaded_at     = Column(DateTime, default=dt.datetime.utcnow)
    archived        = Column(Boolean, default=False)

    # ── Duplicate detection / auth integration (additive) ─────────────────────
    bank_account_id    = Column(Integer, ForeignKey("bank_accounts.id"), nullable=True)
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    file_hash           = Column(String(64), nullable=True, index=True)
    # "processing" | "ready" | "error" — set by the background ingestion job (§4 of design doc)
    ingest_status        = Column(String, nullable=True)
    ingest_error         = Column(Text, nullable=True)
    new_row_count        = Column(Integer, nullable=True)
    duplicate_row_count  = Column(Integer, nullable=True)


# ── App1: Aging snapshot ──────────────────────────────────────────────────────

class AgingInvoice(Base):
    """
    One row per OPEN invoice from the latest Aging Report upload.
    Truncated + reloaded on every aging upload.
    """
    __tablename__ = "aging_invoices"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    customer_number    = Column(String, index=True)
    customer_name      = Column(String, index=True)
    invoice_number     = Column(String, index=True, nullable=False)
    invoice_type       = Column(String)
    invoice_amount     = Column(Numeric(18, 2))
    invoice_currency   = Column(String(10))
    outstanding_amount = Column(Numeric(18, 2))
    invoice_date       = Column(DateTime, nullable=True)
    due_date           = Column(DateTime, nullable=True)
    ou_number          = Column(String, index=True)
    source_file_id     = Column(Integer, ForeignKey("source_files.id"), nullable=True)

    __table_args__ = (UniqueConstraint("invoice_number", "ou_number", name="uq_invoice_ou"),)


# ── App1: Line item ───────────────────────────────────────────────────────────

class LineItem(Base):
    __tablename__ = "line_items"

    id     = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("analysis_runs.run_id"), index=True)

    # ── Raw bank statement fields ─────────────────────────────────────────────
    bank_name                 = Column(String)
    account_number            = Column(String)
    business_unit             = Column(String)
    ou_number                 = Column(String)
    statement_date            = Column(DateTime, nullable=True)
    narrative                 = Column(Text)
    credit_amount             = Column(Numeric(18, 2))
    bank_reference            = Column(String, nullable=True)
    customer_reference_number = Column(String, nullable=True)

    # ── Three-currency model ──────────────────────────────────────────────────
    #
    # statement_currency  = what arrived in the bank (credited currency)
    # invoice_currency    = what the invoice was raised in (from aging row)
    # functional_currency = OU ledger currency
    #
    # Leg 1: credited → invoice
    #   is_cross_currency   True when statement_currency != invoice_currency
    #   fx_credit_to_invoice        FX rate for Leg 1 conversion
    #   fx_credit_to_invoice_source "gl_rates_table" | "static_map" | "spoc_manual"
    #
    # Leg 2: invoice → functional  (Oracle's own conversion — we only store the rate)
    #   is_cross_ledger                True when invoice_currency != functional_currency
    #   fx_invoice_to_functional       FX rate passed to Oracle as ConversionRate
    #   fx_invoice_to_functional_source

    statement_currency              = Column(String(10))           # credited currency
    invoice_currency                = Column(String(10), nullable=True)  # from aging row; None until Pass 2
    functional_currency             = Column(String(10), nullable=True)  # OU ledger currency

    # Leg 1
    is_cross_currency               = Column(Boolean, default=False)     # credited != invoice
    fx_credit_to_invoice            = Column(Float, nullable=True)
    fx_credit_to_invoice_source     = Column(String, nullable=True)

    # Leg 2
    is_cross_ledger                 = Column(Boolean, default=False)     # invoice != functional
    fx_invoice_to_functional        = Column(Float, nullable=True)       # = Oracle ConversionRate
    fx_invoice_to_functional_source = Column(String, nullable=True)

    # ── Problem 2: cross-OU business flag ────────────────────────────────────
    # True when the row reaches ready_to_post / acceptable_short_payment AND
    # the customer's aging invoices are in a different OU than the bank account.
    # Visible on the front-end review screen; recorded in the audit trail.
    # Does NOT require HITL — amounts matched — but finance needs visibility.
    is_cross_ou_currency            = Column(Boolean, default=False)
    # Evidence behind the is_cross_ou_currency decision above -- which OU(s)
    # the bank account belongs to, and for each OU where the customer was
    # found, the exact matched name / fuzzy score / outstanding amount (see
    # rule_engine/ou_resolver.py::OUResolverResult.customer_ou_details).
    # Persisted (not recomputed live against today's aging map) so the Row
    # Detail page shows what was ACTUALLY true when this row was evaluated,
    # same principle as every other historical field on this row. Shape:
    #   {"bank_ou_numbers": ["111"], "customer_ou_details": [...]}
    ou_evidence                      = Column(JSON, nullable=True)

    # ── Extraction layer output ───────────────────────────────────────────────
    extracted_customer_name   = Column(String, nullable=True)
    extracted_invoice_numbers = Column(JSON, default=list)
    extraction_method         = Column(String, nullable=True)
    customer_match_pct        = Column(Float, nullable=True)
    invoice_match_pct         = Column(Float, nullable=True)
    confidence_score          = Column(Float, nullable=True)

    # ── Remittance lookup result ──────────────────────────────────────────────
    remittance_extraction_id    = Column(Integer, nullable=True)
    remittance_matched_invoices = Column(JSON, default=list)

    # ── Rule engine output ────────────────────────────────────────────────────
    # matched_invoices JSON schema per element:
    #   invoice_number    : str
    #   outstanding_amount: float   (in invoice_currency)
    #   customer_name     : str
    #   ou_number         : str
    #   invoice_currency  : str     ← included from this patch onward
    #   stated_amount     : float   (in invoice_currency, always set, never None)
    #   deduction_amount  : float | null
    matched_invoices = Column(JSON, default=list)
    target_total     = Column(Numeric(18, 2), nullable=True)   # sum of outstanding (invoice ccy)
    shortfall_pct    = Column(Float, nullable=True)

    # ── State machine ─────────────────────────────────────────────────────────
    current_state = Column(Enum(RowState), default=RowState.UNIDENTIFIED, index=True)
    reason_code   = Column(String, nullable=True)
    rule_id       = Column(String, nullable=True)

    # ── Manual invoice mapping tracking ───────────────────────────────────────
    # Set by hitl/manual_mapping.py's confirm_manual_mapping() — the only
    # persistent record of "this row's CURRENT matched_invoices/rule_id came
    # from a SPOC hand-picking an invoice, not automatic AI/regex extraction
    # or an automatic aging match." Previously the only trace of this was a
    # RowStatusHistory row (trigger="manual_mapping") — a historical log
    # entry, not something the row-detail page could read back to know
    # "is this ALREADY manually mapped" versus "does this need mapping."
    # That gap is exactly why the Manual Invoice Mapping card kept showing
    # the same blank picker even after a successful confirm — nothing on
    # the row recorded that a mapping had already happened.
    manually_mapped    = Column(Boolean, default=False)
    manually_mapped_at = Column(DateTime, nullable=True)
    manually_mapped_by = Column(String, nullable=True)  # SPOC email

    # ── Customer-name correction tracking ─────────────────────────────────────
    # Set by rule_engine/customer_name_correction.py's correct_customer_name()
    # — the only persistent record of "a human overrode the AI's own
    # extracted_customer_name because it was wrong", distinct from
    # manually_mapped above (that one is about hand-picking an INVOICE;
    # this one is about correcting the CUSTOMER the AI thought it saw).
    # ai_extracted_customer_name preserves what the AI originally said,
    # exactly once (never overwritten by a second correction), so the
    # original AI guess is never lost even after a human fixes it.
    customer_name_corrected    = Column(Boolean, default=False)
    customer_name_corrected_at = Column(DateTime, nullable=True)
    customer_name_corrected_by = Column(String, nullable=True)  # SPOC email
    ai_extracted_customer_name = Column(String, nullable=True)

    # ── Legacy flags (kept for lib/api.ts backward-compatibility) ─────────────
    # DO NOT rename — frontend depends on these exact column names.
    is_matched        = Column(Boolean, default=False)
    passed_validation = Column(Boolean, default=False)
    failed_rules      = Column(Text, nullable=True)
    status            = Column(String, default="Not Found")
    hitl_status       = Column(String, nullable=True)

    # ── Oracle RECEIPT CREATION (step 1 — Bank Reconciliation stage) ─────────
    # PATCH: these fields used to be written once, at SPOC-approval time,
    # meaning "fully approved AND invoice-mapped". They're now written
    # much earlier — right after the analysis run categorizes this row,
    # for EVERY credit row regardless of category — and mean only "a bare
    # Oracle receipt exists for this row" (see rule_engine/orchestrator.py's
    # Step 4.5, and oracle/fusion_client.py's build_receipt_creation_payload,
    # which deliberately omits remittanceReferences). standard_receipt_id
    # is Oracle's own numeric StandardReceiptId — required to address the
    # child remittanceReferences collection later at invoice-mapping time.
    oracle_ref_no        = Column(String, nullable=True)   # Oracle's ReceiptNumber (our own generated string)
    oracle_status_code   = Column(String, nullable=True)
    standard_receipt_id  = Column(String, nullable=True)   # Oracle's numeric StandardReceiptId
    oracle_post_status   = Column(String, nullable=True)   # "success" | "failed" — RECEIPT CREATION outcome only
    oracle_posted_at     = Column(DateTime, nullable=True)
    post_message         = Column(Text, nullable=True)
    oracle_payload       = Column(JSON, nullable=True)      # last receipt-creation request body sent
    oracle_response_raw  = Column(JSON, nullable=True)      # raw Oracle response body from receipt creation — was discarded before, needed for row-detail display

    # ── Oracle INVOICE MAPPING / reference (step 2 — Finance Approval) ───────
    # Separate from the fields above on purpose: this is what used to be
    # the ONLY Oracle interaction, gated on ready_for_oracle (R9a/R9b) and
    # triggered by SPOC approve. Now it's a POST to
    # /standardReceipts/{standard_receipt_id}/child/remittanceReferences
    # against the receipt already created in step 1, not a new receipt.
    # "Processed"/"Posted to Oracle" downstream (dashboard KPI, Executive
    # Summary, Shortage Review) now means reference_status == "success",
    # NOT oracle_post_status == "success" — a bare receipt with no invoice
    # mapping yet is not "done".
    reference_status     = Column(String, nullable=True)   # "success" | "failed"
    reference_added_at   = Column(DateTime, nullable=True)
    reference_message    = Column(Text, nullable=True)
    reference_payload    = Column(JSON, nullable=True)       # last remittanceReferences request body sent
    reference_response_raw = Column(JSON, nullable=True)     # raw Oracle response(s) from invoice mapping

    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    # ── Optimistic locking (Perf §9) — bump on every HITL approve/reject write
    # so two SPOCs acting on the same row concurrently get a conflict instead
    # of a silent overwrite. See hitl/service.py.
    version = Column(Integer, default=0, nullable=False)

    run            = relationship("AnalysisRun", back_populates="line_items")
    status_history = relationship("RowStatusHistory", back_populates="line_item")

    # ── Backward-compat properties for any code still reading .fx_rate ────────
    # Remove after all callers are migrated to the new field names.
    @property
    def fx_rate(self) -> float | None:
        """Deprecated. Use fx_credit_to_invoice instead."""
        return self.fx_credit_to_invoice

    @fx_rate.setter
    def fx_rate(self, value: float | None) -> None:
        """Deprecated. Use fx_credit_to_invoice instead."""
        self.fx_credit_to_invoice = value

    @property
    def fx_rate_source(self) -> str | None:
        """Deprecated. Use fx_credit_to_invoice_source instead."""
        return self.fx_credit_to_invoice_source

    @fx_rate_source.setter
    def fx_rate_source(self, value: str | None) -> None:
        """Deprecated. Use fx_credit_to_invoice_source instead."""
        self.fx_credit_to_invoice_source = value


# ── HITL row actions (data-driven) ────────────────────────────────────────────

class ActionDefinition(Base):
    """
    One ROW-LEVEL action a SPOC/Oracle Operator/Analyst can potentially take
    on a LineItem (Approve, Reject, Map Invoice, Recheck Remittance, Retry
    Oracle, and any future ones) — seed-defined here rather than hardcoded
    in the frontend, so "which actions can I do on THIS row" is answered
    once, server-side, from state + permission, instead of drifting across
    frontend JSX conditions and backend gates independently over time. See
    hitl/actions_registry.py for how this is resolved into an actual
    per-row list, and scripts/seed_actions.py for the seeded rows.

    `applicable_categories`: JSON list of the row categories (see
    bff/metrics.py's GROUP_* constants) this action is offered for. NULL/
    empty list = every category (subject to `condition_key` below).

    `condition_key`: an extra, code-level eligibility check beyond category
    — e.g. "not_rejected" for Reject, "reference_status_failed" for Retry.
    This is intentionally NOT free-form data (business logic isn't safe to
    store as an arbitrary expression) — it's a fixed key that
    hitl/actions_registry.py's CONDITION_CHECKS dict knows how to evaluate.
    NULL = no extra condition beyond the category match.

    `permission_code`: the permission a user must hold for this action to
    even be considered (checked via auth/permissions.py, same as every
    other permission check in the app).
    """
    __tablename__ = "action_definitions"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    code                   = Column(String(50), unique=True, nullable=False)   # "approve", "reject", ...
    label                  = Column(String(100), nullable=False)               # "Approve & Post"
    icon                   = Column(String(50), nullable=True)                # frontend lucide-react icon key
    permission_code        = Column(String(100), nullable=False)
    applicable_categories  = Column(JSON, nullable=True)                      # None/[] = any category
    condition_key          = Column(String(50), nullable=True)
    confirm_required       = Column(Boolean, default=False)
    is_danger              = Column(Boolean, default=False)                   # red/destructive styling
    sort_order             = Column(Integer, default=0)
    is_active              = Column(Boolean, default=True)                    # disable without a deploy


# ── App1: Audit trail ─────────────────────────────────────────────────────────

class RowStatusHistory(Base):
    __tablename__ = "row_status_history"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    line_item_id = Column(BigInteger, ForeignKey("line_items.id"), index=True)
    from_state   = Column(String, nullable=True)
    to_state     = Column(String, nullable=False)
    trigger      = Column(String, nullable=False)
    rule_id      = Column(String, nullable=True)
    triggered_by = Column(String, nullable=True)
    comment      = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=dt.datetime.utcnow)

    line_item = relationship("LineItem", back_populates="status_history")


# ── App1: Rule definitions ────────────────────────────────────────────────────

class AiUsageLog(Base):
    """
    One row per AI call made during Layer 2B extraction (see
    extraction/ai_providers.py / extraction/layer_2b_ai.py) — either
    Anthropic Claude or OpenAI, whichever Settings.AI_PROVIDER pointed at
    when the call happened. Model + the per-token rate for THAT provider
    are read from Settings at call time, and the actual rate used is
    stored alongside the token counts on each row, so historical cost
    figures stay accurate even if pricing changes later or AI_PROVIDER is
    switched.
    """
    __tablename__ = "ai_usage_logs"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id         = Column(Integer, ForeignKey("analysis_runs.run_id"), index=True, nullable=True)
    call_type      = Column(String(20), nullable=False)   # "batch" | "single" (see layer_2b_ai.py)
    batch_ref      = Column(String(50), nullable=True)     # e.g. OU/chunk ref, for debugging
    model          = Column(String(100), nullable=False)   # Settings.CLAUDE_MODEL at call time
    input_tokens   = Column(Integer, nullable=False, default=0)
    output_tokens  = Column(Integer, nullable=False, default=0)
    cost_per_input_token  = Column(Float, nullable=False)  # rate actually applied, not just current setting
    cost_per_output_token = Column(Float, nullable=False)
    cost_usd       = Column(Float, nullable=False, default=0.0)
    latency_ms     = Column(Integer, nullable=True)
    succeeded      = Column(Boolean, nullable=False, default=True)
    created_at     = Column(DateTime, default=dt.datetime.utcnow, index=True)


class RuleDefinition(Base):
    __tablename__ = "rule_definitions"

    rule_id               = Column(String, primary_key=True)
    order_index           = Column(Integer, nullable=False)
    condition_expr        = Column(Text, nullable=False)
    reason_code           = Column(String, nullable=False)
    target_category       = Column(String, nullable=False)
    auto_approve_eligible = Column(Boolean, default=False)
    active                = Column(Boolean, default=True)
    description           = Column(Text, nullable=True)


# ── App1: Config ──────────────────────────────────────────────────────────────

class AppConfig(Base):
    __tablename__ = "app_config"

    key   = Column(String, primary_key=True)
    value = Column(String, nullable=False)


# ── App2 (App1 only reads) ────────────────────────────────────────────────────

class RemittanceExtraction(Base):
    __tablename__ = "remittance_extractions"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    source                = Column(String)
    storage_key           = Column(String)
    filename              = Column(String, nullable=True)   # original filename, before storage_key's timestamp prefix
    subject               = Column(String, nullable=True)
    sender                = Column(String, nullable=True)   # best-effort sender address/name (.msg only)
    raw_customer_text     = Column(String, nullable=True)
    raw_payer_text        = Column(String, nullable=True)
    payment_reference     = Column(String, nullable=True)
    payment_date          = Column(DateTime, nullable=True)
    payment_currency      = Column(String(10), nullable=True)
    payment_amount        = Column(Numeric(18, 2), nullable=True)
    bank_account_hint     = Column(String, nullable=True)
    extraction_confidence = Column(Float, nullable=True)
    extracted_at          = Column(DateTime, default=dt.datetime.utcnow)
    raw_model_output      = Column(JSON, nullable=True)
    # PATCH: full extracted email/document body text, written by App2
    # (cashapply-remittance-agent). Row-detail's remittance panel "Raw" tab
    # reads this as raw_body — see bff/row_detail.py.
    raw_text              = Column(Text, nullable=True)

    invoice_lines = relationship("RemittanceInvoiceLine", back_populates="extraction")


class RemittanceInvoiceLine(Base):
    __tablename__ = "remittance_invoice_lines"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    extraction_id    = Column(Integer, ForeignKey("remittance_extractions.id"), index=True)
    invoice_number   = Column(String, index=True)
    document_date    = Column(DateTime, nullable=True)
    document_amount  = Column(Numeric(18, 2), nullable=True)
    document_currency = Column(String(10), nullable=True)
    amount_withheld  = Column(Numeric(18, 2), nullable=True)
    discount_taken   = Column(Numeric(18, 2), nullable=True)
    amount_paid      = Column(Numeric(18, 2), nullable=True)
    line_confidence  = Column(Float, nullable=True)

    extraction = relationship("RemittanceExtraction", back_populates="invoice_lines")


# ═══════════════════════════════════════════════════════════════════════════
# Auth / RBAC / Duplicate-Detection / Audit-Logging layer
# See: cashapply-platform-hardening-design.md for the full design rationale.
# All tables below are additive — nothing above this line changes behavior.
# ═══════════════════════════════════════════════════════════════════════════

# ── Organization structure ──────────────────────────────────────────────────

class OrganizationUnit(Base):
    """
    The single source of truth for OU + Business Unit data. Populated by
    the Config Builder wizard at account onboarding time (OU + Business
    Unit are required fields there) — no JSON file, no migration; this
    table and BankAccount.ou_id together are the only place OU/BU data
    lives.
    """
    __tablename__ = "organization_units"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    ou_number           = Column(String(20), unique=True, nullable=False, index=True)
    ou_name             = Column(String(200), nullable=False)
    functional_currency = Column(String(10), nullable=False)
    active               = Column(Boolean, default=True)

    bank_accounts = relationship("BankAccount", back_populates="organization_unit")


class BankAccount(Base):
    """
    The account "header" — one row per onboarded bank account. Recipes
    (formerly account_configs.json's recipes[format]) live in
    AccountConfigRecipe, versioned and FK'd here. OU + Business Unit are a
    real relationship via ou_id -> OrganizationUnit, not free-text columns
    copied into a side JSON file — this table + OrganizationUnit together
    are now the single source of truth for OU/BU, replacing
    account_configs.json + bank_ou_mapping.json + account_ou_map.json.

    MULTI-BU: most accounts belong to exactly one Business Unit — that's
    `ou_id`/`organization_unit` below, unchanged. Some accounts legitimately
    receive payments for MORE than one Business Unit though (see
    BankAccountOU) — `additional_ous` holds those. `all_organization_units`
    returns the full set (primary first). See rule_engine/ou_resolver.py
    for how the full set is used for cross-OU detection, and
    bff/bank_accounts_routes.py for the admin-facing "change Business
    Unit(s)" endpoint — changing this only affects NEW analysis runs (see
    rule_engine/orchestrator.py's live re-resolution at run start); already-
    completed runs keep the Business Unit that was in effect when they ran,
    since LineItem.business_unit is a permanent snapshot, not a live join.
    """
    __tablename__ = "bank_accounts"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    ou_id            = Column(Integer, ForeignKey("organization_units.id"), nullable=False)
    account_number   = Column(String(50), nullable=False)
    account_last4    = Column(String(4), nullable=True, index=True)
    display_name     = Column(String(200), nullable=True)
    bank_name        = Column(String(200), nullable=False)
    bank_config_key  = Column(String(100), nullable=True)   # legacy key, kept for display/back-compat only
    currency         = Column(String(10), nullable=True)
    active           = Column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("account_number", "bank_name", name="uq_bank_account_number_name"),)

    organization_unit = relationship("OrganizationUnit", back_populates="bank_accounts")
    additional_ou_links = relationship("BankAccountOU", back_populates="bank_account",
                                        cascade="all, delete-orphan")
    additional_ous = relationship("OrganizationUnit", secondary="bank_account_ous", viewonly=True)
    recipes           = relationship("AccountConfigRecipe", back_populates="bank_account",
                                      order_by="AccountConfigRecipe.version")

    @property
    def all_organization_units(self) -> list["OrganizationUnit"]:
        """Primary OU first, then any additional ones (de-duplicated) — see
        the class docstring above and rule_engine/ou_resolver.py, which
        uses this full set for cross-OU detection on multi-BU accounts."""
        seen = {self.organization_unit.id} if self.organization_unit else set()
        result = [self.organization_unit] if self.organization_unit else []
        for ou in self.additional_ous:
            if ou.id not in seen:
                seen.add(ou.id)
                result.append(ou)
        return result

    @property
    def all_ou_numbers(self) -> list[str]:
        return [ou.ou_number for ou in self.all_organization_units]


class GlDailyRate(Base):
    """
    Oracle GL Daily Rates — loaded from a file, NOT a REST call.

    There is no live Oracle GL Daily Rates REST API in this environment.
    Finance drops a GL Daily Rates extract (.xlsx/.xls/.csv — same shape as
    the Zensar_GL_Daily_Rates_Extract used to build
    rule_engine/configs/fx_conversion_type_map.json) into a watched folder,
    exactly the way aging reports arrive — see gl_rates/watcher.py, the
    sibling of aging/watcher.py. That file is parsed and its rows UPSERTED
    into this table (unlike the aging report, which stays in-memory only —
    rate history needs to persist and accumulate across files, not be
    replaced wholesale by the latest one).

    rule_engine/fx_service.py.FxService._fetch_from_gl_rates_table() reads
    FROM THIS TABLE (not a REST endpoint) for a given
    (from_currency, to_currency, conversion_date, conversion_rate_type) —
    see that method's docstring for the exact-date-then-nearest-prior-date
    lookup order.

    One row = one (pair, date, rate type) — the natural key mirrors what
    Oracle's own GL_DAILY_RATES table looks like, so multiple files
    (e.g. a new day's rates each morning) ADD rows rather than replace the
    whole table; re-loading the same file re-upserts the same rows
    (idempotent on the unique constraint below).
    """
    __tablename__ = "gl_daily_rates"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    from_currency         = Column(String(10), nullable=False, index=True)
    to_currency           = Column(String(10), nullable=False, index=True)
    conversion_date       = Column(Date, nullable=False, index=True)
    conversion_rate_type  = Column(String(50), nullable=False)          # e.g. "MRC Daily", "Spot"
    conversion_rate       = Column(Numeric(24, 10), nullable=False)     # 1 from_currency = ? to_currency
    source_filename       = Column(String(300), nullable=True)          # which uploaded file this row came from
    loaded_at             = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "from_currency", "to_currency", "conversion_date", "conversion_rate_type",
            name="uq_gl_daily_rate_pair_date_type",
        ),
    )


class BankAccountOU(Base):
    """Join row for a bank account's ADDITIONAL Business Units, beyond its
    primary one (BankAccount.ou_id). Most accounts have zero rows here (one
    account = one BU, via the primary FK alone) — this table exists only
    for the "one bank account receives payments for multiple Business
    Units" case. See BankAccount.all_organization_units /
    bff/bank_accounts_routes.py."""
    __tablename__ = "bank_account_ous"

    bank_account_id = Column(Integer, ForeignKey("bank_accounts.id"), primary_key=True)
    ou_id           = Column(Integer, ForeignKey("organization_units.id"), primary_key=True)

    bank_account = relationship("BankAccount", back_populates="additional_ou_links")
    organization_unit = relationship("OrganizationUnit")


class AccountConfigRecipe(Base):
    """
    Replaces account_configs.json's recipes[format] = [ {version, created_at,
    created_by, recipe} ]. Same append-only versioning: a new save for an
    existing (bank_account_id, format) always inserts the next version,
    never overwrites a prior one. The recipe body itself (account_locator +
    source + fields + credit_rule + ...) stays a single JSON column — same
    shape the parser/detector already expect, least rework vs normalizing it.
    """
    __tablename__ = "account_config_recipes"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    bank_account_id = Column(Integer, ForeignKey("bank_accounts.id"), nullable=False, index=True)
    format          = Column(String(10), nullable=False)   # xlsx | xls | csv | pdf
    version         = Column(Integer, nullable=False)
    recipe          = Column(JSON, nullable=False)
    created_at      = Column(DateTime, default=dt.datetime.utcnow)
    created_by      = Column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("bank_account_id", "format", "version", name="uq_recipe_account_format_version"),
    )

    bank_account = relationship("BankAccount", back_populates="recipes")


# ── Users / RBAC ─────────────────────────────────────────────────────────────

class Role(Base):
    __tablename__ = "roles"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(50), unique=True, nullable=False)   # Administrator, Analyst, ...
    description = Column(Text, nullable=True)

    role_permissions = relationship("RolePermission", back_populates="role")
    user_roles        = relationship("UserRole", back_populates="role")


class Permission(Base):
    __tablename__ = "permissions"

    id   = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(100), unique=True, nullable=False)   # e.g. "statement:upload", "oracle:post"

    role_permissions = relationship("RolePermission", back_populates="permission")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id       = Column(Integer, ForeignKey("roles.id"), primary_key=True)
    permission_id = Column(Integer, ForeignKey("permissions.id"), primary_key=True)

    role       = relationship("Role", back_populates="role_permissions")
    permission = relationship("Permission", back_populates="role_permissions")


class UserRole(Base):
    """Join row for the User <-> Role many-to-many relationship. An
    Administrator can assign a user ANY NUMBER of roles at once (e.g. both
    Analyst and Oracle Operator) — the user's effective permission set is
    the UNION of every assigned role's permissions (see
    auth/permissions.py::user_has_permission). Replaces the earlier
    single User.role_id FK."""
    __tablename__ = "user_roles"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id"), primary_key=True)

    user = relationship("User", back_populates="user_roles")
    role = relationship("Role", back_populates="user_roles")


class User(Base):
    __tablename__ = "users"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    azure_oid      = Column(String(64), unique=True, nullable=False, index=True)  # Azure Entra object id
    email          = Column(String(320), unique=True, nullable=False, index=True)
    display_name   = Column(String(200), nullable=True)
    is_active      = Column(Boolean, default=True)
    provisioned_at = Column(DateTime, default=dt.datetime.utcnow)
    last_login_at  = Column(DateTime, nullable=True)

    user_roles = relationship("UserRole", back_populates="user", cascade="all, delete-orphan")
    # Convenience read-only view of the assigned Role objects (in id order,
    # not any semantic "priority" order — see auth/role_priority.py for
    # display/label ordering). Assign roles via UserRole rows, not this
    # association_proxy directly (see bff/admin_routes.py::_set_user_roles).
    roles = relationship(
        "Role",
        secondary="user_roles",
        viewonly=True,
        order_by="Role.id",
    )

    @property
    def role_names(self) -> list[str]:
        return [r.name for r in self.roles]


# ── Duplicate detection ───────────────────────────────────────────────────────

class StatementFileHash(Base):
    """Exact-duplicate-file ledger. See design doc §2.1."""
    __tablename__ = "statement_file_hashes"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    file_hash       = Column(String(64), unique=True, nullable=False, index=True)  # SHA-256 hex
    source_file_id  = Column(Integer, ForeignKey("source_files.id"), nullable=False)
    uploaded_by     = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at     = Column(DateTime, default=dt.datetime.utcnow)


class StatementTransactionRow(Base):
    """
    Durable, hash-deduplicated ledger of every ingested bank statement row —
    decoupled from any single AnalysisRun. A Run consumes only rows where
    consumed_by_run_id IS NULL. See design doc §0 and §2.2.
    """
    __tablename__ = "statement_transaction_rows"

    id                  = Column(BigInteger, primary_key=True, autoincrement=True)
    source_file_id      = Column(Integer, ForeignKey("source_files.id"), nullable=False)
    bank_account_id     = Column(Integer, ForeignKey("bank_accounts.id"), nullable=True)
    row_hash             = Column(String(64), nullable=False)
    statement_date        = Column(DateTime, nullable=True)
    credit_amount          = Column(Numeric(18, 2), nullable=True)
    currency                = Column(String(10), nullable=True)
    narrative                = Column(Text, nullable=True)
    bank_reference            = Column(String, nullable=True)
    raw_row_json               = Column(JSON, nullable=True)   # original parsed row, for audit/replay
    ingested_at                 = Column(DateTime, default=dt.datetime.utcnow)
    consumed_by_run_id           = Column(Integer, ForeignKey("analysis_runs.run_id"), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("bank_account_id", "row_hash", name="uq_statement_row_account_hash"),
    )


# ── Audit log ──────────────────────────────────────────────────────────────────

class ActivityLog(Base):
    """
    Append-only audit trail. See design doc §6. Not partitioned at the ORM
    level (create_all() gives a single table for local/PoC use) — apply the
    monthly range-partitioning DDL from the design doc via a proper Alembic
    migration before relying on this at high volume in production.
    """
    __tablename__ = "activity_logs"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)  # null = system/background action
    action       = Column(String(100), nullable=False)                     # "statement.upload", "run.start", ...
    entity_type  = Column(String(50), nullable=True)                       # "SourceFile", "AnalysisRun", ...
    entity_id    = Column(String(255), nullable=True)  # widened from 50 — statement.delete
                                                          # logs the whole FILENAME here, not
                                                          # just a small integer ID like every
                                                          # other caller, and filenames routinely
                                                          # exceed 50 chars
    status       = Column(String(20), nullable=False, default="success")   # "success" | "failure"
    ip_address   = Column(String(64), nullable=True)
    log_metadata = Column(JSON, nullable=True)
    created_at   = Column(DateTime, default=dt.datetime.utcnow, index=True)

    user = relationship("User")