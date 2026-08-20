# API Payload Encryption

Remediation for the VAPT finding that sensitive data was readable in API
responses. JSON request and response **bodies** are encrypted with
**AES-256-GCM** under a key shared between the backend and the frontend build
for a given environment.

Status codes, headers, and URLs are unchanged. `X-Request-Id` deliberately
stays in the clear so a user-reported error can still be traced to its log line.

---

## What changed, and what didn't

| | |
|---|---|
| Backend | `app/common/crypto/` (new) + 1 middleware registration in `main.py` |
| Frontend | `lib/crypto/envelope.ts` (new) + 3 interceptors in `lib/api.ts` |
| Endpoints changed | **0 of 113** |
| Services / schemas / models changed | **0** |
| Components changed | **0** |

Handlers still return plain dicts and still receive plain parsed bodies. The
96 exported helpers in `api.ts` still send and receive ordinary objects. This
was the binding constraint: the feature cannot break business logic it never
touches.

---

## Wire format

One opaque base64 string in a single-field object:

```json
{ "d": "AVZqU3fQ9yUXv9EoMNbTKTDkc+pfkQNh4ce7Dt2b7dwdUJhfIA==" }
```

Base64-decoded, that is a fixed 17-byte header followed by the ciphertext:

| Offset | Size | Meaning |
|---|---|---|
| 0 | 1 | Format version (currently 1). An unrecognised version is reported as such rather than failing as a bad decrypt. |
| 1 | 4 | **Key fingerprint** — which key sealed this. Enables the rotation window below. |
| 5 | 12 | 96-bit iv/nonce. Not secret, but **fresh random per message** — see below. |
| 17 | rest | AES-256-GCM ciphertext with its 16-byte auth tag **appended**. |

**The key fingerprint** is the first 4 bytes of `SHA-256(key)`, derived rather
than configured. Publishing it is safe — SHA-256 is one-way, and this is a
4-byte prefix of a digest over 256 bits of randomness. It is the same idea as a
JWK thumbprint. Because it is derived, there is no key-id setting to get wrong,
no way for two environments to share an id, and a new key automatically gets a
new fingerprint.

**On the compact format.** An earlier revision sent
`{"v":1,"kid":"local-1","iv":...,"ct":...}`. It was replaced for one real
reason and one presentational one:

- **Real:** the key id was a human-readable environment label (`local-1`,
  `uat-1`), disclosing the environment naming scheme for no benefit. The
  fingerprint identifies the key without naming anything.
- **Presentational, not security:** named `iv`/`ct` fields document the scheme
  to anyone reading a captured payload. Be clear-eyed that hiding them buys
  **nothing** cryptographically — the algorithm is in plain sight in the
  frontend bundle, so whoever can read a payload can also read how it was made.
  Security here rests entirely on the key, never on the format being
  unfamiliar. What the compact form does buy is 19 fewer bytes per response and
  a payload that does not read as a homegrown construction in an audit report.

Do not mistake the second point for a security property.

**On the IV.** AES-GCM XORs a keystream derived from (key, iv) with the
plaintext. Encrypt two payloads under the same (key, iv) and XOR-ing the two
ciphertexts yields the XOR of the two plaintexts with no key involved; a
repeated nonce also leaks enough to forge valid ciphertexts. It is generated
inside `seal()` on both sides and cannot be passed in by a caller.

**On the tag.** Both Python's `cryptography` and the browser's WebCrypto
produce and expect tag-appended ciphertext. It is deliberately not split into
its own field — doing so is the most common cause of "encrypts in Python,
fails in the browser".

GCM is an AEAD mode, so this provides integrity as well as confidentiality: a
modified ciphertext fails to decrypt rather than yielding garbage. (AES-CBC
would not, which is why it is not used.)

---

## Not encrypted, on purpose

| Left readable | Why |
|---|---|
| 4 download endpoints (`StreamingResponse`) | File/CSV streams. Decided by response content-type, so future exports are covered automatically rather than needing a path list kept up to date. |
| 6 upload endpoints (multipart request bodies) | A multipart file stream cannot be JSON-wrapped. Their **responses** are still encrypted — the two directions are judged independently. |
| `/health` | Container and load-balancer probes. |
| `/docs`, `/redoc`, `/openapi.json` | Swagger UI would stop working. |
| `OPTIONS` preflights | No body. |
| Empty bodies (204/304) | Sealing zero bytes yields an envelope that decrypts to `""`, which the frontend would then fail to `JSON.parse` — an error invented by the middleware itself. |
| Unhandled `500` bodies | `errors.py` registers a handler for bare `Exception`, which Starlette installs on `ServerErrorMiddleware` — **outside** all user middleware, so no middleware can reach it. The body is the generic `{code: 5000, ...}` and carries no customer, bank, or invoice data and no traceback. Every *deliberate* error (`AppError`, `HTTPException`, `RequestValidationError`) **is** encrypted. |
| Key-mismatch errors from the middleware | An encrypted explanation of a key problem is the one thing the client provably cannot read. |

