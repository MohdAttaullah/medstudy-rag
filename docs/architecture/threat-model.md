# Threat model

Scope: the deployed Enterprise Medical RAG system as of M12. Each entry names the asset, the
threat, the control that exists **today in this repository**, the risk that remains, and what a
production deployment must add. Controls described as existing are ones with code and a test
behind them; everything else is listed as recommended, not implemented.

This is an engineering threat model. It is not a compliance assessment, and nothing in it
establishes HIPAA compliance, certification or clinical validation.

## Assets

| Asset | Why it matters |
|---|---|
| Uploaded source documents | Tenant-confidential; the authoritative evidence for every answer |
| Derived artifacts (parses, chunks, vectors, postings) | Reconstruct document content if disclosed |
| Conversations and verified answers | Reveal what a user asked and was told |
| Provider API keys | Direct financial loss and impersonation of this system |
| Infrastructure credentials | Full data access |
| Configuration revisions and audit history | Integrity of the record of who changed what |
| The verified-answer guarantee itself | The product's entire safety claim |

---

## 1. Cross-tenant access

**Threat.** A user of tenant A reads tenant B's documents, chunks, conversations, citations or
settings by guessing or replaying an identifier.

**Existing controls.** Tenant identity comes only from the authenticated principal — from a
verified OIDC claim in production, never from a request body, header or URL. Every repository query
filters on it. A foreign identifier returns *not found* rather than *forbidden*, so existence is
not confirmed to a prober. `test_m12_integration.py` sweeps **every parameterised GET route derived
from the OpenAPI schema** with foreign identifiers and asserts none returns 200; deriving the list
means a route added later is swept automatically. M9 and M10 add their own conversation and
settings isolation tests. Composite `(id, tenant_id)` foreign keys make a cross-tenant row
unwritable at the database level.

**Residual risk.** A future route that queries without the tenant filter would be caught by the
sweep only if it is a GET with a path parameter. A POST route taking an identifier in its body is
not covered automatically.

**Recommended.** Extend the sweep to body-parameter routes as they are added; consider PostgreSQL
row-level security as defence in depth.

## 2. Source URL leakage

**Threat.** A citation or a shared link grants access to an original document to someone who
should not have it.

**Existing controls.** No presigned or otherwise reusable object-store URL is ever issued — grep
for `generate_presigned` returns nothing. Every source request re-authorizes tenant, document and
version server-side and then *streams* the bytes through the API. A verified citation is not itself
an authorization: opening the source is a separate authorized request. `X-Frame-Options: DENY` and
`frame-ancestors 'none'` prevent a hostile page embedding the viewer.

**Residual risk.** An authorized user can still download and redistribute a document. That is a
data-governance problem, not a technical control this system can impose.

**Recommended.** Watermarking or download auditing if the deployment's policy requires it.

## 3. Provider key leakage

**Threat.** `OPENAI_API_KEY` or an equivalent reaches a browser, a log, a metric, an audit record
or a Git object.

**Existing controls.** Keys are backend-only `SecretStr`, never in a `VITE_`-prefixed variable, and
the compose file delivers them to the API container alone. The settings API projects credentials as
presence booleans and never values. `test_m12_integration.py` compares every endpoint's response
against the actual configured secrets. `test_m12_units.py` proves secret files are read without the
value entering a message. The frontend bundle is scanned at build time. `.env` is gitignored.
Production supports `<NAME>_FILE` indirection so the secret never enters the process environment at
all, where `/proc/<pid>/environ` and crash dumps would expose it.

**Residual risk.** A developer can still paste a key into a commit. No pre-commit secret scanner is
installed.

**Recommended.** A secret-scanning pre-commit hook and server-side push protection; periodic key
rotation, which the `_FILE` pattern already supports without a rebuild.

## 4. Prompt injection from the corpus

**Threat.** An uploaded document contains "Ignore previous instructions…" and the generator or
verifier obeys it — abandoning citation, answering from pretrained knowledge, or approving an
unsupported claim.

