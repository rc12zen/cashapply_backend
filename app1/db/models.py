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
    fx_credit_to_invoice_source    : "oracle_gl" | "static_map" | "spoc_manual"

  Leg 2  invoice → functional  (for Oracle ConversionRate only — we don't apply it)
    is_cross_ledger                : invoice_currency != functional_currency
    fx_invoice_to_functional       : FX rate for Leg 2  (= Oracle ConversionRate)
    fx_invoice_to_functional_source: "oracle_gl" | "static_map" | "spoc_manual"

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
    BigInteger, Boolean, Column, DateTime, Enum, Float, ForeignKey,
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
    #   fx_credit_to_invoice_source "oracle_gl" | "static_map" | "spoc_manual"
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

    # ── Legacy flags (kept for lib/api.ts backward-compatibility) ─────────────
    # DO NOT rename — frontend depends on these exact column names.
    is_matched        = Column(Boolean, default=False)
    passed_validation = Column(Boolean, default=False)
    failed_rules      = Column(Text, nullable=True)
    status            = Column(String, default="Not Found")
    hitl_status       = Column(String, nullable=True)

    # ── Oracle posting result ─────────────────────────────────────────────────
    oracle_ref_no       = Column(String, nullable=True)
    oracle_status_code  = Column(String, nullable=True)
    standard_receipt_id = Column(String, nullable=True)
    oracle_post_status  = Column(String, nullable=True)
    oracle_posted_at    = Column(DateTime, nullable=True)
    post_message        = Column(Text, nullable=True)
    oracle_payload      = Column(JSON, nullable=True)

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
    subject               = Column(String, nullable=True)
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
    Formalizes what is currently free-text ou_number strings scattered across
    SourceFile / LineItem / ou_functional_currency.json. Existing string
    columns are left untouched for backward compatibility; this table is the
    new source of truth going forward and is backfilled from
    rule_engine/configs/ou_functional_currency.json on first migration.
    """
    __tablename__ = "organization_units"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    ou_number           = Column(String(20), unique=True, nullable=False, index=True)
    ou_name             = Column(String(200), nullable=False)
    functional_currency = Column(String(10), nullable=False)
    active               = Column(Boolean, default=True)

    bank_accounts = relationship("BankAccount", back_populates="organization_unit")


class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    ou_id            = Column(Integer, ForeignKey("organization_units.id"), nullable=False)
    account_number   = Column(String(50), nullable=False)
    bank_name        = Column(String(200), nullable=False)
    bank_config_key  = Column(String(100), nullable=True)   # links to bank_ou_mapping.json entries
    currency         = Column(String(10), nullable=True)
    active           = Column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("account_number", "bank_name", name="uq_bank_account_number_name"),)

    organization_unit = relationship("OrganizationUnit", back_populates="bank_accounts")


# ── Users / RBAC ─────────────────────────────────────────────────────────────

class Role(Base):
    __tablename__ = "roles"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(50), unique=True, nullable=False)   # Administrator, Analyst, ...
    description = Column(Text, nullable=True)

    role_permissions = relationship("RolePermission", back_populates="role")
    users            = relationship("User", back_populates="role")


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


class User(Base):
    __tablename__ = "users"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    azure_oid      = Column(String(64), unique=True, nullable=False, index=True)  # Azure Entra object id
    email          = Column(String(320), unique=True, nullable=False, index=True)
    display_name   = Column(String(200), nullable=True)
    role_id        = Column(Integer, ForeignKey("roles.id"), nullable=False)
    is_active      = Column(Boolean, default=True)
    provisioned_at = Column(DateTime, default=dt.datetime.utcnow)
    last_login_at  = Column(DateTime, nullable=True)

    role = relationship("Role", back_populates="users")


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
    entity_id    = Column(String(50), nullable=True)
    status       = Column(String(20), nullable=False, default="success")   # "success" | "failure"
    ip_address   = Column(String(64), nullable=True)
    log_metadata = Column(JSON, nullable=True)
    created_at   = Column(DateTime, default=dt.datetime.utcnow, index=True)

    user = relationship("User")