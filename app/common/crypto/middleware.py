"""
app.common.crypto.middleware
=============================
The ASGI layer that encrypts JSON response bodies and decrypts JSON request
bodies, applied to every endpoint at once.

Registered in main.py. NOTHING else in the codebase changes: no route, no
service, no schema, no Pydantic model. A handler still returns an ordinary
dict and still receives an ordinary parsed body. That is the whole point of
doing this here -- 113 endpoints stay untouched, so this cannot break
business logic it never sees.

WHY RAW ASGI AND NOT BaseHTTPMiddleware
---------------------------------------
Starlette's BaseHTTPMiddleware is far more pleasant to write, but it wraps
every response in its own streaming machinery. This codebase serves four
genuine StreamingResponse endpoints (the AI-usage CSV, the config export,
the executive-summary export, and storage file downloads) and
BaseHTTPMiddleware is known to buffer or stall exactly those. Raw ASGI lets
this middleware read the outgoing content-type from the
`http.response.start` message and then decide, BEFORE a single byte of body
is touched, to step aside completely -- file streams pass through with the
same number of chunks and the same back-pressure they would have had if this
middleware did not exist.

WHAT IS DELIBERATELY LEFT ALONE
-------------------------------
  * Non-JSON responses. Decided by content-type, so all four download
    endpoints are covered by one rule rather than a hand-maintained path
    list that would silently rot the next time someone adds an export.
  * Multipart request bodies. The six UploadFile endpoints receive file
    uploads; there is no sane way to JSON-wrap a multipart stream, and doing
    so would mean rewriting the upload path. Their RESPONSES are still
    encrypted -- request and response are judged independently.
  * EXEMPT_PATHS: /health must stay readable or container and
    load-balancer probes fail, and the OpenAPI docs must stay readable or
    Swagger UI stops working.
  * OPTIONS preflights, which carry no body.
  * Empty bodies (204, 304, and any 200 with nothing in it). Sealing zero
    bytes yields a valid envelope that decrypts to "", which the frontend
    would then try to JSON.parse -- an error invented entirely by this
    middleware. Passed straight through instead.

PLAINTEXT REQUESTS ARE STILL ACCEPTED
-------------------------------------
When encryption is on, this middleware decrypts a request body if it looks
like an envelope and leaves it alone if it does not. It does not *require*
callers to encrypt. Two reasons: it makes the rollout safe (a cached or
half-deployed frontend keeps working instead of every request failing at
once), and it is what keeps the multipart upload path viable. This is not a
weakened security boundary -- an attacker gains nothing by sending plaintext,
since the finding being remediated is about sensitive data being *readable
in responses*, not about compelling clients to encrypt.

UNHANDLED 500s ARE NOT ENCRYPTED
--------------------------------
common/errors.py registers a handler for bare `Exception`, and Starlette
installs that one on ServerErrorMiddleware -- which sits OUTSIDE all user
middleware, so its response never passes through here. Every deliberate
error (AppError, HTTPException, RequestValidationError) is handled inside
and IS encrypted. The residue is the generic
`{"code": 5000, "title": ..., "message": ...}` body, which by design carries
no customer, bank, or invoice data and no traceback (see main.py). The
frontend's error interceptor therefore treats an encrypted body as optional
rather than assumed, and works either way.
"""
from __future__ import annotations

import json
import logging

from .envelope import (
    DecryptionError,
    EnvelopeError,
    envelope_fingerprint,
    fingerprint_hex,
    looks_like_envelope,
    seal,
    unseal,
)
from .keyring import KeyRing

logger = logging.getLogger(__name__)

# Kept readable so infrastructure and tooling keep working. Matched as exact
# paths or prefixes (see _is_exempt) rather than by substring, so a business
# route that merely contains one of these words is not accidentally exempted.
EXEMPT_PATHS: tuple[str, ...] = (
    "/health",          # container + load-balancer probes
    "/docs",            # Swagger UI (and /docs/oauth2-redirect)
    "/redoc",
    "/openapi.json",    # the schema Swagger UI fetches
)

_JSON_CONTENT_TYPES = ("application/json", "application/problem+json")


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def _is_json(content_type: bytes | None) -> bool:
    if not content_type:
        return False
    # Compare only the media type: "application/json; charset=utf-8" counts.
    media_type = content_type.split(b";", 1)[0].strip().lower().decode("latin-1")
    return media_type in _JSON_CONTENT_TYPES


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in EXEMPT_PATHS)


