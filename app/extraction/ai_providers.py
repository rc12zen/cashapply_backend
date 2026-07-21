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


def call_ai(system_prompt: str, user_message: str, max_tokens: int) -> AiCallResult:
    """Calls whichever provider Settings.AI_PROVIDER currently points at.
    Raises on failure -- callers (layer_2b_ai.py) already wrap this in
    their own try/except + usage-tracking-on-failure logic, so this stays
    a thin, exception-transparent call rather than swallowing errors here."""
    from ..db.settings import get_settings
    s = get_settings()
    provider = (s.AI_PROVIDER or "anthropic").strip().lower()

    if provider == "openai":
        return _call_openai(system_prompt, user_message, max_tokens)
    if provider == "anthropic":
        return _call_anthropic(system_prompt, user_message, max_tokens)
    raise ValueError(f"Unknown AI_PROVIDER '{provider}' -- must be 'anthropic' or 'openai'")


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

    if provider == "openai":
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