Plaintext **requests** are still accepted when encryption is on. That keeps the
rollout safe (a cached or half-deployed frontend keeps working rather than
every request failing at once) and is what keeps the multipart upload path
viable. It is not a weakened boundary: the finding concerns data being
*readable in responses*, not compelling clients to encrypt.

---

## Configuration

Encryption is **on by default in every environment, local included** — local
used to be the exception, which meant the one environment everyone iterates in
was the one not exercising the code path.

`API_ENCRYPTION_KEY` is **required**, with no in-code default: a key committed
to the repository would not be a secret, and every environment left on it would
share one. A ready-to-use value ships in `backend.env.local.example` — copy it
into `.env` for local work. With encryption on and no key set, **startup fails**
with a message naming the setting, rather than booting and failing every request.

**Backend** (`.env`)

```
API_ENCRYPTION_KEY=<base64, 32 bytes>     # required
# API_ENCRYPTION_ENABLED=false            # opt out (plaintext curl/pytest)
# API_ENCRYPTION_KEY_PREVIOUS=            # during rotation only
```

> Use a **distinct key per environment**, so a key lifted from a UAT bundle
> decrypts nothing in prod. Do not reuse the sample value from
> `backend.env.local.example` for UAT or prod — it is in version control.

**Frontend** (`.env.local` / `.env.uat`)

```
NEXT_PUBLIC_API_ENCRYPTION_KEY=<the same base64 value>
```

There is no key-id on either side — the fingerprint is derived from the key.

Mint a key with:

```bash
python -m scripts.gen_api_key
```

Use a **different key per environment**, so a key lifted from the UAT frontend
bundle decrypts nothing in prod.

Two things that are load-bearing:

- **The frontend key is inlined at build time.** Changing it requires a
  *rebuild*, not a restart.
- **HTTPS is required.** Browsers withhold `crypto.subtle` outside a secure
  context (localhost excepted), so the frontend cannot decrypt anything over
  plain HTTP.

A missing or malformed key makes the backend **refuse to start**, with a
message naming the setting — deliberately, so a misconfigured deploy fails
visibly at boot rather than 500-ing on every request afterwards.

---

## Rotating the key

The fingerprint in each payload is what makes this three unhurried deploys
instead of one synchronised one. Without it, steps 1 and 2 would have to happen
in the same instant, and any skew would break every request.

1. New key → `API_ENCRYPTION_KEY`. Old key → `API_ENCRYPTION_KEY_PREVIOUS`.
   Deploy the backend. It now seals with the new key but still opens payloads
   sealed with the old one, so the live frontend keeps working.
2. Rebuild and deploy the frontend with the new key.
3. Remove the `_PREVIOUS` line whenever convenient.

Startup logs the sealing key's fingerprint and every accepted fingerprint, so
you can confirm a rotation window is genuinely open.

---

## Debugging an encrypted environment

Encryption hides payloads from an attacker and from you equally, so the tool
ships with the feature:

```bash
# straight from curl
curl -s https://uat-host/api/auth/me -H "Authorization: Bearer $TOKEN" \
    | python -m scripts.decrypt_payload

# from a file saved out of Burp or browser devtools
python -m scripts.decrypt_payload captured.json

# a capture from an environment this checkout isn't configured for
python -m scripts.decrypt_payload --key "$UAT_KEY" captured.json
```

It reads keys from this checkout's own settings by default, so no key needs
pasting into a shell (and into shell history). It also reports when a payload
was never encrypted at all, which is an answer rather than a failure.

Local development is now encrypted like every other environment, so a decryption
bug shows up where you are already working rather than first appearing in UAT.
For plaintext work — existing curl scripts, pytest, the reopen test guide — set
`API_ENCRYPTION_ENABLED=false` and restart.

---

## Known limitation — state this before the retest

Under a single shared static key, that key is inlined into the client bundle
and is therefore readable by anyone who loads the application. This raises the
effort required to harvest API payloads and removes them from casual proxy
inspection, but **it is not confidentiality against an attacker who has the
bundle.** A legitimately authenticated user — or XSS running in their browser —
can read the plaintext, which is inherent to any scheme where the client must
render the data.

Compensating controls already in place:

- TLS in transit (a hard requirement here, per the WebCrypto note above).
- Field masking at serialization: bank account numbers are masked by
  `app/common/account_masking.py`, with the real value only ever returned by
  dedicated reveal endpoints that re-check permission and audit-log the reveal.
- RBAC per endpoint, plus audit logging of domain-significant events.

If the retest requires that the key not be recoverable from the client, the
upgrade is a **per-session key** negotiated at login (ECDH/X25519 + HKDF, or
RSA-OAEP key wrapping), which is what makes the key per-user, replay-resistant,
and automatically rotated. That change touches only where the key comes
from — the envelope format, the middleware, and all 113 endpoints stay exactly
as they are, which is why the key fingerprint and the versioned envelope header
were built in from the first commit.
