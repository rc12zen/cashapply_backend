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

from pydantic import field_validator
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

    # ── OpenAI (direct — platform.openai.com) ────────────────────────────────
    # SECURITY: no hardcoded default — must come from .env / real environment.
    OPENAI_API_KEY: str | None = None
    # Exact model string as OpenAI's API expects it -- confirm the current
    # name in your OpenAI account/docs before relying on this default, model
    # naming for the GPT-5 family has changed since this was written.
    OPENAI_MODEL: str = "gpt-5-mini"

    # ── Azure OpenAI Service (a model deployed into your own Azure tenant's
    # resource group -- NOT the same product as direct OpenAI above, and uses
    # a different connection shape. Confirmed against Azure AI Foundry's own
    # sample code for a real deployment: the plain OpenAI client pointed at
    # a custom base_url ending in /openai/v1 (Azure's newer unified
    # endpoint), NOT the older AzureOpenAI class + api_version parameter.
    # Use this AI_PROVIDER value when the model lives in your own Azure
    # OpenAI resource rather than platform.openai.com. See
    # extraction/ai_providers.py's Azure branch -- it also uses the
    # Responses API (client.responses.create), not chat.completions, to
    # match that same confirmed-working sample.
    AZURE_OPENAI_API_KEY: str | None = None
    # The resource's OWN endpoint as shown on its "Keys and Endpoint" page,
    # e.g. https://<your-resource-name>.services.ai.azure.com -- the code
    # appends /openai/v1 itself, don't include that here.
    AZURE_OPENAI_ENDPOINT: str | None = None
    # The DEPLOYMENT NAME chosen when the model was deployed in Azure AI
    # Foundry -- confirmed to often just be the model name itself by
    # default (e.g. deployment "gpt-5.4-mini" running the "gpt-5.4-mini"
    # model) -- get this from the Deployments page, not guessed.
    AZURE_OPENAI_DEPLOYMENT: str | None = None

    # ── AI provider switch (extraction Layer 2B — extraction/ai_providers.py) ──
    # Which provider Layer 2B's AI fallback actually calls. Switching this is
    # the ONLY change needed to move providers -- no code change. Set the
    # matching *_API_KEY (+ endpoint/deployment for azure_openai) above for
    # whichever provider you pick.
    AI_PROVIDER: Literal["anthropic", "openai", "azure_openai"] = "anthropic"

    # ── AI extraction master switch (.env: AI_EXTRACTION_ENABLED) ─────────────
    # Turns the Layer 2B AI fallback pass ON/OFF entirely. Default True. Set to
    # false in local dev to avoid spending provider tokens while working on
    # unrelated things: the pipeline still runs (regex/pattern matching only),
    # unresolved rows just land as "unidentified" instead of getting the AI
    # second pass. When false, get_ai_status() reports a neutral "disabled"
    # state WITHOUT pinging the provider (no valid key needed locally), and the
    # frontend gate treats that as "allowed" (upload/analyse proceed) rather
    # than an outage. See extraction/ai_providers.py get_ai_status() and
    # extraction/layer_2b_ai.py run_layer_2b(), which both check this flag.
    AI_EXTRACTION_ENABLED: bool = True

    # ── AI usage / cost tracking (app.ai_usage) ──────────────────────────────
    # Per-token USD rates -- ONE pair per provider family, since pricing
    # differs. azure_openai reuses the OPENAI_* rates below (Azure OpenAI's
    # per-token pricing for the same underlying model is typically close to
    # direct OpenAI's, though your actual negotiated/regional rate may
    # differ -- override these two if your Azure agreement's pricing is
    # different). The pair actually applied is chosen by whichever provider
    # made that specific call (see ai_usage/tracker.py), not by AI_PROVIDER's
    # CURRENT value -- so historical totals stay correct even after
    # switching providers or changing prices.
    AI_COST_PER_INPUT_TOKEN: float = 0.000003   # Claude Sonnet-class: $3.00 / million input tokens
    AI_COST_PER_OUTPUT_TOKEN: float = 0.000015  # Claude Sonnet-class: $15.00 / million output tokens
    OPENAI_COST_PER_INPUT_TOKEN: float = 0.00000025   # GPT-5 mini-class: $0.25 / million input tokens
    OPENAI_COST_PER_OUTPUT_TOKEN: float = 0.000002    # GPT-5 mini-class: $2.00 / million output tokens

    # ── Oracle Fusion (App1) ─────────────────────────────────────────────────
    # SECURITY: no hardcoded default — must come from .env / real environment.
    # A previous revision shipped live UAT credentials as class defaults; removed.
    # PATCH: this used to be
    # "...resources/latest/standardReceipts" -- with the trailing
    # "/standardReceipts" already baked in. But oracle/fusion_client.py's
    # post_standard_receipt() ALSO appends "/standardReceipts" itself
    # (url = f"{s.ORACLE_FUSION_BASE_URL}/standardReceipts"), and
    # post_remittance_reference() appends
    # "/standardReceipts/{id}/child/remittanceReferences" -- so having it
    # in BOTH places doubled the path. Confirmed as a real, live bug: a
    # genuine POST with the old value hit
    # ".../standardReceipts/standardReceipts" and got a real 404 Not
    # Found back from Oracle. This setting is the resource-collection
    # ROOT -- fusion_client.py's own URL-building code is what appends
    # the specific resource path, for both endpoints it calls.
    #
    # REGRESSED ONCE, then fixed for good by the validator below. Correcting
    # only this default was not enough: .env is gitignored, so every existing
    # environment (local, UAT, prod) kept its own stale copy, and .env
    # OVERRIDES the default. The bug reappeared the moment an unrelated TLS
    # fix stopped masking it -- every POST had been dying at the TLS handshake
    # before it could reach Oracle and collect its 404. A setting that only
    # one file can get right, where that file is not in the repo, is not
    # actually fixed -- hence normalisation at load time.
    ORACLE_FUSION_BASE_URL: str = "https://fa-etvl-test-saasfaprod1.fa.ocs.oraclecloud.com/fscmRestApi/resources/latest"

    @field_validator("ORACLE_FUSION_BASE_URL")
    @classmethod
    def _strip_resource_suffix(cls, v: str) -> str:
        """
        Accept either form of the base URL and always yield the collection ROOT.

        fusion_client.py appends the resource path itself, so a value ending in
        "/standardReceipts" produces ".../standardReceipts/standardReceipts"
        and a silent 404 with an EMPTY body -- which surfaces to the SPOC as
        "Oracle Post Failed" with no message at all, the least diagnosable
        failure the integration can produce.

        Normalising here means a stale .env on any environment self-corrects
        instead of failing every receipt, and both spellings stay valid for
        whoever writes the next deployment config.
        """
        v = (v or "").strip().rstrip("/")
        if v.lower().endswith("/standardreceipts"):
            v = v[: -len("/standardReceipts")]
        return v
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

    # ── GL Daily Rates auto-detection (file-based — NO Oracle REST API) ─────
    # Same "drop a file in a watched folder" pattern as the aging report
    # above (see gl_rates/watcher.py, the sibling of aging/watcher.py) —
    # EXCEPT unlike the aging report (in-memory only), GL rate rows are
    # UPSERTED into the gl_daily_rates table (db/models.py's GlDailyRate),
    # since rate history needs to accumulate across files, not be replaced
    # wholesale by the latest one. rule_engine/fx_service.py reads FROM
    # THIS TABLE for FX resolution — there is no live Oracle GL REST call.
    GL_RATES_SOURCE: str = "local_folder"           # "local_folder" | "sftp" (future)
    GL_RATES_WATCH_FOLDER: str = "./gl_rates_watch"  # folder scanned for new GL rate files
    GL_RATES_POLL_INTERVAL_SECONDS: int = 30        # how often to re-scan

    # ── Oracle Cloud file-transfer VM puller (SSH jump chain) ────────────────
    # Confirmed, tested connectivity (see context doc this was built from):
    #   App VM -> ssh {ORACLE_FILE_JUMP_USER}@{ORACLE_FILE_JUMP_HOST}   (hop 1, DMZ)
    #          -> ssh {ORACLE_FILE_REMOTE_USER}@{ORACLE_FILE_REMOTE_HOST} (hop 2)
    # Both hops are key-based/passwordless already -- see
    # oracle_file_pull/puller.py, which opens this exact two-hop chain
    # natively in paramiko (no shelling out to `ssh -J`).
    #
    # This does NOT change how the aging/GL-rates watchers themselves work
    # (AGING_SOURCE/GL_RATES_SOURCE above stay "local_folder") -- the
    # puller's whole job is to SFTP the remote file down and drop it into
    # the SAME local watch folders those watchers already poll, so the
    # ingestion side needs zero new code.
    ORACLE_FILE_JUMP_HOST: str = "192.168.7.30"          # DMZ server (ze42-v-zffusion)
    ORACLE_FILE_JUMP_USER: str = "cauatadmin"
    ORACLE_FILE_REMOTE_HOST: str = "144.24.100.229"       # Oracle Cloud VM (zenappdev)
    ORACLE_FILE_REMOTE_USER: str = "al123"
    ORACLE_FILE_REMOTE_PATH: str = "/u01/xxzen/data/fin/outbound/ca"

    # Exact remote filenames confirmed via `ls -ltr` on the Oracle Cloud VM.
    # Receipt-methods file deliberately NOT pulled yet -- out of scope for
    # now; add ORACLE_RECEIPT_METHODS_REMOTE_FILENAME + a third pull-spec
    # entry in oracle_file_pull/puller.py's PULL_SPECS when that's ready,
    # rather than reworking this puller.
    ORACLE_AGING_REMOTE_FILENAME:    str = "xxzen_aging_report_excel.xlsx"
    ORACLE_GL_RATES_REMOTE_FILENAME: str = "xxzen_gl_daily_rates_extract.txt"

    # How often the puller checks the remote files' mtimes. Requirement
    # says "4x/day" but exact clock times vs. even spacing was an open
    # question -- defaults to evenly spaced (6 hours). Override in .env
    # once that's confirmed, or run via cron instead with --once (see
    # puller.py's __main__ block) if specific clock times are needed.
    ORACLE_FILE_PULL_INTERVAL_SECONDS: int = 6 * 60 * 60

    # Local JSON file tracking each remote filename's last-SEEN mtime, so
    # a file is only re-downloaded when it's actually changed on the
    # remote side -- same idea as the remittance agent's
    # processed_message_ids dedupe, just keyed by filename+mtime instead
    # of a Graph message ID. A local file (not a DB table) was chosen for
    # simplicity; move this into a small Postgres table later if the
    # puller ever runs from more than one place and needs shared state.
    ORACLE_FILE_PULL_STATE_PATH: str = "./oracle_file_pull_state.json"

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

    # ── CORS ──────────────────────────────────────────────────────────────
    # Comma-separated list of origins allowed to call this API, e.g.
    # "https://cashapply-uat.zensar.com". "*" (default, local dev only) means
    # any origin -- tighten this for UAT/prod, see main.py.
    CORS_ALLOWED_ORIGINS: str = "*"

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

    # ── Oracle receipt-creation concurrency (Step 4.5) ───────────────────────
    # ThreadPoolExecutor size for the bare-receipt-creation fan-out in
    # rule_engine/orchestrator.py. Each worker thread opens its OWN DB
    # session (SQLAlchemy sessions aren't thread-safe) and makes its own
    # Oracle HTTP call, so this is also the ceiling on how many Oracle
    # standardReceipts POSTs are ever in flight at once for a single run.
    # Keep this at/under Oracle Fusion's per-client rate limit.
    ORACLE_RECEIPT_MAX_WORKERS: int = 8

    # ── Rule-engine evaluation concurrency (Step 4) ──────────────────────────
    # ThreadPoolExecutor size for the per-row rule-evaluation fan-out in
    # rule_engine/orchestrator.py (Pass 1 -> persist -> Pass 2 -> evaluate ->
    # transition -> mark-consumed). Each worker thread opens its OWN DB
    # session and its own gl_daily_rates / remittance lookups, so this is
    # also the ceiling on how many rows are mid-evaluation at once for a
    # single run. Keep this at/under your DB connection pool's capacity.
    RULE_ENGINE_MAX_WORKERS: int = 8

    # ── API payload encryption (VAPT remediation) ────────────────────────────
    # Encrypts JSON request and response BODIES with AES-256-GCM under a key
    # shared with the frontend build. See app/common/crypto/ for the format,
    # the key ring, and what is deliberately left unencrypted (file
    # downloads, uploads, health probes).
    #
    # ON by default in EVERY environment, local included. Encryption is the
    # behaviour you get by saying nothing; turning it off is the thing that
    # takes a deliberate act.
    #
    # Set API_ENCRYPTION_ENABLED=false in .env to disable it -- needed for
    # plaintext curl/pytest work and the reopen test guide, and available as
    # an incident-response escape hatch in a deployed environment. Startup
    # logs loudly whenever this is what turned encryption off (see main.py),
    # because it is exactly the kind of flag that gets left behind.
    #
    # This used to default to None, meaning "on unless APP_ENV=local". That
    # made local silently different from every other environment, which is
    # how a decryption bug reaches UAT undetected. Local now behaves like
    # production until someone opts out.
    API_ENCRYPTION_ENABLED: bool = True

    # 32 bytes, base64-encoded -- mint with `python -m scripts.gen_api_key`.
    # MUST match NEXT_PUBLIC_API_ENCRYPTION_KEY in the frontend build for
    # this environment.
    #
    # REQUIRED, since encryption is on by default. Deliberately has no
    # in-code default: a key committed to this repository would not be a
    # secret, and every environment that did not override it would silently
    # share one -- so a key lifted from any frontend bundle would decrypt all
    # of them. A ready-to-use value ships in backend.env.local.example
    # instead; copy it into .env for local work, and mint a distinct one per
    # deployed environment.
    #
    # With no key set, startup FAILS with a message naming this setting (see
    # crypto/keyring.py) rather than booting and 500-ing on every request.
    #
    # There is deliberately no key-ID setting: each payload carries a short
    # fingerprint derived from the key itself, so a key is its own identity
    # and there is nothing to keep in sync. See crypto/envelope.py.
    API_ENCRYPTION_KEY: str = ""

    # Set only while rotating a key, then remove. The backend keeps opening
    # payloads sealed with the previous key while the frontend is rebuilt
    # with the new one, which turns a rotation into three unhurried deploys
    # instead of one synchronised deploy -- see crypto/keyring.py.
    API_ENCRYPTION_KEY_PREVIOUS: str = ""



@lru_cache
def get_settings() -> Settings:
    return Settings()