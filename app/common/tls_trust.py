"""
app.common.tls_trust
=======================
Makes outbound HTTPS use the OPERATING SYSTEM's trust store instead of
certifi's bundled Mozilla root list.

WHY THIS EXISTS
---------------
Every Oracle Fusion call failed on a corporate network with:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate (_ssl.c:1082)

Nothing was wrong with Oracle, our credentials, or our code. The corporate
network runs a TLS-inspecting proxy (Cisco Secure Access / SASE), which
terminates the connection and re-signs it with its own CA. The certificate
the app actually receives for Oracle is:

    subject  CN=fa-etvl-test-saasfaprod1.fa.ocs.oraclecloud.com
    issuer   CN=Cisco Secure Access Secondary SubCA p-aps111-SG, O=Cisco
             -> chains to CN=Cisco Secure Access Root CA, O=Cisco

That root IS installed and trusted in the Windows certificate store — which
is why browsers, curl on Windows, and Oracle's own web UI all work fine on
the same machine. But httpx (like requests) verifies against `certifi`, a
hardcoded copy of Mozilla's root list that deliberately knows nothing about
locally-installed enterprise CAs. So Python was the only thing on the machine
that could not reach Oracle.

WHAT THIS DOES
--------------
`truststore.inject_into_ssl()` patches `ssl.SSLContext` so Python verifies
using the platform's native verifier — SChannel on Windows, Security
framework on macOS, the system CA directory on Linux. Any locally-trusted
enterprise CA is then honoured automatically.

WHY NOT THE ALTERNATIVES
------------------------
  - `verify=False` — disables verification entirely, on the one code path in
    this app that transmits customer bank data and receipt amounts. Never.
  - Ship the Cisco CA as a .pem in the repo and point httpx at it — commits
    one company's network topology into the source tree, breaks the moment
    the proxy CA rotates, and does nothing for the next developer on a
    different network.
  - `SSL_CERT_FILE` env var — httpx 0.27 builds its default context from
    certifi explicitly, so it does not consistently honour this. It also
    pushes the problem onto every developer's shell profile.

Truststore needs no configuration and is correct on every platform: on a
normal network the OS store contains the same public roots certifi does, so
behaviour is unchanged.

WHERE IT IS CALLED
------------------
Both process entry points, before any module that might open a connection:

    app/main.py          — the API process (uvicorn)
    app/tasks/worker.py  — the background worker process

Both are needed. Receipt creation runs in whichever process handles the
request, and an analysis run posts from the worker.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("cashapply.tls")

_configured = False


def configure_tls_trust() -> bool:
    """
    Route SSL verification through the OS trust store. Idempotent.

    Returns True if the injection happened, False if it was skipped. Never
    raises: a failure here must degrade to "certifi-only verification" — the
    behaviour before this module existed — rather than stop the process from
    starting. Verification itself is never weakened either way.
    """
    global _configured
    if _configured:
        return True

    try:
        import truststore
    except ImportError:
        logger.warning(
            "[tls] truststore is not installed — HTTPS will verify against certifi only. "
            "On a network with a TLS-inspecting proxy, outbound calls to Oracle will fail "
            "with CERTIFICATE_VERIFY_FAILED. Fix: pip install -r requirements.txt"
        )
        return False

    try:
        truststore.inject_into_ssl()
    except Exception as e:  # pragma: no cover — platform-specific
        logger.warning(
            "[tls] could not use the OS trust store (%s: %s) — falling back to certifi. "
            "Outbound HTTPS still verifies; it just won't honour locally-installed CAs.",
            type(e).__name__, e,
        )
        return False

    _configured = True
    logger.info("[tls] verifying outbound HTTPS against the OS trust store (locally-trusted CAs honoured)")
    return True
