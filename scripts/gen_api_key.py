"""
scripts/gen_api_key.py
=======================
Mints one AES-256 key for API payload encryption and prints the exact env
lines to paste into both sides.

    python -m scripts.gen_api_key

Run it ONCE PER ENVIRONMENT. Local, UAT and prod each get their own key, so a
key recovered from a UAT frontend bundle decrypts nothing in prod. Generating
a fresh one per environment costs nothing -- the key is just another env var.

The backend and the frontend BUILD for a given environment must carry the same
value. The frontend one is baked in at build time (Next inlines NEXT_PUBLIC_*
variables), so changing it means rebuilding the frontend, not just restarting
it.

There is no key-id to set. Every payload carries a short fingerprint derived
from the key itself, so a key is its own identity -- see
app/common/crypto/envelope.py. The fingerprint is printed below purely so you
can match a running server's startup log, or a captured payload, back to a
specific key.

To rotate: put the new key in API_ENCRYPTION_KEY, move the old one to
API_ENCRYPTION_KEY_PREVIOUS, deploy the backend, then rebuild the frontend.
The backend accepts both keys in the meantime, so the two deploys do not have
to be simultaneous.
"""
from __future__ import annotations

import base64
import os

# Imported rather than reimplemented, so this script and the running server can
# never disagree about what a key's fingerprint is. envelope.py deliberately
# has no settings or DB dependencies, so importing it here is free.
from app.common.crypto.envelope import fingerprint_hex, key_fingerprint

KEY_BYTES = 32  # AES-256


def main() -> None:
    key = os.urandom(KEY_BYTES)
    key_b64 = base64.b64encode(key).decode("ascii")

    # ASCII only, deliberately. Windows consoles default to cp1252, which
    # cannot encode box-drawing characters -- this script's whole job is to
    # print something you copy out of a terminal, so it must not crash on the
    # platform it is most likely to be run on.
    print()
    print("Generated a new AES-256 key for API payload encryption.")
    print("Use a DIFFERENT key for each environment (local / UAT / prod).")
    print()
    print("--- backend: .env ---------------------------------------------")
    print(f"API_ENCRYPTION_KEY={key_b64}")
    print()
    print("--- frontend: .env for this environment's BUILD ---------------")
    print(f"NEXT_PUBLIC_API_ENCRYPTION_KEY={key_b64}")
    print()
    print(f"Fingerprint: {fingerprint_hex(key_fingerprint(key))}")
    print("  Informational only -- nothing to configure. The server logs this")
    print("  at startup, so you can confirm which key a deployment is using.")
    print()
    print("The frontend value is inlined at build time, so rebuild (not just")
    print("restart) the frontend after changing it.")
    print()


if __name__ == "__main__":
    main()
