"""
app.common.crypto.keyring
==========================
Turns environment configuration into the key bytes the middleware needs, and
answers the one question the middleware asks at import time: is payload
encryption on for this environment at all?

WHY A RING RATHER THAN A KEY
-----------------------------
Under a single shared static key, the frontend bundle and the backend must
agree on that key exactly. If there were only ever one, rotating it would mean
deploying frontend and backend in the same instant -- and any skew, in either
direction, breaks every single request until both sides land. That is not a
deploy anyone wants to run against a production finance system.

So the receiver accepts a SET of keys, and every payload carries a fingerprint
of the key that sealed it (see envelope.py). Rotation then becomes ordinary:

    1. Backend learns the new key as CURRENT and keeps the old as PREVIOUS.
       Deploy. Backend now seals with the new key but still opens payloads
       sealed with the old one, so the live frontend keeps working.
    2. Frontend is rebuilt with the new key. Deploy. Both sides now on new.
    3. Drop PREVIOUS from the backend config at any later, unhurried point.

Step 1 is where the ring earns its place: without it, steps 1 and 2 have to be
simultaneous. Four bytes on the wire buys a rotation that is three calm
deploys instead of one synchronised one -- which is why it is here from the
first commit rather than added when first needed.

NO KEY-ID SETTING
-----------------
The fingerprint is DERIVED from the key (envelope.key_fingerprint), not
configured. That deletes a whole class of misconfiguration that an explicit
key-id invites: an id that does not match the key it labels, two environments
sharing an id, a rotation where the id was reused so the new key silently
displaced the old one for incoming payloads. None of those are expressible
now -- a key simply is its own identity, and there is nothing to keep in sync.

ONE KEY PER ENVIRONMENT
-----------------------
Local, UAT and prod each get their own key. A key recovered from a UAT bundle
then decrypts nothing in prod. This costs nothing to arrange (the key is just
another env var, and .env is already per-environment) and removes the worst
version of a leak.

There is deliberately NO in-code default key. One would make a fresh checkout
boot with zero setup, but it would sit in version control -- so it would not be
a secret, and every environment that had not overridden it would share it,
which is precisely the property this section exists to avoid. A ready-to-use
value lives in backend.env.local.example instead: copy-paste convenience for
local work, without the code claiming a key it cannot keep.

FAILING AT STARTUP, NOT AT THE FIRST REQUEST
--------------------------------------------
load_keyring() raises if encryption is enabled and the key is missing,
malformed, or the wrong length. main.py calls it during startup precisely so a
misconfigured deployment refuses to boot with an explicit message, rather than
starting healthily and then 500-ing on every request with something opaque. A
key problem is a deployment problem; it should look like one.

Because encryption is now ON by default everywhere (see encryption_enabled),
this is also what a brand-new checkout hits: no .env, no key, and a startup
error naming API_ENCRYPTION_KEY. That is the intended first experience -- the
alternative is a silent fallback that looks configured and is not.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from .envelope import KEY_BYTES, fingerprint_hex, key_fingerprint


class KeyConfigError(RuntimeError):
    """
    Encryption is enabled but the configured keys are unusable.

    Raised at startup only (see module docstring). The message names the
    offending setting, because the only audience for it is whoever is
    deploying, reading a container log and trying to fix a config value.
    """


@dataclass(frozen=True)
class KeyRing:
    """
    The key this process seals with, and every key it will accept.

    `keys` maps fingerprint -> key and covers INCOMING payloads: always the
    current key, plus the previous one while a rotation is in flight.
    """
    current_key: bytes
    keys: dict[bytes, bytes]

    @property
    def current_fingerprint(self) -> bytes:
        return key_fingerprint(self.current_key)

    def get(self, fingerprint: bytes) -> bytes | None:
        """The key with this fingerprint, or None if this process lacks it."""
        return self.keys.get(fingerprint)

    @property
    def accepted_fingerprints(self) -> list[str]:
        """For startup logging -- confirms a rotation window is really open."""
        return sorted(fingerprint_hex(f) for f in self.keys)


def encryption_enabled(settings) -> bool:
    """
    Whether to encrypt API payloads in this environment.

    On unless API_ENCRYPTION_ENABLED is explicitly false -- in EVERY
    environment, local included. Encryption is what you get by saying
    nothing; disabling it takes a deliberate act, and startup logs loudly
    when that is what happened (see main.py).

    This used to read "on unless APP_ENV=local", which left local silently
    unencrypted. That is how a decryption bug reaches UAT undetected: the
    one environment where anyone iterates was the one environment not
    exercising the code path. Setting API_ENCRYPTION_ENABLED=false is still
    there for plaintext curl/pytest work -- it is just opt-out now rather
    than the default.
    """
    return bool(getattr(settings, "API_ENCRYPTION_ENABLED", True))


def _decode_key(raw: str, setting_name: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyConfigError(
            f"{setting_name} is not valid base64. Generate a correct value with "
            f"`python -m scripts.gen_api_key`."
        ) from exc
    if len(key) != KEY_BYTES:
        raise KeyConfigError(
            f"{setting_name} decodes to {len(key)} bytes; AES-256 needs exactly "
            f"{KEY_BYTES}. Generate a correct value with `python -m scripts.gen_api_key`."
        )
    return key


def load_keyring(settings) -> KeyRing:
    """
    Build the KeyRing from settings, or raise KeyConfigError.

    Only called when encryption_enabled() is true -- there is nothing to
    validate otherwise, and a local developer who has never set a key should
    not be made to.
    """
    current_raw = (getattr(settings, "API_ENCRYPTION_KEY", "") or "").strip()
    if not current_raw:
        raise KeyConfigError(
            "API payload encryption is enabled but API_ENCRYPTION_KEY is not set. "
            "Generate one with `python -m scripts.gen_api_key` and set the SAME "
            "value as NEXT_PUBLIC_API_ENCRYPTION_KEY in the frontend build for "
            "this environment."
        )

    current = _decode_key(current_raw, "API_ENCRYPTION_KEY")
    keys = {key_fingerprint(current): current}

    # Previous key -- present only while a rotation is in flight.
    prev_raw = (getattr(settings, "API_ENCRYPTION_KEY_PREVIOUS", "") or "").strip()
    if prev_raw:
        previous = _decode_key(prev_raw, "API_ENCRYPTION_KEY_PREVIOUS")
        if previous == current:
            raise KeyConfigError(
                "API_ENCRYPTION_KEY_PREVIOUS is identical to API_ENCRYPTION_KEY. "
                "A rotation needs two different keys; as written, there is no "
                "rotation window at all."
            )
        keys[key_fingerprint(previous)] = previous

    return KeyRing(current_key=current, keys=keys)