**Existing controls.** Both the generator and verifier system policies now declare their inputs
**untrusted quoted data** and state explicitly that an instruction inside a document is content,
not a command, because the document's author is not the operator. Evidence is wrapped in a fence
whose markers are neutralised inside block text, so a document cannot close the region and have
what follows read as operator instruction. The operator's instruction is repeated *after* the
untrusted region so the last thing read is the operator's. Neutralisation touches only the fence
markers, so numbers, units and identifiers reproduce exactly. Adversarial cases are tested.

**The structural control matters more than the prompt.** Even a fully obeyed injection cannot
release an answer: M8 verifies every material claim against the cited evidence deterministically —
numeric, negation, citation identity and provenance checks are code, not model judgement — and M9
returns an answer only on a PASS. An injected instruction can at most cause an abstention.

**Residual risk.** Prompt-level defences are probabilistic. A sufficiently clever injection may
still influence *what the draft says*; it cannot make an unsupported claim pass deterministic
verification.

**Recommended.** Keep the verifier deterministic-first. An independent verifier model would reduce
correlated failure — see the known limitations.

## 5. Malicious PDF upload

**Threat.** A crafted document exhausts the parser, executes code, or traverses the filesystem.

**Existing controls.** Content type and PDF magic bytes are validated; size is capped; filenames
are sanitized and never used as a storage path (object keys are UUIDs, so traversal has no
surface); page count and parse timeouts are bounded; a parse failure isolates to the job and the
document is quarantined rather than retried indefinitely. Parsing happens in the worker, not the
API, so a pathological document cannot take the request path down. Upload is rate-limited per
principal in production.

**Residual risk. There is no malware scanning.** This is stated plainly rather than implied away:
a PDF carrying an exploit for a *downstream* reader would be stored and served back to authorized
users unchanged.

**Recommended.** An antivirus/CDR gateway in front of ingestion. Container resource limits
(`memory`, `cpus`) on the worker — documented in the deployment guide, not yet enforced in
`compose.yaml`.

## 6. Model service exposure

**Threat.** Qdrant, Redis or PostgreSQL is reachable from the internet or the browser.

**Existing controls.** Compose binds every backing service to `127.0.0.1` only. The browser's CSP
sets `connect-src 'self'`, so the page can talk to no other origin. The retrieval service and
worker are internal-only. Production refuses to start if Qdrant or the object store is configured
as localhost, which is the usual symptom of a container talking to itself instead of a private
network.

**Residual risk.** Network isolation is a deployment property. This system can refuse obviously
wrong configuration but cannot verify the actual network topology.

**Recommended.** Network policies restricting egress to the provider endpoints and ingress to the
API; the deployment guide states the required topology.

## 7. Configuration abuse

**Threat.** An administrator — or someone who has taken an admin session — weakens safety through
the settings API.

**Existing controls.** M10 made every safety invariant `IMMUTABLE`: there is no setting for
disabling sufficiency, disabling verification, allowing unverified answers, streaming unverified
tokens, or treating pretrained knowledge as evidence. The editable gate thresholds already sit at
their floor, so every reachable change makes the gate **stricter**. Changes are append-only,
preview-bound and audited; `settings:write` is admin-only. Rate-limited per principal.

**Residual risk.** An admin can still degrade retrieval quality (for example by narrowing the
candidate pool), which lowers answer coverage but cannot release an unsupported answer.

## 8. IDOR

Covered by §1. The distinguishing control is that ownership is resolved *before* any expensive
work, so probing a foreign conversation costs no provider call.

## 9. Audit tampering

**Threat.** Someone edits or deletes the record of a configuration change.

**Existing controls.** A PostgreSQL trigger rejects `UPDATE` and `DELETE` on
`configuration_revisions`; a unique `(tenant_id, revision)` constraint prevents forking history;
the downgrade migration refuses while history exists unless loss is explicitly acknowledged. Tested
with raw SQL against real PostgreSQL, not through the service layer.

