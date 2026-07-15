"""
cashapply_shared.settings
==========================
Single source of truth for environment configuration. Both App1 and App2
import this. STORAGE_BACKEND controls storage (local disk vs Azure Blob —
see storage.py); APP_ENV controls the deployment tier and, specifically,
whether the local dev SSO bypass is reachable at all (see auth/bypass.py).
These are independent — a UAT/PROD deployment can run APP_ENV=uat/prod
with STORAGE_BACKEND=local (this app lives on a single VM) while still
requiring real Azure AD SSO tokens.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Storage backend switch ───────────────────────────────────────────────
    # Purely "where do files live" — local disk or Azure Blob. Independent of
    # APP_ENV below: you can run UAT/PROD with local disk storage (this app
    # is a single VM, per design) while still enforcing real Azure AD SSO.
    STORAGE_BACKEND: Literal["local", "azure"] = "local"

    # ── Deployment tier ───────────────────────────────────────────────────────
    # Gates ONE thing: whether the X-Dev-User SSO bypass header (app/auth/bypass.py)
    # is honored at all. Only ever "local" in actual local development — set
    # this to "uat" or "prod" the moment real Azure AD SSO is configured for
    # that environment, even if STORAGE_BACKEND stays "local". Previously a
    # single ENVIRONMENT flag controlled both storage AND the SSO bypass,
    # which meant there was no way to keep local-disk storage while turning
    # off the bypass for a real deployment — this split fixes that.
    APP_ENV: Literal["local", "uat", "prod"] = "local"

    # ── Database (shared by App1 + App2) ────────────────────────────────────
    DATABASE_URL: str = "postgresql+psycopg2://cashapply:cashapply@localhost:5432/cashapply"

    # ── Local storage root (used when STORAGE_BACKEND=local) ────────────────
    LOCAL_STORAGE_ROOT: str = "./storage"

    # ── Azure Blob Storage (used when STORAGE_BACKEND=azure) ────────────────
    AZURE_STORAGE_CONNECTION_STRING: str | None = None
    AZURE_STORAGE_ACCOUNT_URL: str | None = None  # used with AAD/Managed Identity instead of conn string
    AZURE_CONTAINER_BANK_STATEMENTS: str = "bank-statements"
    AZURE_CONTAINER_AGING_REPORTS: str = "aging-reports"
    AZURE_CONTAINER_REMITTANCE_INBOX: str = "remittance-inbox"

    # ── Remittance fetch source (App2) ──────────────────────────────────────
    REMITTANCE_SOURCE: Literal["local_folder", "graph_api"] = "local_folder"
    REMITTANCE_LOCAL_FOLDER: str = "./remittance_inbox_local"

    # ── Remittance recheck worker (App1) ─────────────────────────────────────
    # How often (seconds) the standalone recheck worker
    # (tasks/remittance_recheck_worker.py) re-scans needs_remittance rows
    # against newly-arrived remittances persisted by App2. Independent of
    # App2's own REMITTANCE_POLL_INTERVAL_SECONDS (how often App2 checks the
    # mailbox) — this is how often App1 checks whether any of ITS stuck rows
    # now have a match. Set generously (default 5 min) since this is a
    # full table scan of needs_remittance rows each run.
    REMITTANCE_RECHECK_INTERVAL_SECONDS: int = 300

    # Microsoft Graph (used when REMITTANCE_SOURCE=graph_api)
    GRAPH_TENANT_ID: str | None = None
    GRAPH_CLIENT_ID: str | None = None
    GRAPH_CLIENT_SECRET: str | None = None
    GRAPH_MAILBOX_USER: str | None = None  # e.g. zensar.ar@zensar.com

    # ── Anthropic / Claude (App2) ────────────────────────────────────────────
    # SECURITY: no hardcoded default — must come from .env / real environment.
    # A previous revision shipped a live key as the class default; removed.
    ANTHROPIC_API_KEY: str | None = None
    CLAUDE_MODEL: str = "claude-sonnet-4-6"

    # ── AI usage / cost tracking (app.ai_usage) ──────────────────────────────
    # Per-token USD rates for whatever CLAUDE_MODEL currently points at.
    # Defaults below are Claude Sonnet-class pricing at time of writing —
    # update these (or override in .env) if CLAUDE_MODEL is changed to a
    # different tier, since cost-per-token varies by model and isn't
    # something this app can look up automatically.
    AI_COST_PER_INPUT_TOKEN: float = 0.000003   # $3.00 / million input tokens
    AI_COST_PER_OUTPUT_TOKEN: float = 0.000015  # $15.00 / million output tokens

    # ── Oracle Fusion (App1) ─────────────────────────────────────────────────
    # SECURITY: no hardcoded default — must come from .env / real environment.
    # A previous revision shipped live UAT credentials as class defaults; removed.
    ORACLE_FUSION_BASE_URL: str = "https://fa-etvl-test-saasfaprod1.fa.ocs.oraclecloud.com/fscmRestApi/resources/latest/standardReceipts"
    ORACLE_AUTH_MODE: Literal["basic", "oauth"] = "basic"
    ORACLE_BASIC_USERNAME: str | None = None
    ORACLE_BASIC_PASSWORD: str | None = None
    ORACLE_OAUTH_TOKEN_URL: str | None = None
    ORACLE_OAUTH_CLIENT_ID: str | None = None
    ORACLE_OAUTH_CLIENT_SECRET: str | None = None

    # ── Auth: Microsoft Entra ID (Azure AD) SSO ──────────────────────────────
    AZURE_TENANT_ID: str | None = None
    AZURE_CLIENT_ID: str | None = None          # backend API's App Registration client id (token audience)
    AZURE_JWKS_CACHE_SECONDS: int = 3600

    # Dev/test SSO bypass — see app/auth/bypass.py. Only ever reachable when
    # ENVIRONMENT == "local"; the bypass module is not imported otherwise.
    # Comma-separated list of emails allowed to use the `X-Dev-User` header.
    DEV_SSO_BYPASS_EMAILS: str = ""
    # Role auto-assigned to a brand-new user on first successful SSO login
    # (JIT provisioning). Deliberately the lowest-privilege role, not Admin —
    # see design doc §1.2.
    DEFAULT_NEW_USER_ROLE: str = "Viewer"

    # ── Background task queue (procrastinate — Postgres-backed, no Redis) ───
    # Reuses DATABASE_URL; no separate connection string needed.
    PROCRASTINATE_APP_NAME: str = "cashapply"

    # ── Aging report auto-detection ──────────────────────────────────────────
    AGING_SOURCE: str = "local_folder"              # "local_folder" | "sftp" (future)
    AGING_WATCH_FOLDER: str = "./aging_watch"       # folder scanned for new aging files
    AGING_POLL_INTERVAL_SECONDS: int = 30           # how often to re-scan

    # ── Rule engine tolerances (overridable; mirrors Config screen) ────────
    SHORT_PAYMENT_TOLERANCE_PCT: float = 12.0
    BANK_CHARGE_SPOC_AUTHORITY: float = 50.0
    CUSTOMER_FUZZY_MATCH_MIN_PCT: float = 40.0

    # ── Chunk / threading configuration (extraction pipeline) ───────────────
    # How many credit rows go into one parallel work unit.
    CHUNK_SIZE: int = 15
    # ThreadPoolExecutor size for dispatch_chunks() — how many chunks run
    # concurrently. Each chunk thread makes its own AI calls sequentially,
    # so this is also the ceiling on how many AI network calls are ever
    # in flight across the whole run at once.
    CHUNK_MAX_WORKERS: int = 4

    # ── Layer 2B AI batching configuration ───────────────────────────────────
    # How many unresolved rows (sharing one OU) go into a single Claude
    # prompt. Higher = fewer network round-trips but a bigger prompt and
    # more rows at risk if one batch response is garbled.
    AI_BATCH_SIZE: int = 10
    # How many batches run concurrently INSIDE one chunk's Layer 2B step
    # (nested inside the chunk-level ThreadPoolExecutor). Total AI calls
    # that can be in flight across the whole run at once is roughly
    # CHUNK_MAX_WORKERS * AI_BATCH_MAX_CONCURRENCY — watch your Anthropic
    # org's rate limits before raising both at the same time.
    AI_BATCH_MAX_CONCURRENCY: int = 4


@lru_cache
def get_settings() -> Settings:
    return Settings()