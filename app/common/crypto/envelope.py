"""
app.common.crypto.envelope
===========================
The on-the-wire format for an encrypted API payload, plus the pure functions
that produce and consume it. Deliberately has NO imports from FastAPI,
settings, or anything doing I/O -- so it can be unit tested directly and
reused by scripts/decrypt_payload.py without booting the app.

FORMAT -- one opaque base64 string in a single-field object:

    {"d": "AVZqU3fQ9yUXv9EoMNbTKTDkc+pfkQNh4ce7Dt2b7dwdUJhfIA=="}

Inside, after base64-decoding, is a fixed 17-byte header then the ciphertext:

    offset  size  meaning
    ------  ----  ---------------------------------------------------------
       0      1   format version (currently 1)
       1      4   key fingerprint -- WHICH key sealed this (see below)
       5     12   iv / nonce
      17    rest  AES-256-GCM ciphertext with its 16-byte auth tag appended

Everything the receiver needs is present, and nothing else is. There are no
field names, no algorithm names, and no human-readable labels.

WHY ONE OPAQUE BLOB RATHER THAN NAMED FIELDS
--------------------------------------------
An earlier version of this sent {"v":1,"kid":"local-1","iv":...,"ct":...}.
That was replaced for two reasons, only one of which is about security:

  1. REAL: the key id was a human-readable environment label ("local-1",
     "uat-1"). That disclosed the environment naming scheme and the fact that
     keys are per-environment, for no benefit. It is now a fingerprint
     DERIVED from the key (below), so it identifies the key without naming
     anything.

  2. PRESENTATION, not security: named `iv`/`ct` fields document the scheme
     to anyone reading a captured payload. Be clear-eyed that hiding them
     buys nothing cryptographically -- the algorithm is written in plain
     sight in the frontend bundle, so an attacker who can read a payload can
     also read exactly how it was made. The security of this rests entirely
     on the key, never on the format being unfamiliar. What the compact form
     does buy is that a captured payload no longer reads as a homegrown
     construction in an audit report, and it is 19 bytes smaller per
     response.

Do NOT mistake reason 2 for a security property, and do not let it justify
weakening anything else on the grounds that "the format is obscure".

THE KEY FINGERPRINT
-------------------
The first 4 bytes of SHA-256 over the key. The receiver computes the same
fingerprint from each key it holds and matches, which is how the previous key
can still be accepted during a rotation (see keyring.py).

Deriving it rather than configuring it means there is no separate key-id
setting to get wrong, no way for two environments to accidentally share an
id, and a new key automatically gets a new fingerprint. Publishing it is
safe: SHA-256 is one-way, and 4 bytes of a digest of a 256-bit random key
reveals nothing usable about the key. This is the same idea as a JWK
thumbprint.

WHY THE IV MUST NEVER REPEAT
----------------------------
AES-GCM is a stream cipher: it turns (key, iv) into a keystream and XORs that
with the plaintext. Encrypt two different payloads under the SAME (key, iv)
and an attacker who XORs the two ciphertexts together gets the XOR of the two
plaintexts, with the keystream cancelled out entirely -- no key needed.
Worse, GCM's authentication is algebraic, and a repeated nonce leaks enough
to recover the auth subkey and FORGE valid ciphertexts. This is not a
theoretical weakness; it is the standard way GCM deployments are broken.
Hence os.urandom(IV_BYTES) per call below, with no caching, no counter, and
no caller-supplied iv parameter -- there is deliberately no way for a caller
to pass one in and get this wrong.

WHY THE TAG IS NOT SEPARATED OUT
--------------------------------
Both cryptography's AESGCM.encrypt() and the browser's WebCrypto
crypto.subtle.encrypt() return tag-APPENDED ciphertext, and both expect it
that way when decrypting. Splitting the tag off means slicing and re-joining
it on both ends -- the single most common cause of "encrypts fine in Python,
fails in the browser" in exactly this design. Left joined; both platforms
then need zero special handling.

NO AAD (Associated Authenticated Data)
--------------------------------------
GCM can additionally authenticate data it does not encrypt, which is how you
would bind a payload to its request path or user so a captured response
cannot be replayed elsewhere. Not used here, on purpose: it requires both
ends to reconstruct byte-identical AAD, and any disagreement surfaces as an
indistinguishable "decryption failed". That trade is worth making when the
key is per-session (where replay resistance is the point); it is not worth it
under a single shared static key, which offers no replay protection anyway.
If this moves to per-session keys, add it then.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Bumped only if the binary layout below changes. An unrecognised version is
# reported as such rather than attempted and failed confusingly.
FORMAT_VERSION = 1

# 96 bits, the size AES-GCM is specified and optimised for. Other lengths are
# permitted by the spec but weaken the security proof, and WebCrypto on the
# browser side effectively expects 12 bytes too.
IV_BYTES = 12

# AES-256. Enforced on load (see keyring.py) so a short or typo'd key fails at
# startup rather than at the first request.
KEY_BYTES = 32

# Truncated SHA-256 of the key. 4 bytes distinguishes the two keys a rotation
# window ever holds with overwhelming margin, and keeps the header small.
FINGERPRINT_BYTES = 4

# version(1) + fingerprint(4) + iv(12)
_HEADER_BYTES = 1 + FINGERPRINT_BYTES + IV_BYTES

# The single field carrying everything. Deliberately meaningless.
_FIELD = "d"

# GCM's auth tag. Used only to sanity-check that a payload is long enough to
# possibly be valid before handing it to the cipher.
_TAG_BYTES = 16


class EnvelopeError(Exception):
    """Payload is not a well-formed envelope (shape, encoding, or version)."""


class DecryptionError(Exception):
    """
    Envelope was well-formed but could not be opened.

    Raised for a failed auth tag (wrong key, or the ciphertext was tampered
    with) and for an unrecognised key fingerprint. Deliberately does NOT
    distinguish between those cases in its message: telling a caller *why* a
    decrypt failed is exactly the oracle that padding-oracle-style attacks
    feed on. The specific reason is logged server-side instead.
    """


def key_fingerprint(key: bytes) -> bytes:
    """
    The short, non-secret identifier for a key.

    Safe to publish (see the module docstring): SHA-256 is one-way, and this
    is a 4-byte prefix of a digest over 256 bits of randomness.
    """
    if len(key) != KEY_BYTES:
        raise ValueError(f"AES key must be {KEY_BYTES} bytes, got {len(key)}")
    return hashlib.sha256(key).digest()[:FINGERPRINT_BYTES]


def fingerprint_hex(fingerprint: bytes) -> str:
    """Human-readable form, for startup logs and the decrypt helper only."""
    return fingerprint.hex()


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode_field(env: Any) -> bytes:
    """Pull the packed bytes out of an envelope, validating shape as it goes."""
    if not isinstance(env, Mapping):
        raise EnvelopeError("envelope must be a JSON object")
    blob = env.get(_FIELD)
    if not isinstance(blob, str) or not blob:
        raise EnvelopeError(f"envelope is missing its {_FIELD!r} string")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EnvelopeError("envelope payload is not valid base64") from exc
    if len(raw) < _HEADER_BYTES + _TAG_BYTES:
        raise EnvelopeError("envelope payload is too short to be valid")
    if raw[0] != FORMAT_VERSION:
        raise EnvelopeError(
            f"unsupported envelope version {raw[0]} (this build speaks v{FORMAT_VERSION})"
        )
    return raw


def looks_like_envelope(obj: Any) -> bool:
    """
    True if the SENDER clearly intended this to be an envelope, valid or not.

    Deliberately weaker than is_envelope(): it only asks "is there a non-empty
    string in the payload field", not "does it decode".

    This distinction exists so a corrupt or future-version envelope produces a
    useful error instead of being silently misread. The request path uses this
    one to decide INTENT, then validates strictly -- so a bad base64 payload or
    an unrecognised format version surfaces as the specific EnvelopeError
    message it deserves. Testing validity here instead would fold both cases
    into "not an envelope", hand the mangled body to the endpoint as though it
    were plaintext, and throw away the only diagnostic that explains what went
    wrong -- which would make the format version byte pointless, since the one
    scenario it exists for (a newer client against an older server) is exactly
    the one that would be swallowed.

    A genuine plaintext body has no such field at all, so tolerance for
    unencrypted callers is unaffected.
    """
    if not isinstance(obj, Mapping):
        return False
    value = obj.get(_FIELD)
    return isinstance(value, str) and bool(value)


def is_envelope(obj: Any) -> bool:
    """
    True if obj is a well-formed, decodable envelope.

    Structural validity only -- it does not prove the payload can actually be
    decrypted with any particular key. Used where a definite answer is wanted
    over a diagnostic (the response path, and scripts/decrypt_payload.py, which
    reports "this was never encrypted" as a legitimate result).
    """
    try:
        _decode_field(obj)
    except EnvelopeError:
        return False
    return True


def seal(plaintext: bytes, key: bytes) -> dict[str, str]:
    """
    Encrypt plaintext under key, returning a JSON-serialisable envelope.

    The key's fingerprint is derived here rather than passed in, so a caller
    cannot label a payload with an id that does not match the key that sealed
    it. A fresh random iv is generated here on every call and likewise cannot
    be supplied -- see the module docstring on why neither is a convenience
    worth offering.
    """
    fingerprint = key_fingerprint(key)          # also validates key length
    iv = os.urandom(IV_BYTES)
    ct = AESGCM(key).encrypt(iv, plaintext, None)   # tag appended by the library
    return {_FIELD: _b64e(bytes([FORMAT_VERSION]) + fingerprint + iv + ct)}


def envelope_fingerprint(env: Mapping[str, Any]) -> bytes:
    """
    The key fingerprint an envelope carries, read WITHOUT decrypting.

    Separate from unseal() so the caller can resolve the key from its own
    keyring (including a previous key mid-rotation) and keep this module free
    of any key-management concern.
    """
    raw = _decode_field(env)
    return raw[1 : 1 + FINGERPRINT_BYTES]


def unseal(env: Mapping[str, Any], key: bytes) -> bytes:
    """
    Decrypt an envelope, returning the original plaintext bytes.

    Raises EnvelopeError if the envelope is malformed (a client bug, worth a
    400) and DecryptionError if it is well-formed but unopenable (wrong key or
    tampering, worth a 400 and a server-side log line).
    """
    raw = _decode_field(env)
    if len(key) != KEY_BYTES:
        raise ValueError(f"AES key must be {KEY_BYTES} bytes, got {len(key)}")

    iv = raw[1 + FINGERPRINT_BYTES : _HEADER_BYTES]
    ct = raw[_HEADER_BYTES:]
    try:
        return AESGCM(key).decrypt(iv, ct, None)
    except InvalidTag as exc:
        # Wrong key, or the ciphertext/iv was modified in transit. Same opaque
        # error either way -- see DecryptionError's docstring.
        raise DecryptionError("payload could not be decrypted") from exc