**Residual risk.** A database superuser can drop the trigger. Tamper-evidence stops at the database
boundary — there is no cryptographic chaining or off-host log shipping.

**Recommended.** Ship audit events to append-only external storage; restrict the application role
so it cannot alter triggers.

## 10. Denial of service

**Threat.** One caller exhausts provider budget, worker capacity or memory.

**Existing controls.** Per-principal rate limits on Ask, upload and settings writes, mandatory in
production. A JSON body ceiling refuses oversized payloads before parsing. Question length,
candidate pool, evidence budget and output tokens are all bounded by frozen policy. Health and
readiness are never rate-limited, so load does not become an outage. The limiter itself is bounded
in the number of keys it tracks.

**Residual risk. Rate limiting is per replica, not global.** N replicas allow N times the budget.
This is stated in the deployment guide rather than disguised: a distributed limiter belongs in the
ingress, where the whole fleet's traffic is visible.

**Recommended.** Ingress- or gateway-level global rate limiting; container memory and CPU limits.

## 11. Supply-chain and model tampering

**Threat.** A dependency or a model weight is replaced with a hostile version.

**Existing controls.** Python dependencies are resolved from a committed `uv.lock` and installed
with `--frozen`; the frontend has a committed `package-lock.json`; the base image is a pinned tag.
All three MedCPT models are pinned by exact revision **and** SHA-256 checksum, and load offline —
production now refuses to start unless `offline` is true for the embedding and query encoders, so a
deployment that forgets the variable cannot silently fetch a new revision at query time. A checksum
mismatch fails readiness rather than serving.

**Residual risk.** Base images are pinned by tag, not digest, so `python:3.12-slim` can move.

**Recommended.** Pin base images by digest; run dependency and container scanning in CI; publish an
SBOM per release.

## 12. Development authentication reaching production

**Threat.** The static development bearer tokens — no expiry, no revocation, no issuer — serve real
traffic.

**Existing controls.** Two independent barriers. `Settings` refuses to construct a production
configuration whose auth mode is not `oidc` or which carries any development principal, so the
process cannot start. `build_auth_provider` refuses again if handed a production configuration
selecting the development adapter, covering a `Settings` object built by a test helper that
bypassed validation. Both are tested, and the preflight script reports the mode before deployment.

**Residual risk.** An operator could set `environment=development` in a production deployment. No
system can distinguish that from an actual development environment.

**Recommended.** Set `MEDRAG_ENVIRONMENT` from the deployment pipeline, not from an image default.

## 13. Token forgery and replay

**Threat.** A forged or replayed JWT authenticates as another user or tenant.

**Existing controls.** Signature verification against the issuer's JWKS is unconditional — there is
no flag to disable it, and a test asserts the source contains no such option. Algorithms come from
configuration, never from the token header, so `alg: none` and RS256→HS256 downgrade are both
impossible; only asymmetric algorithms are supported at all, so this service never holds a key
capable of minting tokens. Issuer, audience, expiry and issued-at are all required and verified.
An unmapped role grants nothing. A missing tenant claim is refused rather than defaulted. Rejections
return one opaque message so a prober cannot learn which half of an attempt succeeded.

**Residual risk.** Token lifetime and revocation are the identity provider's responsibility; a
stolen token is valid until it expires. There is no token-revocation check.

**Recommended.** Short access-token lifetimes; back-channel logout or introspection if the
deployment's risk appetite requires immediate revocation.

## 14. Patient-specific misuse

**Threat.** A user asks for individual diagnosis or treatment and receives it.

**Existing controls.** The generation policy forbids addressing an individual patient or giving
personal medical advice, and the product scope is educational corpus QA. Every substantive claim
must still be supported by indexed evidence, so a patient-specific answer has nothing to ground it
and abstains.

**Residual risk.** There is no classifier that detects and refuses a patient-specific question
up front; the refusal is a consequence of grounding rather than an explicit scope check.

**Recommended.** An explicit scope check if the deployment expects such questions.
