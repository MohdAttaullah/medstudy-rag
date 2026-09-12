# Security boundary

M1 data endpoints require a server-authenticated Principal. AuthProvider separates identity
verification from domain permission checks. The development adapter compares configured random
bearer credentials in constant time and derives user, tenant and role from server configuration.
Request bodies cannot supply their own actor/tenant. Unknown/missing credentials return 401;
insufficient permission returns 403; cross-tenant resource lookups return 404.

| Role | Permissions |
|---|---|
| reader | document:read, ingestion:read |
| curator | Reader plus document:upload, document:manage, ingestion:retry, ingestion:reparse, ingestion:rechunk, ingestion:reembed, ingestion:reindex, ingestion:cancel, ingestion:accept |
| admin | Curator plus audit:read |

Queries and service actions apply tenant constraints, backed by composite database foreign keys.
Opaque UUIDs and hidden buttons are not controls. The UI mirrors permissions but the API enforces
them. Source download authenticates and verifies document/version membership before opening a pinned
private object version. API responses are no-store and source downloads are attachments with nosniff.

Parsed material inherits exactly the security boundary of the original it was derived from. Every
parse inspection route resolves the owning document and version under the caller's tenant before
the parse-run child resource, and a run whose version does not match the path is a 404. Page
previews, figure crops and the raw parser artifact stream through the API with no-store, nosniff
and a `default-src 'none'; sandbox` content-security policy; object-storage keys, endpoints and
pre-signed URLs are never returned. Reading the raw parser artifact additionally requires
`document:manage`, since it is the complete unfiltered parser output.

`scripts/init_dev_auth.py` provisions ignored local credentials without rotating existing ones.
The browser stores its key only in tab memory and clears query caches on identity changes.
This adapter is explicitly development-only. Production startup is rejected; OIDC signature/issuer/
audience validation, membership administration, credential expiry/revocation and production session
security are not implemented.

## Untrusted uploads

PDF-only input checks filename traversal/control characters, extension, MIME, declared and streamed
size, nonempty content, SHA-256, signature/EOF and basic pypdf catalog/page-tree structure.
Encrypted PDFs and detected root active actions/embedded scripts or files are rejected.
The parser runs in a subprocess with a time limit. Upload bytes spool to disk; S3 transfers use bounded
multipart buffers. UUID paths prevent user filename control over storage identity.
Invalid files produce sanitized rejection audit; authenticated permission denials are audited too.
Unknown callers have no trusted tenant for persistent domain audit and appear in HTTP telemetry.

The M2 parser is a further untrusted-input surface: it runs in the worker process with a
configured document timeout, a page-count guard, an artifact size ceiling, a per-job temporary
directory removed on every outcome, and remote services and external plugins disabled so parsing
cannot call out of the worker. Parser exception text, file paths and model details never reach a
durable record or an API response; only fixed operator-safe messages and codes do. Document
content is treated as data throughout: no parsed text is ever interpreted as an instruction, and
no model is invoked during parsing.

`ingestion:accept` records a curator's decision to admit a parse the deterministic quality layer flagged. It sits with the operate-the-pipeline capabilities because a curator already holds reparse and cancel, which discard and regenerate the evidence dataset outright. Accepting never rewrites the validation result or removes a finding; it writes an append-only decision bound to one exact parse run (ADR-018). Readers never hold it.

M3 chunking inherits that posture and narrows it further. The builder reads only frozen in-memory
contracts, so it can reach neither the database, the parser library nor object storage; the only
model artifact it loads is the bundled tokenizer, whose revision, file checksum and runtime version
are verified before use and which is used solely to count and slice tokens. No network call, no
embedding and no generation happens at this stage, and question text, options and answers are
carried verbatim as data rather than executed as instructions. Chunk inspection routes enforce the
same tenant boundary as the original and expose no storage key, lease token or vector field; the
rechunk action requires its own `ingestion:rechunk` permission and supplying a custom chunk policy
additionally requires `audit:read`.

M4 adds a second datastore and keeps it internal. Qdrant is never reachable from the browser: the
frontend has no vector-database client and no credential, and the single live-statistics endpoint
is server-side, tenant-scoped and requires `document:manage`. Port 6333 is published only for local
development and must not be exposed publicly. Index payloads carry routing and provenance only —
never chunk text and never a provider secret — so an index exposure would not disclose licensed
source material. Tenant scope is a server-side payload filter on an indexed, tenant-oriented key,
proven at the repository boundary by integration tests rather than by frontend filtering. No dense
vector is returned by any route. The re-embed action requires `ingestion:reembed`, and supplying a
custom embedding policy additionally requires `audit:read`. The embedding worker loads only a
checksum-verified local model and, in the container, runs with downloads disabled, so no ingestion
path reaches an external model host at request time.