def _replace_content_length(
    headers: list[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    """
    Rebuild headers with a corrected content-length.

    Rewriting a body without fixing this is THE failure mode of body-mutating
    ASGI middleware: the client is told to expect the original number of
    bytes, so it either truncates the payload or blocks waiting for bytes
    that never arrive. Any transfer-encoding is dropped too, since the
    outgoing body is now a single complete buffer rather than a chunked
    stream.
    """
    rebuilt = [
        (key, value)
        for key, value in headers
        if key.lower() not in (b"content-length", b"transfer-encoding")
    ]
    rebuilt.append((b"content-length", str(length).encode("latin-1")))
    return rebuilt


class ApiEncryptionMiddleware:
    """
    Seals outgoing JSON, opens incoming JSON.

    Instantiated with an already-validated KeyRing (main.py builds it at
    startup so a bad key stops the process booting rather than failing every
    request later).
    """

    def __init__(self, app, keyring: KeyRing):
        self.app = app
        self.keyring = keyring

    async def __call__(self, scope, receive, send):
        # Lifespan and websocket scopes have no HTTP bodies to touch.
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        if scope.get("method") == "OPTIONS" or _is_exempt(scope.get("path", "")):
            return await self.app(scope, receive, send)

        decrypted_receive = await self._decrypting_receive(scope, receive, send)
        if decrypted_receive is None:
            # Body was an envelope this process cannot open; already responded.
            return
        await self.app(scope, decrypted_receive, self._sealing_send(send))

    # ── request path ────────────────────────────────────────────────────────
    async def _decrypting_receive(self, scope, receive, send):
        """
        Returns a replacement `receive` serving the decrypted body, or the
        original one untouched. Returns None if it has already sent an error
        response, in which case the caller must not invoke the app.
        """
        content_type = _header(scope.get("headers", []), b"content-type")
        if not _is_json(content_type):
            # Multipart uploads, form posts, and bodiless GETs land here.
            return receive

        body, disconnected = await _read_body(receive)
        if disconnected:
            return _replay(body, more=False)
        if not body:
            return _replay(body, more=False)

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            # Not JSON despite the header. Let the app's own validation
            # produce the error message it would normally produce -- inventing
            # a different one here would change existing behaviour.
            return _replay(body, more=False)

        # Intent, not validity -- so a corrupt or future-version envelope falls
        # into the strict branch below and gets a specific error, rather than
        # being handed to the endpoint as though it were plaintext. See
        # envelope.looks_like_envelope().
        if not looks_like_envelope(payload):
            return _replay(body, more=False)   # plaintext client; see module docstring

        try:
            fingerprint = envelope_fingerprint(payload)
            key = self.keyring.get(fingerprint)
            if key is None:
                # Almost always a frontend built with a different environment's
                # key, or one that predates a completed rotation.
                raise DecryptionError(
                    f"no key held with fingerprint {fingerprint_hex(fingerprint)}"
                )
            plaintext = unseal(payload, key)
        except (EnvelopeError, DecryptionError) as exc:
            # Logged with the real reason; the client is told only that it
            # could not be decrypted (see DecryptionError's docstring).
            logger.warning(
                "Request payload decryption failed for %s %s: %s",
                scope.get("method"), scope.get("path"), exc,
            )
            await _send_plain_error(
                send,
                400,
                "Encrypted payload could not be read",
                "The request body could not be decrypted. This usually means the "
                "client and server are configured with different API encryption "
                "keys, or the frontend needs rebuilding after a key rotation.",
            )
            return None

        _set_request_length(scope, len(plaintext))
        return _replay(plaintext, more=False)

    # ── response path ───────────────────────────────────────────────────────
    def _sealing_send(self, send):
        state: dict = {"start": None, "seal": False, "chunks": []}

        async def wrapped(message):
            kind = message.get("type")

            if kind == "http.response.start":
                # Hold the start message back: whether to seal depends on the
                # content-type declared here, and if we do seal, content-length
                # must change before these headers go out.
                state["start"] = message
                state["seal"] = _is_json(_header(message.get("headers", []), b"content-type"))
                if not state["seal"]:
                    await send(message)      # non-JSON: step aside entirely
                return

            if kind == "http.response.body":
                if not state["seal"]:
                    await send(message)      # streams through chunk-for-chunk
                    return

                state["chunks"].append(message.get("body", b""))
                if message.get("more_body", False):
                    return                   # keep buffering; JSON bodies are small

                plaintext = b"".join(state["chunks"])
                start = state["start"]

                if not plaintext:
                    # 204/304 and empty 200s -- see module docstring.
                    await send(start)
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return

                sealed = json.dumps(
                    seal(plaintext, self.keyring.current_key),
                    separators=(",", ":"),
                ).encode("utf-8")

                await send({
                    "type": "http.response.start",
                    "status": start["status"],
                    "headers": _replace_content_length(start.get("headers", []), len(sealed)),
                })
                await send({"type": "http.response.body", "body": sealed, "more_body": False})
                return

            await send(message)

        # `state` lives in this closure, created fresh per request -- never on
        # the middleware instance, which is shared by every concurrent request.
        return wrapped


async def _read_body(receive) -> tuple[bytes, bool]:
    """Drain the request body. Returns (body, client_disconnected)."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return b"".join(chunks), True
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks), False


def _replay(body: bytes, more: bool):
    """
    A `receive` that yields this body once, then reports disconnect.

    The body has already been drained from the real receive, so downstream
    (Starlette's Request.body(), FastAPI's model parsing) needs something
    that hands it over exactly once. Reporting http.disconnect afterwards
    rather than hanging matters: a client that aborts mid-request would
    otherwise leave a handler awaiting a message that can never arrive.
    """
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": more}
        return {"type": "http.disconnect"}

    return receive


def _set_request_length(scope, length: int) -> None:
    """Point content-length at the DECRYPTED body, which is a different size."""
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() != b"content-length"
    ]
    headers.append((b"content-length", str(length).encode("latin-1")))
    scope["headers"] = headers


async def _send_plain_error(send, status: int, title: str, message: str) -> None:
    """
    Emit an unencrypted error, matching common/errors.py's body shape.

    Deliberately plaintext: this fires precisely when the two sides disagree
    about keys, so an encrypted explanation of a key problem is the one thing
    the client provably cannot read. The frontend's error interceptor accepts
    plaintext bodies for exactly this reason. It carries no business data.
    """
    body = json.dumps(
        {"code": 4000, "title": title, "message": message},
        separators=(",", ":"),
    ).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
