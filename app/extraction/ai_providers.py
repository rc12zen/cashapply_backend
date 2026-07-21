"""
app.extraction.ai_providers
==============================
Thin abstraction over "send a system+user prompt, get back a text
completion" so extraction/layer_2b_ai.py can run against either Anthropic
Claude or OpenAI (e.g. GPT-5 mini) — switchable via Settings.AI_PROVIDER
(`.env`'s AI_PROVIDER=anthropic|openai), no code change needed to swap.

Both branches return the SAME shape (AiCallResult) so layer_2b_ai.py has
zero provider-specific branching — it calls call_ai(...) once and doesn't
care which provider actually answered.

CAVEAT — GPT-5 mini specifics: the newer OpenAI reasoning-capable model
families (o1/o3/gpt-5) have tightened up what parameters they accept
compared to older chat models:
  - `max_tokens` is deprecated in favor of `max_completion_tokens` (used
    below).
  - Some of these models reject any `temperature` other than the default
    (1) — including 0, which is what this pipeline wants for deterministic
    extraction. The OpenAI branch below does NOT send `temperature` at all
    for exactly this reason. If your specific GPT-5 mini deployment DOES
    accept temperature=0 and you want it for consistency with the
    Anthropic path, uncomment the line below and test it against a real
    call first — a rejected parameter fails the whole request, not just a
    warning.
Confirm both of these against the exact model string in OPENAI_MODEL/your
OpenAI account before relying on this in UAT — model behavior here has
changed across GPT-5 family releases.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class AiCallResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str          # "anthropic" | "openai" -- see ai_usage/tracker.py
    latency_ms: int


_anthropic_client_singleton = None
_openai_client_singleton = None


def _get_anthropic_client():
    """Single shared client per process — safe to reuse across sequential
    calls (each chunk thread only ever has one call in flight at a time)."""
    global _anthropic_client_singleton
    if _anthropic_client_singleton is not None:
        return _anthropic_client_singleton
    import anthropic
    from ..db.settings import get_settings
    s = get_settings()
    _anthropic_client_singleton = anthropic.Anthropic(api_key=s.ANTHROPIC_API_KEY)
    return _anthropic_client_singleton


def _get_openai_client():
    global _openai_client_singleton
    if _openai_client_singleton is not None:
        return _openai_client_singleton
    import openai
    from ..db.settings import get_settings
    s = get_settings()
    _openai_client_singleton = openai.OpenAI(api_key=s.OPENAI_API_KEY)
    return _openai_client_singleton


_azure_openai_client_singleton = None


def _get_azure_openai_client():
    """Azure OpenAI Service -- a model deployed into your own Azure tenant's
    resource group, NOT the same product as direct OpenAI above.

    Uses Azure's newer unified "v1" endpoint (confirmed against Azure AI
    Foundry's own sample code for this exact resource): the plain `OpenAI`
    client pointed at a custom base_url ending in `/openai/v1`, with the
    resource's API key -- NOT the older `AzureOpenAI` class + separate
    `api_version` parameter, which is a different (older) connection shape.
    `model=` at call time is the DEPLOYMENT NAME (often same as the model
    name itself, e.g. "gpt-5.4-mini" -- confirm in Azure AI Foundry's
    Deployments page, NOT assumed)."""
    global _azure_openai_client_singleton
    if _azure_openai_client_singleton is not None:
        return _azure_openai_client_singleton
    import openai
    from ..db.settings import get_settings
    s = get_settings()
    base_url = s.AZURE_OPENAI_ENDPOINT.rstrip("/") + "/openai/v1"
    _azure_openai_client_singleton = openai.OpenAI(api_key=s.AZURE_OPENAI_API_KEY, base_url=base_url)
    return _azure_openai_client_singleton


def _call_anthropic(system_prompt: str, user_message: str, max_tokens: int) -> AiCallResult:
    from ..db.settings import get_settings
    s = get_settings()
    client = _get_anthropic_client()

    t0 = time.monotonic()
    response = client.messages.create(
        model=s.CLAUDE_MODEL,
        max_tokens=max_tokens,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    text = response.content[0].text if response.content else ""
    return AiCallResult(
        text=text.strip(),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=s.CLAUDE_MODEL,
        provider="anthropic",
        latency_ms=latency_ms,
    )


def _call_openai(system_prompt: str, user_message: str, max_tokens: int) -> AiCallResult:
    from ..db.settings import get_settings
    s = get_settings()
    client = _get_openai_client()

    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=s.OPENAI_MODEL,
        max_completion_tokens=max_tokens,
        # temperature intentionally omitted -- see module docstring caveat.
        # temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    text = response.choices[0].message.content or ""
    usage = response.usage
    return AiCallResult(
        text=text.strip(),
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        model=s.OPENAI_MODEL,
        provider="openai",
        latency_ms=latency_ms,
    )


def _call_azure_openai(system_prompt: str, user_message: str, max_tokens: int) -> AiCallResult:
    """Uses the Responses API (client.responses.create), NOT
    chat.completions -- confirmed against Azure AI Foundry's own sample
    code for this exact deployment/endpoint (the v1 unified endpoint).
    Response shape is different from chat.completions: text comes back on
    `response.output_text`, and token counts are `response.usage.
    input_tokens`/`output_tokens` (not `prompt_tokens`/`completion_tokens`).
    """
    from ..db.settings import get_settings
    s = get_settings()
    client = _get_azure_openai_client()

    t0 = time.monotonic()
    response = client.responses.create(
        model=s.AZURE_OPENAI_DEPLOYMENT,  # Azure addresses models by DEPLOYMENT NAME
        instructions=system_prompt,
        input=user_message,
        max_output_tokens=max_tokens,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    text = getattr(response, "output_text", None) or ""
    usage = getattr(response, "usage", None)
    return AiCallResult(
        text=text.strip(),
        input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        model=s.AZURE_OPENAI_DEPLOYMENT,
        provider="azure_openai",
        latency_ms=latency_ms,
    )


def call_ai(system_prompt: str, user_message: str, max_tokens: int) -> AiCallResult:
    """Calls whichever provider Settings.AI_PROVIDER currently points at.
    Raises on failure -- callers (layer_2b_ai.py) already wrap this in
    their own try/except + usage-tracking-on-failure logic, so this stays
    a thin, exception-transparent call rather than swallowing errors here."""
    from ..db.settings import get_settings
    s = get_settings()
    provider = (s.AI_PROVIDER or "anthropic").strip().lower()

    if provider == "azure_openai":
        return _call_azure_openai(system_prompt, user_message, max_tokens)
    if provider == "openai":
        return _call_openai(system_prompt, user_message, max_tokens)
    if provider == "anthropic":
        return _call_anthropic(system_prompt, user_message, max_tokens)
    raise ValueError(f"Unknown AI_PROVIDER '{provider}' -- must be 'anthropic', 'openai', or 'azure_openai'")


# ── Status check ──────────────────────────────────────────────────────────
# "Is AI extraction actually usable right now" -- for the Home page banner
# (see bff/config_routes.py's /ai-status) so a SPOC knows, BEFORE starting
# analysis, whether Layer 2B's AI fallback will actually run or every
# unresolved row will just fall through to "unidentified" for lack of a
# working AI call. A present-but-invalid/expired key would look "configured"
# with no further check, so this makes one real, minimal call to the
# provider (list models -- metadata only, costs no completion tokens) to
# confirm the key genuinely authenticates and the service is reachable.
#
# Cached briefly (_STATUS_CACHE_TTL_SECONDS) so a page that shows this on
# every load doesn't hit the provider's API constantly -- pass
# force_refresh=True (wired to a "Recheck" button) to bypass the cache.

_STATUS_CACHE_TTL_SECONDS = 60
_status_cache: dict | None = None
_status_cache_at: float = 0.0


def _check_anthropic_reachable() -> tuple[bool, str | None]:
    from ..db.settings import get_settings
    s = get_settings()
    if not s.ANTHROPIC_API_KEY:
        return False, "ANTHROPIC_API_KEY is not set"
    try:
        client = _get_anthropic_client()
        client.models.list(limit=1)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _check_openai_reachable() -> tuple[bool, str | None]:
    from ..db.settings import get_settings
    s = get_settings()
    if not s.OPENAI_API_KEY:
        return False, "OPENAI_API_KEY is not set"
    try:
        client = _get_openai_client()
        client.models.retrieve(s.OPENAI_MODEL)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _check_azure_openai_reachable() -> tuple[bool, str | None]:
    from ..db.settings import get_settings
    s = get_settings()
    missing = [
        name for name, val in [
            ("AZURE_OPENAI_API_KEY", s.AZURE_OPENAI_API_KEY),
            ("AZURE_OPENAI_ENDPOINT", s.AZURE_OPENAI_ENDPOINT),
            ("AZURE_OPENAI_DEPLOYMENT", s.AZURE_OPENAI_DEPLOYMENT),
        ] if not val
    ]
    if missing:
        return False, f"Missing: {', '.join(missing)}"
    try:
        client = _get_azure_openai_client()
        # Azure OpenAI's deployment-scoped resources don't expose a cheap
        # "retrieve one model" metadata call the way direct OpenAI does --
        # the reliable equivalent is a minimal real completion against the
        # deployment (1 output token, negligible cost) to confirm the
        # endpoint/key/deployment name are all valid together. Uses the
        # same responses.create() shape as the real extraction call -- see
        # _call_azure_openai's docstring for why (Azure's own confirmed
        # working sample for this endpoint, not chat.completions).
        client.responses.create(
            model=s.AZURE_OPENAI_DEPLOYMENT,
            input="ping",
            max_output_tokens=16,
        )
        return True, None
    except Exception as exc:
        return False, str(exc)


def get_ai_status(force_refresh: bool = False) -> dict:
    """Returns:
        {"provider": "openai", "model": "gpt-5-mini", "configured": bool,
         "active": bool, "checked_at": iso-str, "message": str, "cached": bool}
    `configured` = an API key is present at all. `active` = that key was
    just confirmed to actually authenticate against the provider (the
    stronger, more useful signal -- a key can be "configured" and still be
    revoked/expired/typo'd).
    """
    import datetime as _dt
    import time as _time
    global _status_cache, _status_cache_at

    if not force_refresh and _status_cache is not None and (_time.monotonic() - _status_cache_at) < _STATUS_CACHE_TTL_SECONDS:
        return {**_status_cache, "cached": True}

    from ..db.settings import get_settings
    s = get_settings()
    provider = (s.AI_PROVIDER or "anthropic").strip().lower()

    if provider == "azure_openai":
        model = s.AZURE_OPENAI_DEPLOYMENT
        active, error = _check_azure_openai_reachable()
        configured = bool(s.AZURE_OPENAI_API_KEY and s.AZURE_OPENAI_ENDPOINT and s.AZURE_OPENAI_DEPLOYMENT)
    elif provider == "openai":
        model = s.OPENAI_MODEL
        active, error = _check_openai_reachable()
        configured = bool(s.OPENAI_API_KEY)
    elif provider == "anthropic":
        model = s.CLAUDE_MODEL
        active, error = _check_anthropic_reachable()
        configured = bool(s.ANTHROPIC_API_KEY)
    else:
        model = None
        active, error, configured = False, f"Unknown AI_PROVIDER '{provider}'", False

    if active:
        message = f"AI extraction is active ({provider} {model})."
    elif not configured:
        message = (
            f"No API key configured for {provider} -- AI extraction fallback will not run. "
            f"Analysis can still proceed using pattern/regex matching only, but unresolved rows "
            f"won't get the AI second pass."
        )
    else:
        message = f"{provider} is configured but not reachable right now ({error}). Contact an administrator."

    result = {
        "provider": provider,
        "model": model,
        "configured": configured,
        "active": active,
        "checked_at": _dt.datetime.utcnow().isoformat(),
        "message": message,
    }
    _status_cache = result
    _status_cache_at = _time.monotonic()
    return {**result, "cached": False}