These checks are not antivirus or comprehensive PDF sanitization. Subprocess validation has a time
limit but no dedicated hard memory sandbox, and the parser itself runs in the worker process
rather than an isolated sandbox with a hard memory cap. Public upload deployment requires malware scanning,
stronger parser isolation, per-user quotas/rate/concurrency limits, scoped storage credentials and
resource budgets. The local API/frontend run unprivileged, but infrastructure credentials are for
development only. Do not expose this stack publicly or use it for patient data.

Originals live in a private, versioned bucket; no storage root credentials or keys reach the browser.
Failure compensation only removes uncommitted intent keys. Archive retains source/history; no
retention scheduler or permanent-deletion API exists. Backup/restore, legal retention, TLS, managed
secrets, network isolation and production telemetry access control remain unresolved.

Logs allow only operational fields and omit uploaded bytes, metadata text, secrets and exception
bodies. Correlation/resource UUIDs are diagnostic identifiers, not evidence or authorization.
Metrics use bounded labels. Health/metrics remain unauthenticated on loopback development ports.
Future corpus/model boundaries must treat document instructions as untrusted and preserve grounding
and tenant isolation. No compliance certification follows from these controls.


## M10 configuration

`settings:read` and `settings:write` are admin-only and tenant-scoped; curators and readers hold
neither. Shared SYSTEM-scope fields are read-only. Backend registry validation, revision checks and
mandatory auditing govern every change. Secrets are managed externally and never returned; see
[configuration management](configuration-management.md).


## M12 production hardening

**Identity.** Production requires OIDC. `Settings.production_is_hardened` refuses to construct a
production configuration whose auth mode is not `oidc` or which carries any development principal,
and `build_auth_provider` refuses again if handed one — two independent barriers, because the
static development tokens have no expiry, no revocation and no issuer. Signature verification
against the issuer's JWKS is unconditional and there is no flag to disable it. Algorithms come from
configuration, never from the token header, so `alg: none` and RS256→HS256 downgrade are both
impossible; only asymmetric algorithms are supported, so this service never holds a key that could
mint tokens. Tenant identity comes from a verified claim, an unmapped role grants nothing, and a
token carrying several mapped roles resolves to the narrowest. Vendor-neutral: issuer, audience and
claim names are configuration.

**RBAC.** The `PERMISSIONS` inventory now covers every capability any route enforces — it
previously omitted `audit:read` and `settings:*` while routes enforced them, so it was not usable
for review, and a test now fails if a route enforces a capability the inventory lacks. Roles are
explicit subsets rather than "curator gets everything", so adding a capability no longer widens
curator silently. `operations:read` is new and guards read-only operational state; the brief's
`operations:admin` maps to the existing `ingestion:*` scopes rather than being duplicated.

**Transport.** Security headers on every response including errors: CSP with `frame-ancestors
'none'`, `object-src 'none'` and `connect-src 'self'`, plus `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, COOP/CORP and a restrictive
`Permissions-Policy`. HSTS is opt-in because sending it from a service reached over plain HTTP pins
browsers to HTTPS a host does not serve. Production CORS must be explicit HTTPS origins; a wildcard
is refused on a credentialed API.

**Abuse ceilings.** Per-principal rate limits on Ask, upload and settings writes, mandatory in
production and off by default elsewhere so local work is unobstructed. JSON bodies capped at 256 KB
and refused with 413 before parsing. Health and readiness are never limited.

**Secrets.** `<NAME>_FILE` indirection supports every managed secret store without binding to one,
and keeps values out of the process environment where `/proc/<pid>/environ` would expose them. Only
known names are resolved; a direct value wins; an empty or unreadable file fails startup naming the
path, never the value.

**Prompt injection.** Both the generator and verifier policies declare their inputs untrusted
quoted data. Evidence is fenced with markers that document text cannot forge or close, and the
operator's instruction is repeated after the untrusted region. Neutralisation touches only the fence
markers, so numbers, units and identifiers reproduce exactly. The structural defence matters more:
M8's deterministic checks are code, so an obeyed injection can at most cause an abstention.

See the [threat model](threat-model.md) and [production deployment](production-deployment.md).
