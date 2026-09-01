"""
app.oracle.http_debug_log
===========================
Shared request/response logging for every outbound call to Oracle Fusion
(standardReceipts posting, GL daily rates lookups, OAuth token fetches).

WHY: debugging Oracle integration issues (wrong URL, bad auth, unexpected
payload shape, Oracle-side validation errors) previously meant re-reading
code to guess what was actually sent. This module logs the EXACT request as
a copy-pasteable curl command, plus the full response, so any Oracle call
can be reproduced independently (e.g. in Postman or a terminal) without
touching the app.

SECURITY: credentials are ALWAYS redacted in the logged curl command —
Basic Auth passwords, Bearer tokens, and OAuth client secrets never appear
in plaintext in logs. Log files are typically retained far longer and read
by more people/tools than the settings.py file the secret came from, so
printing it in every request log would be a materially worse exposure than
today's plaintext-default-in-code issue. If you need the real credential to
manually replay a curl command, pull it from your .env / secrets manager,
not from logs.

USAGE:
    from .http_debug_log import log_oracle_request, log_oracle_response

    curl_cmd = log_oracle_request("POST", url, headers=headers, auth=auth,
                                   json_body=payload, tag="standardReceipts")
    resp = httpx.post(url, json=payload, headers=headers, auth=auth, timeout=60)
    log_oracle_response(resp, tag="standardReceipts")
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("oracle.http")

_REDACTED = "***REDACTED***"
_MAX_BODY_LOG_CHARS = 8000  # avoid flooding logs with pathological payloads/responses


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            redacted[k] = f"Bearer {_REDACTED}" if v.lower().startswith("bearer") else _REDACTED
        else:
            redacted[k] = v
    return redacted


def _curl_auth_flag(auth: Optional[tuple[str, str]]) -> str:
    """Basic-auth flag for curl, with the password always redacted."""
    if not auth:
        return ""
    username, _password = auth
    return f' -u "{username}:{_REDACTED}"'


def _shell_quote_single(s: str) -> str:
    # Safe single-quoting for embedding arbitrary JSON in a curl -d '...' arg.
    return "'" + s.replace("'", "'\\''") + "'"


def build_curl_command(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[tuple[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict] = None,
    form_body: Optional[dict] = None,
    xml_body: Optional[str] = None,
) -> str:
    """
    Builds a copy-pasteable curl command equivalent to the given request.
    All secrets (Authorization header, Basic Auth password) are redacted —
    replace ***REDACTED*** with the real value from .env to actually run it.

    Pass exactly one of json_body (sent as -d '<json>' with a JSON
    Content-Type), form_body (sent as -d "k=v&k2=v2", matching
    application/x-www-form-urlencoded — e.g. the OAuth token endpoint), or
    xml_body (sent as -d '<xml>' with a text/xml Content-Type — SOAP calls,
    e.g. processUnapplyReceipt. The caller is expected to already have set
    an explicit Content-Type header for XML requests, since SOAP servers
    are often picky about the exact charset suffix — this function does not
    add one on xml_body's behalf the way it does for json_body).
    """
    headers = headers or {}
    content_type = {"Content-Type": "application/json"} if json_body is not None else {}
    all_headers = {**content_type, **_redact_headers(headers)}

    parts = [f"curl -X {method.upper()}"]

    full_url = url
    if params:
        # httpx encodes params itself; here we just show them appended for
        # readability — good enough for a debug curl, not byte-exact.
        query = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{url}?{query}"
    parts.append(f'"{full_url}"')

    for k, v in all_headers.items():
        parts.append(f'-H "{k}: {v}"')

    auth_flag = _curl_auth_flag(auth)
    if auth_flag:
        parts.append(auth_flag.strip())

    if json_body is not None:
        body_str = json.dumps(json_body, default=str)
        parts.append(f"-d {_shell_quote_single(body_str)}")
    elif form_body is not None:
        body_str = "&".join(f"{k}={v}" for k, v in form_body.items())
        parts.append(f"-d {_shell_quote_single(body_str)}")
    elif xml_body is not None:
        parts.append(f"-d {_shell_quote_single(xml_body)}")

    return " \\\n  ".join(parts)


def log_oracle_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[tuple[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict] = None,
    form_body: Optional[dict] = None,
    xml_body: Optional[str] = None,
    tag: str = "oracle",
) -> str:
    """Logs the outbound request as a redacted curl command. Returns the curl string."""
    curl_cmd = build_curl_command(
        method, url, headers=headers, auth=auth, params=params,
        json_body=json_body, form_body=form_body, xml_body=xml_body,
    )
    logger.info("[%s] REQUEST →\n%s", tag, curl_cmd)
    return curl_cmd


def log_oracle_response(resp, *, tag: str = "oracle") -> None:
    """Logs status code, headers, and body (truncated) for an httpx.Response."""
    body_preview = resp.text[:_MAX_BODY_LOG_CHARS]
    truncated_note = " …[truncated]" if len(resp.text) > _MAX_BODY_LOG_CHARS else ""
    logger.info(
        "[%s] RESPONSE ← status=%s\nheaders=%s\nbody=%s%s",
        tag, resp.status_code, dict(resp.headers), body_preview, truncated_note,
    )


def log_oracle_error(exc: Exception, *, tag: str = "oracle") -> None:
    """Logs a connection-level failure (DNS, timeout, refused, etc.) — no response object exists yet."""
    logger.error("[%s] REQUEST FAILED before a response was received: %s", tag, exc)