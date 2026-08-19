"""
scripts/decrypt_payload.py
===========================
Opens a captured encrypted payload so an environment with encryption ON is
still debuggable.

    # straight from curl
    curl -s http://localhost:8000/api/auth/me -H "X-Dev-User: a@b.com" \
        | python -m scripts.decrypt_payload

    # from a file saved out of Burp / browser devtools
    python -m scripts.decrypt_payload captured.json

    # with an explicit key, e.g. reading a UAT capture from a local checkout
    python -m scripts.decrypt_payload --key "$UAT_KEY" captured.json

WHY THIS EXISTS
---------------
Encrypting responses hides them from an attacker and from you in equal
measure. The moment a UAT incident needs "what did this endpoint actually
return", an opaque base64 blob is a genuine obstacle -- so the tool to look
inside ships with the feature rather than being written under pressure at the
time.

Keys are read from the backend's own settings by default, so no key ever has
to be pasted into a shell (where it lands in history) for local or UAT work
done from a configured checkout. --key is there for the case where you are
holding a capture from an environment this checkout is not configured for.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys

from app.common.crypto.envelope import (
    DecryptionError,
    EnvelopeError,
    envelope_fingerprint,
    fingerprint_hex,
    is_envelope,
    key_fingerprint,
    unseal,
)


def _keys_from_settings() -> dict[bytes, bytes]:
    """Every key this checkout is configured for, as {fingerprint: key}."""
    from app.db.settings import get_settings
    from app.common.crypto.keyring import load_keyring

    try:
        return dict(load_keyring(get_settings()).keys)
    except Exception as exc:  # KeyConfigError, or no encryption configured here
        sys.exit(
            f"Could not load keys from this checkout's settings: {exc}\n"
            f"Pass one explicitly with --key <base64> if you are decrypting a "
            f"capture from another environment."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decrypt an API payload captured from a request or response."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="File holding the payload JSON. Reads stdin when omitted, so this "
             "can be piped straight from curl.",
    )
    parser.add_argument(
        "--key",
        help="Base64 AES-256 key to use instead of this checkout's configured "
             "keys. For captures from an environment this checkout is not set "
             "up for.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the decrypted bytes exactly as they were, without "
             "re-indenting as JSON. Use when the payload is not valid JSON and "
             "you need to see why.",
    )
    args = parser.parse_args()

    raw = open(args.file, "rb").read() if args.file else sys.stdin.buffer.read()
    if not raw.strip():
        sys.exit("Nothing to decrypt (empty input).")

    try:
        payload = json.loads(raw)
    except ValueError:
        sys.exit('Input is not JSON. An encrypted payload looks like {"d":"..."}.')

    if not is_envelope(payload):
        # Genuinely useful rather than an error: /health, file downloads, and
        # unhandled 500s are all plaintext by design, so "this was never
        # encrypted" is an answer, not a failure.
        print("This payload is NOT encrypted -- printing it as-is:\n", file=sys.stderr)
        print(json.dumps(payload, indent=2))
        return

    wanted = envelope_fingerprint(payload)

    if args.key:
        # The caller is asserting "this is the right key for this capture", so
        # it is used regardless -- but a fingerprint mismatch is reported,
        # since it means the decrypt below will certainly fail and the reason
        # is worth knowing up front.
        try:
            key = base64.b64decode(args.key.strip(), validate=True)
        except Exception:
            sys.exit("--key is not valid base64.")
        if len(key) != 32:
            sys.exit(f"--key decodes to {len(key)} bytes; AES-256 needs 32.")
        if key_fingerprint(key) != wanted:
            print(
                f"Warning: this payload was sealed by key "
                f"{fingerprint_hex(wanted)}, but --key has fingerprint "
                f"{fingerprint_hex(key_fingerprint(key))}. Trying anyway.",
                file=sys.stderr,
            )
    else:
        keys = _keys_from_settings()
        key = keys.get(wanted)
        if key is None:
            sys.exit(
                f"This payload was sealed by key {fingerprint_hex(wanted)}, but the "
                f"keys available here are {sorted(fingerprint_hex(f) for f in keys)}. "
                f"It is probably from a different environment -- pass that "
                f"environment's key with --key."
            )

    try:
        plaintext = unseal(payload, key)
    except (EnvelopeError, DecryptionError) as exc:
        sys.exit(
            f"Could not decrypt: {exc}\n"
            f"The payload names key {fingerprint_hex(wanted)}; if that key has since "
            f"been rotated out, this capture can only be read with the key that "
            f"was current at the time."
        )

    if args.raw:
        sys.stdout.buffer.write(plaintext)
        return

    try:
        print(json.dumps(json.loads(plaintext), indent=2))
    except ValueError:
        # Decrypted fine but is not JSON -- show it rather than failing, since
        # that itself is the finding.
        sys.stdout.buffer.write(plaintext)


if __name__ == "__main__":
    main()
