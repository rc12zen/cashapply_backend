"""
app.common.crypto
==================
API payload encryption (VAPT remediation: sensitive data must not travel as
readable JSON in API responses).

Three modules, deliberately layered so the crypto itself has no dependency
on the web framework or on configuration:

    envelope.py    the wire format + pure seal()/unseal(). No I/O, no
                   settings, no FastAPI -- unit-testable on its own and
                   reused by scripts/decrypt_payload.py.
    keyring.py     turns environment configuration into usable key bytes,
                   including the previous key during a rotation window.
    middleware.py  the ASGI layer that applies it to every JSON request and
                   response, and -- just as importantly -- knows what to
                   leave alone (file downloads, uploads, health probes).

Nothing outside this package needs to know encryption exists: no route, no
service, and no schema in this codebase references it. See middleware.py for
why that separation is load-bearing rather than merely tidy.
"""
